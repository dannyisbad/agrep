-- Synthetic opencode store. The harness builds home/.local/share/opencode/opencode.db
-- from this at test time (no binary committed; this file is the auditable source).
-- Schema mirrors the columns the adapter reads (see ingest/opencode.rs).
CREATE TABLE session(id TEXT PRIMARY KEY, directory TEXT, parent_id TEXT, title TEXT, time_created INTEGER);
CREATE TABLE message(id TEXT PRIMARY KEY, session_id TEXT, data TEXT, time_created INTEGER);
CREATE TABLE part(id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, data TEXT, time_created INTEGER);

INSERT INTO session(id, directory, parent_id, title, time_created) VALUES
  ('sess-oc-1', '/work/epsilon', NULL, NULL, 1767348000000),
  ('sess-oc-child', '/work/epsilon', 'sess-oc-1', 'spec check', 1767348030000);

INSERT INTO message(id, session_id, data, time_created) VALUES
  ('m1', 'sess-oc-1', '{"role":"user"}', 1767348000000),
  ('m2', 'sess-oc-1', '{"role":"assistant","modelID":"claude-fable-5"}', 1767348001000),
  ('m3', 'sess-oc-child', '{"role":"user"}', 1767348030000),
  ('m4', 'sess-oc-child', '{"role":"assistant","modelID":"claude-fable-5"}', 1767348031000);

-- p1 real typed text; p2 injected tool-narration (dropped); p3 assistant reply; p4 tool event.
INSERT INTO part(id, message_id, session_id, data, time_created) VALUES
  ('p1', 'm1', 'sess-oc-1', '{"type":"text","text":"convert config to yaml"}', 1767348000000),
  ('p2', 'm1', 'sess-oc-1', '{"type":"text","text":"Called the read tool with the following input: {}"}', 1767348000001),
  ('p3', 'm2', 'sess-oc-1', '{"type":"text","text":"Converted config.json to config.yaml."}', 1767348001000),
  ('p4', 'm2', 'sess-oc-1', '{"type":"tool","tool":"bash","callID":"c1","state":{"status":"completed","input":{"command":"cat config.json"},"output":"config as text"}}', 1767348001500),
  ('p5', 'm3', 'sess-oc-child', '{"type":"text","text":"check the generated yaml schema"}', 1767348030000),
  ('p6', 'm4', 'sess-oc-child', '{"type":"text","text":"The generated yaml matches the schema."}', 1767348031000);
