"""ReviewStore: очередь, переходы, append-only журнал."""

from __future__ import annotations

import json
import sqlite3

import pytest

from stamp_flow.review.models import ItemState
from stamp_flow.review.store import ReviewStore, TransitionError
from hh_applicant_tool.storage import StorageFacade


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    StorageFacade(c)  # применяет schema.sql
    yield c
    c.close()


@pytest.fixture
def store(conn, tmp_path):
    return ReviewStore(conn, log_dir=tmp_path / "review-log", run_id="run-1")


def _enqueue(store, vacancy_id=1, steps=("letter",)):
    return store.enqueue(
        resume_id="r1",
        vacancy_id=vacancy_id,
        steps=list(steps),
        vacancy={"id": str(vacancy_id), "name": "Python dev"},
        resume_text="resume",
    )


def test_enqueue_and_find(store):
    item = _enqueue(store)
    assert item.state is ItemState.QUEUED
    assert item.steps == ["letter"]
    assert item.vacancy_json["name"] == "Python dev"
    assert store.is_known("r1", "1")
    assert not store.is_known("r1", 2)
    assert store.next_queued().id == item.id


def test_duplicate_vacancy_rejected(store):
    _enqueue(store)
    with pytest.raises(sqlite3.IntegrityError):
        _enqueue(store)


def test_happy_path_transitions(store):
    item = _enqueue(store, steps=["letter", "questionnaire"])
    item = store.set_state(item, ItemState.IN_REVIEW)
    item = store.update(item, step_idx=1, letter_final="text")
    assert item.current_step == "questionnaire"
    for state in (
        ItemState.APPROVED,
        ItemState.SUBMITTING,
        ItemState.SUBMITTED,
    ):
        item = store.set_state(item, state)
    assert item.state is ItemState.SUBMITTED


def test_forbidden_transitions(store):
    item = _enqueue(store)
    with pytest.raises(TransitionError):
        store.set_state(item, ItemState.SUBMITTING)
    item = store.set_state(item, ItemState.IN_REVIEW)
    item = store.set_state(item, ItemState.REJECTED)
    with pytest.raises(TransitionError):
        store.set_state(item, ItemState.QUEUED)


def test_retry_and_stale_paths(store):
    item = _enqueue(store)
    for state in (ItemState.IN_REVIEW, ItemState.APPROVED, ItemState.SUBMITTING):
        item = store.set_state(item, state)
    item = store.set_state(item, ItemState.FAILED_RETRYABLE)
    item = store.set_state(item, ItemState.SUBMITTING)
    item = store.set_state(item, ItemState.STALE)
    item = store.set_state(item, ItemState.QUEUED)
    assert item.state is ItemState.QUEUED


def test_update_rejects_unknown_fields(store):
    item = _enqueue(store)
    with pytest.raises(ValueError):
        store.update(item, state="submitted")


def test_events_are_append_only(store, conn):
    _enqueue(store)
    assert conn.execute("SELECT count(*) FROM review_events").fetchone()[0] > 0
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE review_events SET type = 'x'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM review_events")


def test_items_cannot_be_deleted(store, conn):
    _enqueue(store)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM review_items")


def test_generic_repository_clear_cannot_wipe_log(store, conn):
    """`query "DELETE ..."` / BaseRepository.clear() не трогают журнал."""
    from hh_applicant_tool.storage.repositories.errors import RepositoryError

    _enqueue(store)
    before = conn.execute("SELECT count(*) FROM review_events").fetchone()[0]

    storage = StorageFacade(conn)
    storage.skipped_vacancies.clear()  # как clear-skipped
    storage.settings.clear()

    with pytest.raises((sqlite3.IntegrityError, RepositoryError)):
        storage.skipped_vacancies.conn.execute("DELETE FROM review_events")
    after = conn.execute("SELECT count(*) FROM review_events").fetchone()[0]
    assert after == before


def test_schema_reinit_keeps_data(store, conn):
    _enqueue(store)
    StorageFacade(conn)  # schema.sql выполняется при каждом старте
    assert store.is_known("r1", 1)


def test_jsonl_mirror_and_redaction(store, tmp_path):
    item = _enqueue(store)
    store.log_event(
        "llm_request",
        {"prompt": "hi", "api_key": "sk-secret", "nested": {"token": "t"}},
        item_id=item.id,
    )
    files = list((tmp_path / "review-log").glob("*.jsonl"))
    assert len(files) == 1
    lines = files[0].read_text(encoding="utf-8").splitlines()
    records = [json.loads(line) for line in lines]
    assert [r["type"] for r in records] == ["enqueued", "llm_request"]
    last = records[-1]
    assert last["run_id"] == "run-1"
    assert last["payload"]["api_key"] == "***"
    assert last["payload"]["nested"]["token"] == "***"
    assert "sk-secret" not in files[0].read_text(encoding="utf-8")
    db_payload = store.events(type="llm_request")[0]["payload"]
    assert db_payload["api_key"] == "***"


def test_jsonl_only_appends(store, tmp_path):
    _enqueue(store, vacancy_id=1)
    _enqueue(store, vacancy_id=2)
    path = next((tmp_path / "review-log").glob("*.jsonl"))
    assert len(path.read_text(encoding="utf-8").splitlines()) == 2


def test_state_change_logged(store):
    item = _enqueue(store)
    store.set_state(item, ItemState.IN_REVIEW, reason="card sent")
    ev = store.events(type="state_changed")[-1]
    assert ev["payload"] == {
        "from": "queued",
        "to": "in_review",
        "reason": "card sent",
    }
