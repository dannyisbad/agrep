import {promises as fs} from "node:fs";
import {tmpdir} from "node:os";
import {join} from "node:path";

const SESSION_ENV = "AGREP_PI_SESSION_ID";
const PUBLICATION_DIR_ENV = "AGREP_CALLER_PUBLICATION_DIR";
const PUBLICATION_VERSION = 1;
const MESSAGE_TYPE = "agrep-postcompact";

const COMPACTION_GUIDANCE = `Required handoff content:

## Post-compact recovery
State as a gate, not a ritual: before acting, name exactly what the next action needs and check the visible summary. If the summary fully states those facts, use it and do not retrieve history. If it only names or alludes to a needed exact value, decision, error text, path, failed approach, or unread result from before this boundary, the agent's first move is \`agrep postcompact\`, bounded rather than a dump, before acting or reconstructing from memory. \`agrep recall\` is for other sessions, not this boundary.

## Retrieval anchors
For every frame change, key discovery, user correction, and commissioned result, preserve one distinctive verbatim quote of 4-10 words. Never paraphrase an anchor. If no quotable phrase exists, name the pivot and write "no anchor".

## Killed hypotheses and open contradictions
Preserve every tested and disproved hypothesis with the observation that killed it. Flag every unexplained anomaly as open.

## Commissioned, unread
List pending reviews, subagent verdicts, and background results that have not been consumed.`;

function scopedCommand(sessionId, boundaryTimestamp) {
  if (typeof sessionId === "string"
    && /^[A-Za-z0-9._:-]+$/.test(sessionId)) {
    const boundary = Date.parse(boundaryTimestamp);
    const target = Number.isSafeInteger(boundary) && boundary >= 0
      ? ` --boundary-ms ${boundary}` : "";
    return `agrep postcompact --session ${sessionId}${target}`;
  }
  return "agrep postcompact";
}

function recoveryMessage(sessionId, boundaryTimestamp) {
  const command = scopedCommand(sessionId, boundaryTimestamp);
  return `Post-compaction recovery: this session just crossed a compaction boundary. The summary is lossy, while the replaced turns remain indexed. Before acting, name exactly what the next action needs and check the visible summary. If it fully states those facts, use it and do not retrieve history. If it only names or alludes to a needed exact value, decision, error text, path, failed approach, or unread result from before this boundary, run \`${command}\` as the first tool action before acting or reconstructing from memory. This exact command is already scoped to this Pi/OMP session; do not inspect the environment or substitute \`agrep recall\`. Follow only the \`--more\` continuation it prints if the needed evidence is omitted.`;
}

function resumedAtUnrecoveredCompaction(entries, sessionId) {
  const command = scopedCommand(sessionId);
  for (let index = entries.length - 1; index >= 0; index -= 1) {
    const entry = entries[index];
    if (entry.type === "custom_message"
      && entry.customType === MESSAGE_TYPE
      && typeof entry.content === "string"
      && entry.content.includes(command)) {
      return false;
    }
    if (entry.type === "compaction") {
      return entry;
    }
  }
  return false;
}

// Tool shells inherit a pre-session snapshot of this env, so the identity is
// also published per pid; agrep walks its parent chain (docs/COORDINATION.md).
function publicationFile(env) {
  let dir = (env[PUBLICATION_DIR_ENV] || "").trim();
  if (!dir) {
    const uid = typeof process.getuid === "function" ? process.getuid() : "user";
    // /tmp, not TMPDIR: the sandboxed tool shell must resolve the same path
    const root = process.platform === "win32" ? tmpdir() : "/tmp";
    dir = join(root, `agrep-caller-v${PUBLICATION_VERSION}-${uid}`);
  }
  return {dir, file: join(dir, `${process.pid}.json`)};
}

async function processStartIdentity() {
  // agrep's Linux birth identity is proc_<start ticks>; elsewhere it checks
  // the record's timestamp against the live process birth time instead
  if (process.platform !== "linux") return undefined;
  try {
    const raw = await fs.readFile("/proc/self/stat", "ascii");
    const fields = raw.slice(raw.lastIndexOf(")") + 2).split(/\s+/);
    return fields[19] ? `proc_${fields[19]}` : undefined;
  } catch {
    return undefined;
  }
}

