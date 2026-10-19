#!/bin/sh
# Rewrites ../home and ../expected.json by running gemini-cli's own ChatRecordingService (the
# current fb972b2 and the `$set.messages` 361b0bb) through every flow in driver.ts.
# Needs curl and bun; touches nothing outside a temp dir and the fixture.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
raw=https://raw.githubusercontent.com/google-gemini/gemini-cli
current=fb972b2f87fe7d5b06d37eac711490162d98de2c
checkpoint=361b0bbc58502342d66a2588f1052675c83b7674
src=packages/core/src

mkdir -p "$work/services" "$work/utils" "$work/core" "$work/config" "$work/scheduler" \
  "$work/tools" "$work/out"
for file in services/chatRecordingService.ts services/chatRecordingTypes.ts utils/partUtils.ts \
  utils/sessionUtils.ts utils/cryptoUtils.ts core/geminiRequest.ts; do
  curl -sSfL -o "$work/$file" "$raw/$current/$src/$file"
done
curl -sSfL -o "$work/services/preChatRecordingService.ts" \
  "$raw/$checkpoint/$src/services/chatRecordingService.ts"
curl -sSfL -o "$work/services/preChatRecordingTypes.ts" \
  "$raw/$checkpoint/$src/services/chatRecordingTypes.ts"

# Only the recorders' id source changes, so regenerated ids are stable.
for file in "$work/services/chatRecordingService.ts" "$work/services/preChatRecordingService.ts"; do
  sed -i.orig "s#^import { randomUUID } from 'node:crypto';#import { randomUUID } from '../utils/fakeCrypto.js';#" "$file"
done
sed -i.orig "s#from './chatRecordingTypes.js';#from './preChatRecordingTypes.js';#" \
  "$work/services/preChatRecordingService.ts"

# The recorders' other imports, reduced to what recording needs.
cat > "$work/utils/fakeCrypto.ts" <<'EOF'
let prefix = '00000000';
let counter = 0;
export function setIdPrefix(next: string): void {
  prefix = next;
  counter = 0;
}
export function randomUUID(): string {
  counter += 1;
  return `${prefix}-0000-4000-8000-${counter.toString().padStart(12, '0')}`;
}
EOF
cat > "$work/utils/paths.ts" <<'EOF'
import { createHash } from 'node:crypto';
export function getProjectHash(root: string): string {
  return createHash('sha256').update(root).digest('hex');
}
EOF
cat > "$work/utils/fileUtils.ts" <<'EOF'
export function sanitizeFilenamePart(part: string): string {
  return part.replace(/[^a-zA-Z0-9_-]/g, '_');
}
EOF
cat > "$work/utils/errors.ts" <<'EOF'
export function isNodeError(e: unknown): e is NodeJS.ErrnoException {
  return e instanceof Error && 'code' in e;
}
EOF
cat > "$work/utils/sessionOperations.ts" <<'EOF'
export async function deleteSessionArtifactsAsync(): Promise<void> {}
export async function deleteStoredSession(): Promise<void> {}
EOF
echo "export const debugLogger = { error() {}, warn() {}, log() {}, debug() {} };" \
  > "$work/utils/debugLogger.ts"
echo "export type ThoughtSummary = { subject: string; description: string };" \
  > "$work/utils/thoughtUtils.ts"
echo "export type Status = string;" > "$work/scheduler/types.ts"
echo "export type ToolResultDisplay = unknown;" > "$work/tools/tools.ts"
echo "export type AgentLoopContext = unknown;" > "$work/config/agent-loop-context.ts"
echo "export type HistoryTurn = { id: string; content: { role?: string; parts?: unknown[] } };" \
  > "$work/core/agentChatHistory.ts"

cp "$here/driver.ts" "$work/driver.ts"
(cd "$work" && bun run driver.ts "$work/out")
rm -rf "$here/../home"
mkdir -p "$here/../home/.gemini/tmp"
cp -R "$work/out/hash8888synthetic" "$here/../home/.gemini/tmp/"
cp "$work/out/expected.json" "$here/../expected.json"
