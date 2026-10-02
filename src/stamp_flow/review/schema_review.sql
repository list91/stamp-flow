/* stamp-flow: таблицы Telegram-ревью откликов.
   Применяются идемпотентно при открытии ReviewStore (CREATE ... IF NOT EXISTS). */

-- Текущее состояние вакансии в ревью. Строки можно менять, удалять нельзя.
CREATE TABLE IF NOT EXISTS review_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    resume_id TEXT NOT NULL,
    vacancy_id INTEGER NOT NULL,
    state TEXT NOT NULL,
    steps TEXT NOT NULL DEFAULT '[]',
    step_idx INTEGER NOT NULL DEFAULT 0,
    dry_run BOOLEAN NOT NULL DEFAULT 0,
    verdict_json TEXT,
    vacancy_json TEXT NOT NULL,
    resume_text TEXT NOT NULL DEFAULT '',
    test_json TEXT,
    letter_draft TEXT,
    letter_final TEXT,
    answers_draft_json TEXT,
    answers_json TEXT,
    tg_message_ids TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (resume_id, vacancy_id)
);

-- Журнал: только INSERT. UPDATE/DELETE запрещены триггерами.
CREATE TABLE IF NOT EXISTS review_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts DATETIME DEFAULT CURRENT_TIMESTAMP,
    run_id TEXT,
    item_id INTEGER,
    resume_id TEXT,
    vacancy_id INTEGER,
    type TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_review_items_state ON review_items(state);
CREATE INDEX IF NOT EXISTS idx_review_events_vacancy ON review_events(vacancy_id);
CREATE INDEX IF NOT EXISTS idx_review_events_type ON review_events(type);
CREATE INDEX IF NOT EXISTS idx_review_events_ts ON review_events(ts);

CREATE TRIGGER IF NOT EXISTS trg_review_items_updated
AFTER UPDATE ON review_items
BEGIN
    UPDATE review_items
    SET updated_at = CURRENT_TIMESTAMP
    WHERE id = OLD.id;
END;

CREATE TRIGGER IF NOT EXISTS trg_review_items_no_delete
BEFORE DELETE ON review_items
BEGIN
    SELECT RAISE(ABORT, 'review_items: delete forbidden');
END;

CREATE TRIGGER IF NOT EXISTS trg_review_events_no_update
BEFORE UPDATE ON review_events
BEGIN
    SELECT RAISE(ABORT, 'review_events: append-only');
END;

CREATE TRIGGER IF NOT EXISTS trg_review_events_no_delete
BEFORE DELETE ON review_events
BEGIN
    SELECT RAISE(ABORT, 'review_events: append-only');
END;
