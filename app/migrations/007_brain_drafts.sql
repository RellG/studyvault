-- Slice 12, step 2: "Build from notes". The job reads a course's notes and writes a draft (competencies, cards, questions,
-- a study plan, an overview summary) that the student reviews and applies. Nothing in the draft touches the course until then.
CREATE TABLE brain_drafts (
  id           INTEGER PRIMARY KEY,
  course_id    INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
  payload_json TEXT NOT NULL,
  notes_hash   TEXT NOT NULL,              -- the sections it was built from (chunks.notes_hash)
  status       TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','applied','dismissed')),
  result       TEXT NOT NULL DEFAULT '',
  created_at   TEXT NOT NULL
);
CREATE INDEX brain_drafts_course ON brain_drafts(course_id, status);
