from types import SimpleNamespace

from stamp_flow.review.runner import ReviewPass


def test_pooled_text_current_first():
    text = ReviewPass._pooled_text("a", {"a": "AAA", "b": "BBB"})
    assert text.index("AAA") < text.index("BBB")
    assert "ТЕКУЩЕЕ" in text
    assert ReviewPass._pooled_text("a", {"a": "AAA"}) == "AAA"


def test_rotation_shifts_start(tmp_path):
    rp = ReviewPass.__new__(ReviewPass)
    rp.dry_run = True
    rp.tool = SimpleNamespace(config_path=tmp_path)
    rs = [{"id": i} for i in "abcd"]
    firsts = [rp._rotate(rs)[0]["id"] for _ in range(5)]
    assert firsts == ["a", "b", "c", "d", "a"]


def test_rounds_quota_order():
    calls = []
    rp = ReviewPass.__new__(ReviewPass)
    rp.stop = False
    rp.args = SimpleNamespace(per_resume=2, rounds=2)
    rp._cancelled = lambda: False
    from collections import Counter

    rp.stats = Counter()
    rp.store = SimpleNamespace(log_event=lambda *a, **k: None)
    rp.tool = SimpleNamespace(config={})
    rp._rotate = lambda r: r
    pauses = []
    rp.resume_delay = rp.round_delay = (1, 1)
    rp._pause = lambda rng, kind, label: pauses.append(kind)
    rp._load_texts = lambda r: {x["id"]: x["id"] for x in r}
    rp.op = SimpleNamespace(_get_vacancies=lambda resume_id: [{"id": f"{resume_id}{i}"} for i in range(5)])

    def handle(resume, text, vac):
        calls.append(vac["id"])
        return True

    rp._handle_vacancy = handle
    import stamp_flow.review.runner as m

    orig = m.review_config
    m.review_config = lambda tool: {}
    try:
        rp._run_rounds([{"id": "x"}, {"id": "y"}])
    finally:
        m.review_config = orig
    assert calls == ["x0", "x1", "y0", "y1", "x2", "x3", "y2", "y3"]
    assert pauses == ["resume", "round", "resume"]


def test_single_pause_when_first_resume_exhausted():
    """x кончился в круге 1 → в круге 2 перед y только пауза круга, без паузы резюме."""
    from collections import Counter

    import stamp_flow.review.runner as m

    rp = ReviewPass.__new__(ReviewPass)
    rp.stop = False
    rp.args = SimpleNamespace(per_resume=2, rounds=2)
    rp._cancelled = lambda: False
    rp.stats = Counter()
    rp.store = SimpleNamespace(log_event=lambda *a, **k: None)
    rp.tool = SimpleNamespace(config={})
    rp._rotate = lambda r: r
    pauses = []
    rp._pause = lambda rng, kind, label: pauses.append(kind)
    rp.resume_delay = rp.round_delay = (1, 1)
    rp._load_texts = lambda r: {x["id"]: x["id"] for x in r}
    counts = {"x": 1, "y": 5}
    rp.op = SimpleNamespace(
        _get_vacancies=lambda resume_id: [{"id": f"{resume_id}{i}"} for i in range(counts[resume_id])]
    )
    rp._handle_vacancy = lambda resume, text, vac: True
    orig = m.review_config
    m.review_config = lambda tool: {}
    try:
        rp._run_rounds([{"id": "x"}, {"id": "y"}])
    finally:
        m.review_config = orig
    assert pauses == ["resume", "round"]


def test_deferred_not_counted_and_lookahead():
    """Лимит исчерпан: вакансии без ревью не в квоту; ревью — в квоту; стоп по lookahead."""
    from collections import Counter

    import stamp_flow.review.runner as m

    rp = ReviewPass.__new__(ReviewPass)
    rp.stop = False
    rp._cancelled = lambda: False
    rp.stats = Counter()
    seen = []

    def handle(resume, text, vac):
        seen.append(vac["id"])
        if vac["review"]:
            return True
        rp._deferred = True
        return False

    rp._handle_vacancy = handle
    # квота 2: две ревью-вакансии среди отложенных — обе засчитаны, отложенные нет
    vacs = [{"id": i, "review": i in (3, 7)} for i in range(20)]
    assert rp._run_resume({"id": "r"}, "", iter(vacs), quota=2) is True
    assert seen == list(range(8))
    # только отложенные: стоп через quota*LOOKAHEAD, вакансии не «кончились»
    seen.clear()
    vacs = [{"id": i, "review": False} for i in range(100)]
    assert rp._run_resume({"id": "r"}, "", iter(vacs), quota=2) is True
    assert len(seen) == 2 * m.LOOKAHEAD


def test_pacer_skips_after_structural_pause():
    from datetime import datetime, timedelta, timezone

    from stamp_flow.review import pacing

    now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    last = [now - timedelta(seconds=10)]
    orig = pacing.real_applications
    pacing.real_applications = lambda store, since: list(last)
    try:
        waits = []
        p = pacing.Pacer(None, delay=(60, 60), now=lambda: now, waiter=lambda s: waits.append(s) or s)
        assert 49 <= p.remaining() <= 50
        p.skip_next = True
        assert p.wait_before_apply() == 0.0 and not waits  # покрыто паузой
        assert p.wait_before_apply() > 0 and waits  # дальше — обычная пауза
        p.skip_next = True
        last.append(now)  # между паузой и откликом был другой отклик
        assert p.wait_before_apply() > 0
    finally:
        pacing.real_applications = orig


def test_wait_board_skip(tmp_path):
    from stamp_flow.review.timer import WaitBoard

    board = WaitBoard(tmp_path)
    ticks = []

    def sleep(s):
        ticks.append(s)
        if len(ticks) == 3:
            board.request_skip(board.current()["id"])

    waited = board.wait(100, "vacancy", "x", sleep=sleep)
    assert len(ticks) == 3 and waited < 5
    assert board.current() is None
