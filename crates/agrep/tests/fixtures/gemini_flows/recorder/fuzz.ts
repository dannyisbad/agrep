// Random flows through gemini-cli's recorders, scored by the oracle (oracle.ts): in the work dir
// record.sh builds, `bun run fuzz.ts OUT COUNT SEED [MIX]` writes COUNT sessions per recorder mode
// under OUT/hash8888synthetic/chats plus OUT/oracle.json ({sessionId: {flow, ops, rows, events,
// parent?}}); `python score.py <agrep-rs> OUT` indexes them in a sandbox and lists the flows whose
// published rows or events differ. MIX: realistic (default), resume, esc, stress.
import * as fs from 'node:fs';
import * as path from 'node:path';
import { setIdPrefix } from './utils/fakeCrypto.ts';
import { MASKED, VERSIONS, type Context, type Content } from './session.ts';
import { Tracked } from './oracle.ts';

const OUT = process.argv[2];
const COUNT = Number(process.argv[3] ?? 50);
const SEED = Number(process.argv[4] ?? 1);
const MIX = process.argv[5] ?? 'realistic';

// xorshift32: deterministic per seed
let state = (SEED >>> 0) || 1;
function rand(): number {
  state ^= state << 13;
  state >>>= 0;
  state ^= state >>> 17;
  state ^= state << 5;
  state >>>= 0;
  return state / 0x100000000;
}
function pick<T>(items: T[]): T {
  return items[Math.floor(rand() * items.length)];
}
function chance(p: number): boolean {
  return rand() < p;
}

const WORDS = ['alpaca', 'bison', 'cheetah', 'dingo', 'elephant', 'fox', 'gnu', 'heron'];
const SMALL = ['alpaca', 'bison'];

type Weights = Record<string, number>;
const MIXES: Record<string, Weights> = {
  realistic: { plain: 30, tool: 20, esc: 3, escTrail: 1, decline: 3, declineLater: 2, abort: 4, fail: 2, mask: 8, truncate: 2, compress: 6, save: 4, chatResume: 4, resume: 5, rewind: 6, hook: 2, ide: 2, binary: 1, subagent: 2 },
  resume: { plain: 20, tool: 10, esc: 2, escTrail: 1, decline: 2, declineLater: 1, abort: 3, fail: 1, mask: 6, truncate: 1, compress: 8, save: 12, chatResume: 14, resume: 6, rewind: 10, hook: 2, ide: 1, binary: 1, subagent: 1 },
  esc: { plain: 20, tool: 10, esc: 10, escTrail: 10, decline: 4, declineLater: 3, abort: 6, fail: 3, mask: 10, truncate: 2, compress: 8, save: 4, chatResume: 4, resume: 5, rewind: 8, hook: 2, ide: 1, binary: 1, subagent: 1 },
  stress: { plain: 20, tool: 10, esc: 6, escTrail: 4, decline: 5, declineLater: 3, abort: 6, fail: 3, mask: 10, truncate: 3, compress: 10, save: 8, chatResume: 10, resume: 8, rewind: 14, hook: 4, ide: 3, binary: 2, subagent: 2 },
};
const weights = MIXES[MIX];
function pickOp(): string {
  const total = Object.values(weights).reduce((a, b) => a + b, 0);
  let roll = rand() * total;
  for (const [op, weight] of Object.entries(weights)) {
    roll -= weight;
    if (roll < 0) return op;
  }
  return 'plain';
}

/** The user turns a person can /rewind to: live user prompts (not injected context, not results). */
function rewindTargets(s: Tracked): string[] {
  return s.history
    .filter((turn) => turn.content.role === 'user')
    .filter((turn) => turn.content.parts.some((part) => typeof part.text === 'string' && /^[a-z]/.test(part.text)))
    .map((turn) => turn.id);
}

