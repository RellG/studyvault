-- Slice 11: study agent conversations, and the changes it proposes (applied only when the user presses Apply).
CREATE TABLE agent_threads (
  id         INTEGER PRIMARY KEY,
  title      TEXT NOT NULL,
  course_id  INTEGER REFERENCES courses(id) ON DELETE SET NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE agent_messages (
  id         INTEGER PRIMARY KEY,
  thread_id  INTEGER NOT NULL REFERENCES agent_threads(id) ON DELETE CASCADE,
  role       TEXT NOT NULL CHECK (role IN ('user','assistant')),
  text       TEXT NOT NULL,
  meta_json  TEXT NOT NULL DEFAULT '{}',   -- assistant: {"tools": [...], "error": "..."}
  created_at TEXT NOT NULL
);
CREATE INDEX agent_messages_thread ON agent_messages(thread_id, id);

CREATE TABLE agent_proposals (
  id           INTEGER PRIMARY KEY,
  thread_id    INTEGER NOT NULL REFERENCES agent_threads(id) ON DELETE CASCADE,
  message_id   INTEGER REFERENCES agent_messages(id) ON DELETE CASCADE,
  kind         TEXT NOT NULL,
  course_id    INTEGER REFERENCES courses(id) ON DELETE CASCADE,
  payload_json TEXT NOT NULL,
  status       TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','applied','dismissed')),
  result       TEXT NOT NULL DEFAULT '',
  link         TEXT NOT NULL DEFAULT '',
  created_at   TEXT NOT NULL
);
CREATE INDEX agent_proposals_message ON agent_proposals(message_id);
