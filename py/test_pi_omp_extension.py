"""Execute the shipped pi/OMP extension against their shared lifecycle API."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "py" / "hooks" / "pi_postcompact.ts"
JS_RUNTIME = shutil.which("bun") or shutil.which("node")


@unittest.skipUnless(JS_RUNTIME, "bun or node is required to execute the extension")
class PiOmpExtensionLifecycleTests(unittest.TestCase):
    def test_compaction_only_recovery_and_session_identity(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            module = root / "agrep-postcompact.mjs"
            module.write_bytes(EXTENSION.read_bytes())
            publication_dir = root / "publication"
            runner = root / "runner.mjs"
            runner.write_text(
                f"""
import {{promises as fs}} from "node:fs";
process.env.AGREP_CALLER_PUBLICATION_DIR = {json.dumps(str(publication_dir))};
const publication = {json.dumps(str(publication_dir))} + "/" + process.pid + ".json";
async function published() {{
  try {{
    return JSON.parse(await fs.readFile(publication, "utf8"));
  }} catch (error) {{
    if (error && error.code === "ENOENT") return null;
    throw error;
  }}
}}
const {{default: install}} = await import({json.dumps(module.as_uri())});
const handlers = new Map();
const sent = [];
const pi = {{
  on(event, handler) {{
    const registered = handlers.get(event) ?? [];
    registered.push(handler);
    handlers.set(event, registered);
  }},
  sendMessage(message, options) {{ sent.push({{message, options}}); }},
}};
const check = (value, message) => {{ if (!value) throw new Error(message); }};
install(pi);
check(JSON.stringify([...handlers.keys()]) === JSON.stringify([
  "session_start", "session_switch", "session.compacting",
  "session_compact", "session_shutdown",
]), "unexpected lifecycle registrations");
check(!handlers.has("context") && !handlers.has("before_agent_start"),
  "ordinary-message hook registered");

let sessionId = "";
let branch = [{{type: "compaction", summary: "stale identity trap"}}];
const ctx = {{sessionManager: {{
  getSessionId: () => sessionId,
  getBranch: () => branch,
}}}};
process.env.AGREP_PI_SESSION_ID = "inherited-stale-session";
await handlers.get("session_start")[0]({{type: "session_start"}}, ctx);
check(!("AGREP_PI_SESSION_ID" in process.env),
  "blank first session left inherited identity behind");
check(sent.length === 0, "blank session queued unscoped recovery context");

sessionId = "pi-session-one";
branch = [{{type: "message", role: "user"}}];
await handlers.get("session_start")[0]({{type: "session_start"}}, ctx);
check(process.env.AGREP_PI_SESSION_ID === sessionId, "identity not exported");
check(sent.length === 0, "fresh session received recovery context");
let record = await published();
check(record !== null, "session start did not publish the caller record");
check(record.pid === process.pid && record.version === 1,
  "publication must name this process");
check(JSON.stringify(record.sessions) === JSON.stringify([sessionId]),
  "publication must list exactly the live session");
check(record.cwd === process.cwd() && Number.isInteger(record.updated),
  "publication must carry cwd and an integer timestamp");
check(process.platform === "win32" || !((await fs.stat(publication)).mode & 0o077),
  "publication must be private to the user");

const compacting = await handlers.get("session.compacting")[0](
  {{type: "session.compacting", sessionId, messages: []}}, ctx);
check(Array.isArray(compacting.context) && compacting.context.length === 1,
  "OMP context must be one complete array item");
const guidance = compacting.context[0];
for (const phrase of ["## Post-compact recovery", "## Retrieval anchors",
  "## Killed hypotheses and open contradictions", "## Commissioned, unread",
  "check the visible summary", "do not retrieve history",
  "agrep postcompact"]) {{
  check(guidance.includes(phrase), `missing compaction guidance: ${{phrase}}`);
}}

branch = [{{type: "compaction", summary: "summary omitted the route"}}];
await handlers.get("session_start")[0]({{type: "session_start"}}, ctx);
check(sent.length === 1 && sent[0].options.deliverAs === "nextTurn",
  "compacted resume did not queue next-turn recovery");
check(sent[0].message.display === false
  && sent[0].message.customType === "agrep-postcompact",
  "recovery context must be hidden and uniquely typed");