async function flow(s: Tracked, steps: number, vocabulary: string[]): Promise<string[]> {
  const ops: string[] = [];
  let saved: Content[] | undefined;
  let counter = 0;
  for (let i = 0; i < steps; i++) {
    const op = pickOp();
    const w = pick(vocabulary);
    counter += 1;
    const id = `${w}-${counter}`;
    try {
      switch (op) {
        case 'plain':
          s.prompt(w);
          s.reply(chance(0.2) ? `ok <!-- ${w} -->${w}\u200B` : `ok ${w}`);
          break;
        case 'tool': {
          s.prompt(w);
          const lead = chance(0.4) ? `I will read ${w}.` : '';
          const trail = chance(0.15) ? `Read ${w}.` : '';
          s.call(`read-${id}`, 'read_file', { file_path: `${w}.ts` }, `export const ${w} = 1;`, 'success', lead, trail);
          s.respond(`read-${id}`, 'read_file', `export const ${w} = 1;`);
          if (chance(0.3)) s.tool(`grep-${id}`, 'grep_search', { pattern: w }, `${w}.ts:1`);
          s.reply(`ok ${w}`);
          break;
        }
        case 'esc':
          s.prompt(w);
          s.escTool(`read-${id}`, 'read_file', { file_path: `${w}.ts` }, chance(0.5) ? `I will read ${w}.` : '');
          break;
        case 'escTrail':
          s.prompt(w);
          s.escTool(`read-${id}`, 'read_file', { file_path: `${w}.ts` }, chance(0.5) ? `I will read ${w}.` : '', `Reading ${w} now.`);
          break;
        case 'decline': {
          const compressed = chance(0.2) ? { goal: `${w} so far`, keep: pick([0, 2, 4]) } : undefined;
          if (compressed && s.history.length < 2) break;
          await s.declined(w, `shell-${id}`, 'run_shell_command', { command: `rm -r ${w}` }, compressed, chance(0.4) ? `I will remove ${w}.` : '');
          break;
        }
        case 'declineLater':
          s.declinedLater(
            w,
            { callId: `read-${id}`, name: 'read_file', args: { file_path: `${w}.ts` }, output: `export const ${w} = 1;` },
            { callId: `shell-${id}`, name: 'run_shell_command', args: { command: `rm -r ${w}` } },
            chance(0.5) ? `I will remove ${w}.` : '',
          );
          break;
        case 'abort':
          s.prompt(w);
          if (chance(0.4)) s.tool(`read-${id}`, 'read_file', { file_path: `${w}.ts` }, `export const ${w} = 1;`);
          s.abort();
          break;
        case 'fail':
          s.prompt(w);
          s.fail();
          break;
        case 'mask':
          if (s.history.length < 2) break;
          s.mask(MASKED);
          break;
        case 'truncate':
          if (s.history.length < 2) break;
          s.mask('Output too large. Full output saved to: /tmp/truncated-1.txt');
          break;
        case 'compress':
          if (s.history.length < 2) break;
          await s.compress(`${w} so far`, pick([0, 2, 4, 6]));
          break;
        case 'save':
          if (s.history.length < 2) break;
          saved = s.save(`s${counter}`);
          break;
        case 'chatResume':
          if (!saved) break;
          s.setHistory(saved);
          break;
        case 'resume':
          await s.resume();
          if (chance(0.5)) s.hook('resumed');
          break;
        case 'rewind': {
          const targets = rewindTargets(s);
          if (targets.length === 0) break;
          s.rewind(pick(targets));
          break;
        }
        case 'hook':
          s.hook(`ctx ${w}`);
          break;
        case 'ide':
          s.ide(`{"activeFile":"${w}.ts"}`);
          break;
        case 'binary':
          s.prompt(w);
          s.binaryTool(`read-${id}`, w);
          s.reply(`ok ${w}`);
          break;
        case 'subagent':
          s.prompt(w);
          await s.subagent(`agent-${id}`, 'codebase_investigator', `Where does ${w} live?`, (child) => {
            child.prompt(`Investigate ${w}.`);
            child.call(`done-${id}`, 'complete_task', { result: `${w} lives in zoo.ts` }, 'Task completed.', 'success', 'Found it.');
          }, `${w} lives in zoo.ts`);
          s.reply(`ok ${w}`);
          break;
      }
      ops.push(op);
    } catch (error) {
      ops.push(`${op}!`);
      // the driver refuses what upstream refuses (nothing to roll back, no live turn): skip
      if (!(error instanceof Error && /no prompt to roll back|no live turn|rewind target|nothing to compress/.test(error.message))) throw error;
    }
  }
  return ops;
}

const oracle: Record<string, unknown> = {};
let n = 0;
for (let i = 0; i < COUNT; i++) {
  for (const coalesced of [false, true]) {
    for (const [r, version] of VERSIONS.entries()) {
      const v = (coalesced ? VERSIONS.length : 0) + r;
      const code = n % 0xfff;
      const tag = `${(0x1000 + code).toString(16)}`.repeat(2);
      setIdPrefix(tag);
      const sessionId = `${tag}-${(n >> 12).toString(16).padStart(4, '0')}-4000-8000-${tag}${(v * 4096 + (i % 4096)).toString(16).padStart(4, '0')}`;
      n += 1;
      const ctx: Context = {
        promptId: sessionId,
        config: {
          getProjectRoot: () => '/work/zoo',
          storage: { getProjectTempDir: () => path.join(OUT, 'hash8888synthetic') },
          getWorkspaceContext: () => ({ getDirectories: () => ['/work/zoo'] }),
        },
        toolRegistry: { getTool: (name) => ({ displayName: name, description: `${name} tool`, isOutputMarkdown: false }) },
      };
      const day = `2026-${String(1 + (i % 12)).padStart(2, '0')}-${String(1 + ((i >> 4) % 28)).padStart(2, '0')}`;
      const session = new Tracked(version, ctx, day, coalesced);
      session.tick();
      await session.start([]);
      const vocabulary = chance(0.25) ? SMALL : WORDS;
      const steps = 3 + Math.floor(rand() * 9);
      const ops = await flow(session, steps, vocabulary);
      const flowName = `${version.name}${coalesced ? '+coalesced' : ''}/${i}`;
      const got = session.expect();
      oracle[sessionId] = { flow: flowName, ops, rows: got.rows, events: got.events };
      for (const [c, child] of session.trackedChildren.entries()) {
        const childGot = child.expect();
        oracle[child.ctx.promptId] = { flow: `${flowName}/subagent${c + 1}`, ops: [], rows: childGot.rows, events: childGot.events, parent: sessionId };
      }
    }
  }
}
fs.writeFileSync(path.join(OUT, 'oracle.json'), JSON.stringify(oracle));
console.log(`${Object.keys(oracle).length} sessions`);
