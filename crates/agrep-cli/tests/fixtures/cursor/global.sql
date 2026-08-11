-- Synthetic cursor globalStorage store (hand-written fixture, no real data).
-- Mirrors the real key layout: composerData:<uuid> sessions, bubbleId:<cid>:<uuid>
-- messages, plus the agentKv/NULL-row noise a real store carries.
CREATE TABLE ItemTable (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB);
CREATE TABLE cursorDiskKV (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB);

-- session 1: two turns, a completed and a failed tool call; mapped to a workspace
INSERT INTO cursorDiskKV VALUES ('composerData:c1a11111-1111-4111-8111-111111111111',
'{"composerId":"c1a11111-1111-4111-8111-111111111111","name":"Fix the flaky test","createdAt":1772366400000,"lastUpdatedAt":1772366400000,"unifiedMode":"agent","modelConfig":{"modelName":"claude-4.6-sonnet","maxMode":false},"fullConversationHeadersOnly":[{"bubbleId":"b1b11111-1111-4111-8111-111111111111","type":1},{"bubbleId":"b2b22222-2222-4222-8222-222222222222","type":2},{"bubbleId":"b3b33333-3333-4333-8333-333333333333","type":2},{"bubbleId":"b4b44444-4444-4444-8444-444444444444","type":1},{"bubbleId":"b5b55555-5555-4555-8555-555555555555","type":1},{"bubbleId":"b6b66666-6666-4666-8666-666666666666","type":2}]}');

INSERT INTO cursorDiskKV VALUES ('bubbleId:c1a11111-1111-4111-8111-111111111111:b1b11111-1111-4111-8111-111111111111',
'{"bubbleId":"b1b11111-1111-4111-8111-111111111111","type":1,"text":"the login test fails every third run, figure out why","createdAt":"2026-03-01T12:00:00.000Z"}');

INSERT INTO cursorDiskKV VALUES ('bubbleId:c1a11111-1111-4111-8111-111111111111:b2b22222-2222-4222-8222-222222222222',
'{"bubbleId":"b2b22222-2222-4222-8222-222222222222","type":2,"text":"","createdAt":"2026-03-01T12:00:05.000Z","toolFormerData":{"toolCallId":"t0000001-aaaa-4aaa-8aaa-000000000001","name":"grep_search","status":"completed","rawArgs":"{\"query\":\"login\"}","params":"{\"query\":\"login\",\"caseSensitive\":false}","result":"{\"matches\":3,\"files\":[\"test_login.py\"]}"}}');

INSERT INTO cursorDiskKV VALUES ('bubbleId:c1a11111-1111-4111-8111-111111111111:b3b33333-3333-4333-8333-333333333333',
'{"bubbleId":"b3b33333-3333-4333-8333-333333333333","type":2,"text":"the test binds a random port and retries collide - the seed is shared across workers","createdAt":"2026-03-01T12:00:09.000Z"}');

-- empty user bubble: must not become a turn
INSERT INTO cursorDiskKV VALUES ('bubbleId:c1a11111-1111-4111-8111-111111111111:b4b44444-4444-4444-8444-444444444444',
'{"bubbleId":"b4b44444-4444-4444-8444-444444444444","type":1,"text":"","createdAt":"2026-03-01T12:01:00.000Z"}');

INSERT INTO cursorDiskKV VALUES ('bubbleId:c1a11111-1111-4111-8111-111111111111:b5b55555-5555-4555-8555-555555555555',
'{"bubbleId":"b5b55555-5555-4555-8555-555555555555","type":1,"text":"fix it and rerun the suite","createdAt":"2026-03-01T12:01:10.000Z"}');

-- a failed tool call and reply text in the SAME bubble: event ok=false, text still the reply
INSERT INTO cursorDiskKV VALUES ('bubbleId:c1a11111-1111-4111-8111-111111111111:b6b66666-6666-4666-8666-666666666666',
'{"bubbleId":"b6b66666-6666-4666-8666-666666666666","type":2,"text":"patched the port allocation to derive from the worker id","createdAt":"2026-03-01T12:01:30.000Z","toolFormerData":{"toolCallId":"t0000002-bbbb-4bbb-8bbb-000000000002","name":"run_terminal_cmd","status":"error","rawArgs":"{\"command\":\"pytest -x\"}","result":"exit 1: address already in use"}}');

-- session 2: no workspace mapping (project falls back to "cursor"), no modelConfig,
-- and a header pointing at a bubble row that does not exist (must be skipped)
INSERT INTO cursorDiskKV VALUES ('composerData:c2a22222-2222-4222-8222-222222222222',
'{"composerId":"c2a22222-2222-4222-8222-222222222222","name":"General chat","createdAt":1772366500000,"fullConversationHeadersOnly":[{"bubbleId":"deadbeef-0000-4000-8000-000000000000","type":1},{"bubbleId":"b7b77777-7777-4777-8777-777777777777","type":1}]}');

INSERT INTO cursorDiskKV VALUES ('bubbleId:c2a22222-2222-4222-8222-222222222222:b7b77777-7777-4777-8777-777777777777',
'{"bubbleId":"b7b77777-7777-4777-8777-777777777777","type":1,"text":"does cursor keep chat history locally","createdAt":"2026-03-02T09:30:00.000Z"}');

-- real stores carry composerData rows with NULL values: must be tolerated
INSERT INTO cursorDiskKV VALUES ('composerData:c3a33333-3333-4333-8333-333333333333', NULL);

-- an empty new-chat stub (headers []): emits nothing
INSERT INTO cursorDiskKV VALUES ('composerData:c4a44444-4444-4444-8444-444444444444',
'{"composerId":"c4a44444-4444-4444-8444-444444444444","name":"","createdAt":1772366600000,"fullConversationHeadersOnly":[]}');

-- agentKv blobs are the agent harness request cache, not the transcript: ignored
INSERT INTO cursorDiskKV VALUES ('agentKv:blob:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
'{"role":"user","content":"internal prompt scaffolding, never indexed"}');