// one record per process, shared by every session hosted here (root,
// advisor, subagents) however many extension instances the loader creates
const PROCESS_STATE = "__agrepCallerPublication";
globalThis[PROCESS_STATE] ??= {sessions: new Set(), queue: Promise.resolve()};

export default function agrepPostcompact(pi) {
  let ownedSessionId;
  const shared = globalThis[PROCESS_STATE];
  const publishedSessions = shared.sessions;

  function publishIdentity(ctx) {
    const sessionId = ctx.sessionManager.getSessionId();
    if (typeof sessionId !== "string" || !sessionId.trim()) {
      delete process.env[SESSION_ENV];
      if (ownedSessionId) publishedSessions.delete(ownedSessionId);
      ownedSessionId = undefined;
      return;
    }
    ownedSessionId = sessionId.trim();
    process.env[SESSION_ENV] = ownedSessionId;
    publishedSessions.add(ownedSessionId);
  }

  function writePublication() {
    // serialized: several in-process sessions can start back to back
    shared.queue = shared.queue.then(async () => {
      try {
        const {dir, file} = publicationFile(process.env);
        if (publishedSessions.size === 0) {
          await fs.rm(file, {force: true});
          return;
        }
        await fs.mkdir(dir, {recursive: true, mode: 0o700});
        // /tmp is shared: a pre-created directory owned by someone else must
        // never receive session ids (mkdir above is a silent no-op then)
        const info = await fs.lstat(dir);
        const uid = typeof process.getuid === "function" ? process.getuid() : undefined;
        if (!info.isDirectory() || (uid !== undefined && info.uid !== uid)) return;
        if (uid !== undefined && (info.mode & 0o777) !== 0o700) await fs.chmod(dir, 0o700);
        const record = {
          version: PUBLICATION_VERSION,
          pid: process.pid,
          sessions: [...publishedSessions],
          cwd: process.cwd(),
          updated: Date.now(),
        };
        const start = await processStartIdentity();
        if (start) record.start = start;
        const staged = `${file}.${process.pid}.tmp`;
        await fs.writeFile(staged, JSON.stringify(record), {mode: 0o600});
        await fs.rename(staged, file);
      } catch {
        // best effort: the env export still serves harnesses with a live env
      }
    });
    return shared.queue;
  }

  function queueRecovery(deliverAs, compaction) {
    if (!ownedSessionId) return;
    pi.sendMessage(
      {
        customType: MESSAGE_TYPE,
        content: recoveryMessage(ownedSessionId, compaction?.timestamp),
        display: false,
      },
      {deliverAs},
    );
  }

  pi.on("session_start", async (_event, ctx) => {
    publishIdentity(ctx);
    await writePublication();
    const compaction = resumedAtUnrecoveredCompaction(
      ctx.sessionManager.getBranch(), ownedSessionId);
    if (compaction) {
      queueRecovery("nextTurn", compaction);
    }
  });

  pi.on("session_switch", async (_event, ctx) => {
    const previous = ownedSessionId;
    publishIdentity(ctx);
    if (previous && previous !== ownedSessionId) publishedSessions.delete(previous);
    await writePublication();
    const compaction = resumedAtUnrecoveredCompaction(
      ctx.sessionManager.getBranch(), ownedSessionId);
    if (compaction) {
      queueRecovery("nextTurn", compaction);
    }
  });

  pi.on("session.compacting", () => ({context: [COMPACTION_GUIDANCE]}));

  pi.on("session_compact", async (event, ctx) => {
    publishIdentity(ctx);
    await writePublication();
    queueRecovery(event.willRetry ? "steer" : "nextTurn", event.compactionEntry);
  });

  pi.on("session_shutdown", async () => {
    if (ownedSessionId && process.env[SESSION_ENV] === ownedSessionId) {
      delete process.env[SESSION_ENV];
    }
    if (ownedSessionId) publishedSessions.delete(ownedSessionId);
    ownedSessionId = undefined;
    await writePublication();
  });
}
