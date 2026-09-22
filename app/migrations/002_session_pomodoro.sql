-- Slice 8: optional 25/5 Pomodoro display for a running study session.
ALTER TABLE sessions ADD COLUMN pomodoro INTEGER NOT NULL DEFAULT 0;
CREATE INDEX sessions_started ON sessions(started_at);
