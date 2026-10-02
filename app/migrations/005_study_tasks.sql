-- Study tasks: planned work (by hand or from an Ask proposal), separate from the PA rubric tasks in pa_tasks.
-- Additive only. A task with done_rule 'cards_reviewed' or 'quiz_finished' is closed by the app from real
-- review / quiz events (evidence says which); 'manual' is closed only by the student.
CREATE TABLE study_tasks (
  id            INTEGER PRIMARY KEY,
  course_id     INTEGER REFERENCES courses(id) ON DELETE CASCADE,        -- NULL = not tied to a course
  competency_id INTEGER REFERENCES competencies(id) ON DELETE SET NULL,
  note          TEXT NOT NULL DEFAULT '',                                -- optional note name / link text
  title         TEXT NOT NULL,
  kind          TEXT NOT NULL DEFAULT 'study'
                CHECK (kind IN ('study','review_cards','quiz','read','write','other')),
  status        TEXT NOT NULL DEFAULT 'todo'
                CHECK (status IN ('todo','doing','blocked','done','cancelled')),
  due_on        TEXT,                                                    -- YYYY-MM-DD
  est_minutes   INTEGER,
  priority      INTEGER NOT NULL DEFAULT 2,                              -- 1 high, 2 normal, 3 low
  source        TEXT NOT NULL DEFAULT 'manual',                          -- 'manual' or 'proposal:<agent_proposals.id>'
  done_rule     TEXT NOT NULL DEFAULT 'manual'
                CHECK (done_rule IN ('manual','cards_reviewed','quiz_finished')),
  evidence      TEXT,                                                    -- JSON: what closed it
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  done_at       TEXT
);
CREATE INDEX study_tasks_status ON study_tasks(status, due_on);
CREATE INDEX study_tasks_course ON study_tasks(course_id, status);
