const SESSION_ENV = "AGREP_PI_SESSION_ID";
const MESSAGE_TYPE = "agrep-postcompact";

const COMPACTION_GUIDANCE = `Required handoff content:

## Post-compact recovery
State as an instruction, not an option: this summary is lossy, but the conversation it replaces remains indexed. Whenever resumed work needs an exact value, decision, error text, path, or result from before this boundary, the agent's first move is \`agrep postcompact\`, bounded rather than a dump, before acting or reconstructing from memory. \`agrep recall\` is for other sessions, not this boundary.

## Retrieval anchors
For every frame change, key discovery, user correction, and commissioned result, preserve one distinctive verbatim quote of 4-10 words. Never paraphrase an anchor. If no quotable phrase exists, name the pivot and write "no anchor".

## Killed hypotheses and open contradictions
Preserve every tested and disproved hypothesis with the observation that killed it. Flag every unexplained anomaly as open.

## Commissioned, unread
List pending reviews, subagent verdicts, and background results that have not been consumed.`;

function scopedCommand(sessionId) {
  if (typeof sessionId === "string"
    && /^[A-Za-z0-9._:-]+$/.test(sessionId)) {
    return `agrep postcompact --session ${sessionId}`;
  }
  return "agrep postcompact";
}

function recoveryMessage(sessionId) {
  const command = scopedCommand(sessionId);
  return `Post-compaction recovery: this session just crossed a compaction boundary. The summary is lossy, while the replaced turns remain indexed. If the task needs any exact pre-boundary value, decision, error text, path, or result, run \`${command}\` as the first tool action before acting or reconstructing from memory. This exact command is already scoped to this Pi/OMP session; do not inspect the environment or substitute \`agrep recall\`. Follow only the \`--more\` continuation it prints if the needed evidence is omitted.`;
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
      return true;
    }
  }
  return false;
}

export default function agrepPostcompact(pi) {
  let ownedSessionId;

  function publishIdentity(ctx) {
    const sessionId = ctx.sessionManager.getSessionId();
    if (typeof sessionId !== "string" || !sessionId.trim()) {
      delete process.env[SESSION_ENV];
      ownedSessionId = undefined;
      return;
    }
    ownedSessionId = sessionId.trim();
    process.env[SESSION_ENV] = ownedSessionId;
  }

  function queueRecovery(deliverAs) {
    if (!ownedSessionId) return;
    pi.sendMessage(
      {
        customType: MESSAGE_TYPE,
        content: recoveryMessage(ownedSessionId),
        display: false,
      },
      {deliverAs},
    );
  }

  pi.on("session_start", (_event, ctx) => {
    publishIdentity(ctx);
    if (resumedAtUnrecoveredCompaction(
      ctx.sessionManager.getBranch(), ownedSessionId)) {
      queueRecovery("nextTurn");
    }
  });

  pi.on("session_switch", (_event, ctx) => {
    publishIdentity(ctx);
    if (resumedAtUnrecoveredCompaction(
      ctx.sessionManager.getBranch(), ownedSessionId)) {
      queueRecovery("nextTurn");
    }
  });

  pi.on("session.compacting", () => ({context: [COMPACTION_GUIDANCE]}));

  pi.on("session_compact", (event, ctx) => {
    publishIdentity(ctx);
    queueRecovery(event.willRetry ? "steer" : "nextTurn");
  });

  pi.on("session_shutdown", () => {
    if (ownedSessionId && process.env[SESSION_ENV] === ownedSessionId) {
      delete process.env[SESSION_ENV];
    }
    ownedSessionId = undefined;
  });
}
