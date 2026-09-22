-- studyvault initial schema. Dates are ISO strings (YYYY-MM-DD), timestamps ISO datetimes, local time.

CREATE TABLE meta (
  key   TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE program (
  id       INTEGER PRIMARY KEY CHECK (id = 1),
  name     TEXT NOT NULL,
  total_cu INTEGER NOT NULL
);

CREATE TABLE transfers (
  id    INTEGER PRIMARY KEY,
  code  TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL,
  cu    INTEGER NOT NULL,
  via   TEXT
);

CREATE TABLE terms (
  id        INTEGER PRIMARY KEY,
  n         INTEGER NOT NULL UNIQUE,
  start     TEXT NOT NULL,
  end       TEXT NOT NULL,
  target_cu INTEGER NOT NULL
);

CREATE TABLE courses (
  id              INTEGER PRIMARY KEY,
  code            TEXT NOT NULL UNIQUE,
  title           TEXT NOT NULL,
  cu              INTEGER NOT NULL,
  term_id         INTEGER REFERENCES terms(id),
  ord             INTEGER NOT NULL DEFAULT 0,
  status          TEXT NOT NULL DEFAULT 'not_started'
                  CHECK (status IN ('not_started','in_progress','pre_assessed','scheduled','passed','revision_needed')),
  assessment_type TEXT CHECK (assessment_type IN ('OA','PA','cert')),
  start           TEXT,
  due             TEXT,
  target          TEXT,
  approved        INTEGER NOT NULL DEFAULT 0,
  cert_name       TEXT,
  after_code      TEXT,
  passed_on       TEXT,
  attempted       INTEGER NOT NULL DEFAULT 0,
  attempted_on    TEXT,
  exam_date       TEXT,
  quiz_target     INTEGER NOT NULL DEFAULT 80
);

CREATE TABLE competencies (
  id            INTEGER PRIMARY KEY,
  course_id     INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
  ord           INTEGER NOT NULL,
  text          TEXT NOT NULL,
  confidence    INTEGER CHECK (confidence BETWEEN 1 AND 5),
  last_reviewed TEXT
);
CREATE INDEX competencies_course ON competencies(course_id, ord);

CREATE TABLE cards (
  id            INTEGER PRIMARY KEY,
  course_id     INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
  competency_id INTEGER REFERENCES competencies(id) ON DELETE SET NULL,
  front         TEXT NOT NULL,
  back          TEXT NOT NULL,
  type          TEXT NOT NULL DEFAULT 'basic' CHECK (type IN ('basic','cloze')),
  ease          REAL NOT NULL DEFAULT 2.5,
  interval      INTEGER NOT NULL DEFAULT 0,
  reps          INTEGER NOT NULL DEFAULT 0,
  due_on        TEXT NOT NULL,
  source        TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('manual','ai-accepted')),
  created_at    TEXT NOT NULL
);
CREATE INDEX cards_due ON cards(course_id, due_on);

CREATE TABLE reviews (
  id          INTEGER PRIMARY KEY,
  card_id     INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
  reviewed_at TEXT NOT NULL,
  grade       INTEGER NOT NULL CHECK (grade BETWEEN 1 AND 4)
);
CREATE INDEX reviews_card ON reviews(card_id, reviewed_at);

CREATE TABLE questions (
  id            INTEGER PRIMARY KEY,
  course_id     INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
  competency_id INTEGER REFERENCES competencies(id) ON DELETE SET NULL,
  kind          TEXT NOT NULL CHECK (kind IN ('mc','multi','short')),
  prompt        TEXT NOT NULL,
  choices_json  TEXT NOT NULL DEFAULT '[]',
  answer_json   TEXT NOT NULL,
  explanation   TEXT NOT NULL DEFAULT '',
  source        TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('manual','ai-accepted')),
  created_at    TEXT NOT NULL
);

CREATE TABLE quiz_attempts (
  id                INTEGER PRIMARY KEY,
  course_id         INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
  started_at        TEXT NOT NULL,
  finished_at       TEXT,
  score             INTEGER,
  total             INTEGER NOT NULL,
  time_limit_min    INTEGER,
  question_ids_json TEXT NOT NULL
);