for (const phrase of [
  `agrep postcompact --session ${{sessionId}}`,
  "first tool action",
  "check the visible summary",
  "do not retrieve history",
  "do not inspect the environment",
  "do not inspect the environment or substitute `agrep recall`",
  "--more",
]) {{
  check(sent[0].message.content.includes(phrase),
    `missing recovery instruction: ${{phrase}}`);
}}
branch = [
  {{type: "compaction", summary: "summary omitted the route"}},
  {{
    type: "custom_message",
    customType: "agrep-postcompact",
    content: sent[0].message.content,
  }},
];
sent.length = 0;
await handlers.get("session_start")[0]({{type: "session_start"}}, ctx);
check(sent.length === 0, "scoped recovery context was duplicated");

branch = [{{type: "compaction", summary: "summary omitted the route"}}];
await handlers.get("session_compact")[0]({{
  type: "session_compact", willRetry: true,
  compactionEntry: {{summary: "route omitted", timestamp: "2026-04-05T06:07:13.000Z"}},
}}, ctx);
check(sent.length === 1 && sent[0].options.deliverAs === "steer",
  "automatic retry did not receive steering recovery");
check(sent[0].message.content.includes("--boundary-ms 1775369233000"),
  "the hook omitted the compaction target while its source may still be flushing");
sent.length = 0;
await handlers.get("session_compact")[0]({{
  type: "session_compact", willRetry: false,
  compactionEntry: {{summary: "run agrep postcompact first"}},
}}, ctx);
check(sent.length === 1 && sent[0].options.deliverAs === "nextTurn",
  "summary route did not receive exact session scope");
check(sent[0].message.content.includes(
  `agrep postcompact --session ${{sessionId}}`),
  "post-compaction scope omitted its session ID");
sent.length = 0;

sessionId = "omp-session-two";
branch = [{{type: "compaction", summary: "legacy summary route"}}];
await handlers.get("session_switch")[0]({{type: "session_switch"}}, ctx);
check(process.env.AGREP_PI_SESSION_ID === sessionId,
  "session switch left stale identity");
check(JSON.stringify((await published()).sessions)
  === JSON.stringify([sessionId]),
  "session switch must replace the previous session in the publication");
check(sent.length === 1 && sent[0].message.content.includes(
  "agrep postcompact --session omp-session-two"),
  "compacted session switch did not receive exact scope");
sent.length = 0;
sessionId = "";
branch = [{{type: "message", role: "user"}}];
await handlers.get("session_switch")[0]({{type: "session_switch"}}, ctx);
check(!("AGREP_PI_SESSION_ID" in process.env),
  "missing session ID left prior identity behind");
branch = [{{type: "compaction", summary: "scope remains unavailable"}}];
await handlers.get("session_compact")[0]({{
  type: "session_compact", willRetry: false,
  compactionEntry: {{summary: "route omitted"}},
}}, ctx);
check(sent.length === 0, "blank compact queued unscoped recovery context");
sessionId = "shutdown-session";
await handlers.get("session_switch")[0]({{type: "session_switch"}}, ctx);
check(JSON.stringify((await published()).sessions)
  === JSON.stringify([sessionId]), "switch after blank did not republish");
// several sessions in one process: advisor and subagents each install the
// extension and start; the record is the union until the last one leaves
const {{default: installAgain}} = await import({json.dumps(module.as_uri())});
const second = new Map();
installAgain({{
  on(event, handler) {{ second.set(event, handler); }},
  sendMessage() {{}},
}});
const advisor = {{sessionManager: {{
  getSessionId: () => "advisor-session",
  getBranch: () => [{{type: "message", role: "user"}}],
}}}};
await second.get("session_start")({{type: "session_start"}}, advisor);
check(JSON.stringify((await published()).sessions.slice().sort())
  === JSON.stringify(["advisor-session", "shutdown-session"]),
  "second in-process session must join the publication, not replace it");
await handlers.get("session_shutdown")[0]({{type: "session_shutdown"}}, ctx);
check(process.env.AGREP_PI_SESSION_ID === "advisor-session",
  "the env var belongs to the session still live in this process");
check(JSON.stringify((await published()).sessions)
  === JSON.stringify(["advisor-session"]),
  "shutdown of one session must not remove the others");
await second.get("session_shutdown")({{type: "session_shutdown"}}, advisor);
check(!("AGREP_PI_SESSION_ID" in process.env),
  "shutdown leaked session identity");
check((await published()) === null,
  "the record must be deleted once the last session leaves");
console.log(JSON.stringify({{ok: true, events: [...handlers.keys()]}}));
""",
                encoding="utf-8",
            )
            result = subprocess.run(
                [str(JS_RUNTIME), str(runner)],
                capture_output=True,
                text=True,
                timeout=30,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["ok"])


if __name__ == "__main__":
    unittest.main()
