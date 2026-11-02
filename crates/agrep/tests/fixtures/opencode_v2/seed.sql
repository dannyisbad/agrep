-- Synthetic opencode 2.x store. The harness builds home/.local/share/opencode/opencode.db
-- from this at test time (no binary committed; this file is the auditable source).
-- Schema mirrors the columns the v2 adapter reads (see ingest/opencode.rs): messages
-- carry their content parts inline, and the session tables are session_v2/session_message.
CREATE TABLE session_v2(id TEXT PRIMARY KEY, parent_id TEXT, directory TEXT NOT NULL, title TEXT, time_created INTEGER NOT NULL);
CREATE TABLE session_message(id TEXT PRIMARY KEY, session_id TEXT NOT NULL, type TEXT NOT NULL, seq INTEGER NOT NULL, time_created INTEGER NOT NULL, data TEXT NOT NULL);

INSERT INTO session_v2(id, parent_id, directory, title, time_created) VALUES
  ('ses-oc2-1', NULL, '/work/epsilon', NULL, 1767348000000),
  ('ses-oc2-child', 'ses-oc2-1', '/work/epsilon', 'spec check', 1767348030000);

-- m1 user prompt; m2 wrapper-only user text (dropped by the shared injected-part
-- filter); m3 assistant with reasoning (skipped), reply text, and a tool part whose
-- output lives in state.content text blocks; m4/m5 the child session's turn.
INSERT INTO session_message(id, session_id, type, seq, time_created, data) VALUES
  ('m1', 'ses-oc2-1', 'user', 1, 1767348000000,
   '{"time":{"created":1767348000000},"text":"convert config to yaml","files":[],"agents":[]}'),
  ('m2', 'ses-oc2-1', 'user', 2, 1767348000500,
   '{"time":{"created":1767348000500},"text":"Called the read tool with the following input: {}","files":[],"agents":[]}'),
  ('m3', 'ses-oc2-1', 'assistant', 3, 1767348001000,
   '{"time":{"created":1767348001000,"completed":1767348002000},"agent":"build","model":{"id":"claude-fable-5","providerID":"anthropic"},"content":[{"type":"reasoning","text":"planning the conversion"},{"type":"text","text":"Converted config.json to config.yaml."},{"type":"tool","id":"c1","name":"bash","state":{"status":"completed","input":{"command":"cat config.json"},"content":[{"type":"text","text":"config as text"}],"metadata":{"exit":0}},"time":{"created":1767348001500}}]}'),
  ('m4', 'ses-oc2-child', 'user', 1, 1767348030000,
   '{"time":{"created":1767348030000},"text":"check the generated yaml schema","files":[],"agents":[]}'),
  ('m5', 'ses-oc2-child', 'assistant', 2, 1767348031000,
   '{"time":{"created":1767348031000},"agent":"build","model":{"id":"claude-fable-5","providerID":"anthropic"},"content":[{"type":"text","text":"The generated yaml matches the schema."}]}');
