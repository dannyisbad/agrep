-- Synthetic cursor workspaceStorage db: maps composers to this workspace (the sibling
-- workspace.json names the folder). Only session 1 is listed here; session 2 has no
-- workspace and must fall back to project "cursor".
CREATE TABLE ItemTable (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB);
CREATE TABLE cursorDiskKV (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB);

INSERT INTO ItemTable VALUES ('composer.composerData',
'{"allComposers":[{"composerId":"c1a11111-1111-4111-8111-111111111111","name":"Fix the flaky test"}]}');
