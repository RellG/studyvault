-- The study agent's memory: short facts about the student it carries into every chat (weak topics from a
-- pre-assessment, goals, how they like to study). The agent saves them with `remember`; the user sees, adds and
-- deletes them on /ask/memory.
CREATE TABLE agent_memory (
  id         INTEGER PRIMARY KEY,
  course_id  INTEGER REFERENCES courses(id) ON DELETE CASCADE,   -- NULL = about the student in general
  text       TEXT NOT NULL,
  thread_id  INTEGER REFERENCES agent_threads(id) ON DELETE SET NULL,  -- the chat it came from; NULL = added by hand
  created_at TEXT NOT NULL
);
