-- Synthetic crush store. The harness builds home/.local/share/crush/crush.db from this at
-- test time (no binary committed; this is the auditable source). Columns mirror the ones the
-- adapter reads from charmbracelet/crush's schema (sessions + messages).
CREATE TABLE sessions(id TEXT PRIMARY KEY, parent_session_id TEXT, title TEXT, updated_at INTEGER, created_at INTEGER);
CREATE TABLE messages(id TEXT PRIMARY KEY, session_id TEXT, role TEXT, parts TEXT, model TEXT, created_at INTEGER, updated_at INTEGER);

INSERT INTO sessions(id, parent_session_id, title, updated_at, created_at) VALUES
  ('sc1', NULL, 'readme', 1767348100000, 1767348000000),
  ('sc2', NULL, 'tests', 1767348200000, 1767348100000),
  ('sc3', 'sc1', 'verify conversion', 1767348300000, 1767348200000);

INSERT INTO messages(id, session_id, role, parts, model, created_at, updated_at) VALUES
  ('m1', 'sc1', 'user', '[{"type":"text","data":{"text":"convert the readme to asciidoc"}}]', NULL, 1767348000000, 1767348000000),
  ('m2', 'sc1', 'assistant', '[{"type":"text","data":{"text":"Converted README.md to README.adoc."}},{"type":"reasoning","data":{"thinking":"excluded reasoning"}},{"type":"tool_call","data":{"id":"tcx","name":"bash","input":"{\"command\":\"pandoc README.md -o README.adoc\"}","finished":true}},{"type":"tool_result","data":{"tool_call_id":"tcx","name":"bash","content":"wrote README.adoc","is_error":false}}]', 'gpt-5.5', 1767348001000, 1767348001000),
  ('m3', 'sc2', 'user', '[{"type":"text","data":{"text":"add a smoke test"}}]', NULL, 1767348100000, 1767348100000),
  ('m4', 'sc2', 'assistant', '[{"type":"text","data":{"text":"Added tests/smoke.rs with one assertion."}}]', 'gpt-5.5', 1767348101000, 1767348101000),
  ('m5', 'sc3', 'user', '[{"type":"text","data":{"text":"verify the asciidoc headings"}}]', NULL, 1767348200000, 1767348200000),
  ('m6', 'sc3', 'assistant', '[{"type":"text","data":{"text":"All converted headings retain their levels."}}]', 'gpt-5.5', 1767348201000, 1767348201000);