CREATE TABLE quiz_answers (
  id          INTEGER PRIMARY KEY,
  attempt_id  INTEGER NOT NULL REFERENCES quiz_attempts(id) ON DELETE CASCADE,
  question_id INTEGER REFERENCES questions(id) ON DELETE SET NULL,
  given_json  TEXT NOT NULL,
  correct     INTEGER
);

CREATE TABLE preassessments (
  id             INTEGER PRIMARY KEY,
  course_id      INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
  taken_on       TEXT NOT NULL,
  score          REAL,
  passed         INTEGER NOT NULL DEFAULT 0,
  breakdown_json TEXT NOT NULL DEFAULT '""'
);

CREATE TABLE pa_tasks (
  id          INTEGER PRIMARY KEY,
  course_id   INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
  ord         INTEGER NOT NULL,
  rubric_text TEXT NOT NULL,
  status      TEXT NOT NULL DEFAULT 'todo' CHECK (status IN ('todo','drafting','done'))
);

CREATE TABLE pa_submissions (
  id           INTEGER PRIMARY KEY,
  course_id    INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
  version      TEXT NOT NULL,
  submitted_on TEXT NOT NULL,
  result       TEXT NOT NULL DEFAULT 'pending' CHECK (result IN ('pending','passed','revision')),
  feedback     TEXT NOT NULL DEFAULT ''
);

CREATE TABLE certs (
  id              INTEGER PRIMARY KEY,
  name            TEXT NOT NULL,
  course_id       INTEGER REFERENCES courses(id) ON DELETE SET NULL,
  voucher_status  TEXT NOT NULL DEFAULT 'none' CHECK (voucher_status IN ('none','requested','received','used','expired')),
  voucher_expires TEXT,
  exam_date       TEXT,
  result          TEXT NOT NULL DEFAULT 'pending' CHECK (result IN ('pending','passed','failed')),
  cert_id         TEXT,
  expires_on      TEXT
);

CREATE TABLE sessions (
  id         INTEGER PRIMARY KEY,
  course_id  INTEGER REFERENCES courses(id) ON DELETE SET NULL,
  started_at TEXT NOT NULL,
  ended_at   TEXT,
  minutes    REAL
);

CREATE TABLE notes_index (
  path       TEXT PRIMARY KEY,
  course_id  INTEGER REFERENCES courses(id) ON DELETE CASCADE,
  title      TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- One search index for notes, cards and questions. `ref` is a notes path or a row id.
CREATE VIRTUAL TABLE search_fts USING fts5(
  kind UNINDEXED,
  ref UNINDEXED,
  course_id UNINDEXED,
  title,
  body,
  tokenize = 'porter unicode61'
);

CREATE TRIGGER cards_ai AFTER INSERT ON cards BEGIN
  INSERT INTO search_fts(kind, ref, course_id, title, body)
  VALUES ('card', NEW.id, NEW.course_id, NEW.front, NEW.back);
END;
CREATE TRIGGER cards_au AFTER UPDATE OF front, back, course_id ON cards BEGIN
  DELETE FROM search_fts WHERE kind = 'card' AND ref = OLD.id;
  INSERT INTO search_fts(kind, ref, course_id, title, body)
  VALUES ('card', NEW.id, NEW.course_id, NEW.front, NEW.back);
END;
CREATE TRIGGER cards_ad AFTER DELETE ON cards BEGIN
  DELETE FROM search_fts WHERE kind = 'card' AND ref = OLD.id;
END;

CREATE TRIGGER questions_ai AFTER INSERT ON questions BEGIN
  INSERT INTO search_fts(kind, ref, course_id, title, body)
  VALUES ('question', NEW.id, NEW.course_id, NEW.prompt, NEW.choices_json || ' ' || NEW.explanation);
END;
CREATE TRIGGER questions_au AFTER UPDATE OF prompt, choices_json, explanation, course_id ON questions BEGIN
  DELETE FROM search_fts WHERE kind = 'question' AND ref = OLD.id;
  INSERT INTO search_fts(kind, ref, course_id, title, body)
  VALUES ('question', NEW.id, NEW.course_id, NEW.prompt, NEW.choices_json || ' ' || NEW.explanation);
END;
CREATE TRIGGER questions_ad AFTER DELETE ON questions BEGIN
  DELETE FROM search_fts WHERE kind = 'question' AND ref = OLD.id;
END;
