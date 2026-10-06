-- Slice 12, step 1: the notes become the single source the course features are built from.
-- Purely additive: no existing table loses or rewrites a row. The new columns are NULL on everything that already exists.

-- A course's notes cut into sections by heading. Derived from the .md files (which stay the source of truth) and
-- rebuilt whenever a note is indexed, so it is safe to drop and recreate. `key` is a stable id for a section
-- (file + heading path); `hash` changes when its text does, which is how stale cards and questions are spotted.
CREATE TABLE note_chunks (
  id         INTEGER PRIMARY KEY,
  course_id  INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
  file       TEXT NOT NULL,              -- overview | competencies | notebook | mistakes (never pa/)
  key        TEXT NOT NULL,
  ord        INTEGER NOT NULL,
  heading    TEXT NOT NULL,              -- the section's own heading; '' = text before the first heading
  breadcrumb TEXT NOT NULL,              -- parent headings joined with ' > '
  level      INTEGER NOT NULL,
  text       TEXT NOT NULL,
  hash       TEXT NOT NULL,
  chars      INTEGER NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (course_id, file, key)
);
CREATE INDEX note_chunks_course ON note_chunks(course_id, file, ord);

-- What the app has worked out about a course from its notes (filled in by the analysis step; empty until then).
CREATE TABLE course_brain (
  course_id    INTEGER PRIMARY KEY REFERENCES courses(id) ON DELETE CASCADE,
  notes_hash   TEXT NOT NULL DEFAULT '',   -- hash of the sections the profile was built from
  profile_json TEXT NOT NULL DEFAULT '{}',
  status       TEXT NOT NULL DEFAULT 'none' CHECK (status IN ('none','fresh','stale')),
  analyzed_at  TEXT,
  updated_at   TEXT NOT NULL
);

-- Where a card or question came from: the note section (key) and what that section said (hash) at the time.
ALTER TABLE cards ADD COLUMN source_key TEXT;
ALTER TABLE cards ADD COLUMN source_hash TEXT;
ALTER TABLE questions ADD COLUMN source_key TEXT;
ALTER TABLE questions ADD COLUMN source_hash TEXT;
CREATE INDEX cards_source ON cards(course_id, source_key);
CREATE INDEX questions_source ON questions(course_id, source_key);
