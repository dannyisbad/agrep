// Drives gemini-cli's own ChatRecordingService the way GeminiClient/GeminiChat do, under both the
// current recorder (fb972b2) and the `$set.messages` one released before it (361b0bb), and writes
// one session file per flow plus the transcript a person would read (expected.json). Run through
// record.sh, which fetches those recorders; each flow's `expect` is written by hand.
import * as fs from 'node:fs';
import * as path from 'node:path';
import {
  ChatRecordingService,
  loadConversationRecord,
} from './services/chatRecordingService.ts';
import {
  ChatRecordingService as PreDeltaRecordingService,
  loadConversationRecord as loadPreDeltaRecord,
} from './services/preChatRecordingService.ts';
import type { ConversationRecord, ToolCallRecord } from './services/chatRecordingTypes.ts';
import { convertSessionToClientHistory, ensureStableToolIds } from './utils/sessionUtils.ts';
import { deriveStableId } from './utils/cryptoUtils.ts';
import { randomUUID, setIdPrefix } from './utils/fakeCrypto.ts';

type Part = Record<string, unknown>;
type Content = { role: string; parts: Part[] };
type Turn = { id: string; content: Content };
type Resumed = { conversation: ConversationRecord; filePath: string };

/** The surface both recorder versions share. */
interface Recorder {
  initialize(resumed?: Resumed, kind?: 'main' | 'subagent'): Promise<void>;
  updateMessagesFromHistory(history: readonly Turn[]): void;
  recordMessage(message: { model: string | undefined; type: string; content: unknown }): string;
  recordSyntheticMessage(type: string, content: unknown): string;
  recordToolCalls(model: string, toolCalls: ToolCallRecord[]): void;
  getConversation(): ConversationRecord | null;
  getConversationFilePath(): string | null;
  rewindTo(messageId: string): ConversationRecord | null;
}
type Version = {
  name: string;
  make: (ctx: Context) => Recorder;
  load: (file: string) => Promise<ConversationRecord | null>;
};

type Context = {
  promptId: string;
  config: {
    getProjectRoot: () => string;
    storage: { getProjectTempDir: () => string };
    getWorkspaceContext: () => { getDirectories: () => string[] };
  };
  toolRegistry: {
    getTool: (name: string) => { displayName: string; description: string; isOutputMarkdown: boolean };
  };
};

// The recorder stamps records with `new Date()`; a settable clock keeps fixtures byte-stable.
const RealDate = Date;
let clock = 0;
class FakeDate extends RealDate {
  constructor(...args: ConstructorParameters<typeof Date> | []) {
    if (args.length === 0) super(clock);
    else super(...(args as ConstructorParameters<typeof Date>));
  }
  static override now(): number {
    return clock;
  }
}
globalThis.Date = FakeDate as DateConstructor;

const OUT = process.argv[2];
const MODEL = 'gemini-2.5-pro';
const ENV_ID = deriveStableId(['environment-context']);
const ENV_TEXT = '<session_context>\nThis is the Gemini CLI.\n</session_context>';
const ACK = 'Got it. Thanks for the additional context!';
const MASKED = '<tool_output_masked>\noutput hidden to save context\n</tool_output_masked>';

// Both classes expose the Recorder surface; their declared private fields differ by version.
const VERSIONS: Version[] = [
  {
    name: 'current',
    make: (ctx) => new ChatRecordingService(ctx) as unknown as Recorder,
    load: loadConversationRecord,
  },
  {
    name: 'checkpoint',
    make: (ctx) => new PreDeltaRecordingService(ctx) as unknown as Recorder,
    load: loadPreDeltaRecord,
  },
];

/** GeminiClient + GeminiChat, reduced to the calls that reach the recorder. */
class Session {
  history: Turn[] = [];
  recorder!: Recorder;
  private promptStart: number | undefined;
  private minute = 0;

  constructor(
    readonly version: Version,
    readonly ctx: Context,
    readonly day: string,
  ) {}

  tick(): string {
    this.minute += 1;
    const iso = `${this.day}T09:${String(this.minute).padStart(2, '0')}:00.000Z`;
    clock = RealDate.parse(iso);
    return iso;
  }

  /** GeminiClient.startChat: env turn first, Content turns get fresh ids, then init + sync. */
  async start(extra: Array<Turn | Content>, resumed?: Resumed): Promise<void> {
    const first = extra[0];
    const withEnv: Array<Turn | Content> =
      first && 'id' in first && first.id === ENV_ID
        ? [...extra]
        : [{ id: ENV_ID, content: { role: 'user', parts: [{ text: ENV_TEXT }] } }, ...extra];
    this.history = withEnv.map((item) => ('id' in item ? item : { id: randomUUID(), content: item }));
    ensureStableToolIds(this.history);
    this.recorder = this.version.make(this.ctx);
    await this.recorder.initialize(resumed, 'main');
    this.recorder.updateMessagesFromHistory(this.history);
    this.promptStart = undefined;
  }

  /** sendMessageStream for a typed prompt (a new prompt_id). */
  prompt(text: string): void {
    this.tick();
    this.promptStart = this.history.length;
    const parts = [{ text }];
    const id = this.recorder.recordMessage({ model: MODEL, type: 'user', content: parts });
    this.history.push({ id, content: { role: 'user', parts } });
  }

  /** A model text response: the record keeps upstream's cleaned `responseText`. */
  reply(raw: string): void {
    this.tick();
    const cleaned = raw.replace(/[\u200B-\u200D\uFEFF\u200E\u200F]/g, '').replace(/<!--[\s\S]*?-->/g, '').trim();
    const id = this.recorder.recordMessage({ model: MODEL, type: 'gemini', content: cleaned });
    this.history.push({ id, content: { role: 'model', parts: [{ text: raw }] } });
  }

  /** A model function call, its completion record, and the function response sent back. */
  tool(callId: string, name: string, args: Record<string, unknown>, output: string): void {
    this.tick();
    const id = this.recorder.recordMessage({ model: MODEL, type: 'gemini', content: '' });
    this.history.push({ id, content: { role: 'model', parts: [{ functionCall: { id: callId, name, args } }] } });
    const response = [{ functionResponse: { id: callId, name, response: { output } } }];
    this.recorder.recordToolCalls(MODEL, [
      { id: callId, name, args, result: response, status: 'success', timestamp: new Date().toISOString() },
    ]);
    const rid = this.recorder.recordSyntheticMessage('user', response);
    this.history.push({ id: rid, content: { role: 'user', parts: response } });
  }

  /** The `finally` of sendMessageStream: abort or failure rolls back to before the prompt. */
  abort(): void {
    this.tick();
    if (this.promptStart === undefined) throw new Error('no prompt to abort');
    this.history = this.history.slice(0, this.promptStart);
    this.recorder.updateMessagesFromHistory(this.history);
    this.promptStart = undefined;
  }

  /** GeminiChat.setHistory with plain Content: every turn is re-recorded, then synced. */
  setHistory(contents: Content[]): void {
    this.tick();
    this.history = contents.map((content) => ({
      id: this.recorder.recordSyntheticMessage(content.role === 'user' ? 'user' : 'gemini', content.parts),
      content,
    }));
    ensureStableToolIds(this.history);
    this.recorder.updateMessagesFromHistory(this.history);
  }

  contents(rewrite: (part: Part) => Part = (part) => part): Content[] {
    return this.history.map((turn) => ({
      role: turn.content.role,
      parts: structuredClone(turn.content.parts).map(rewrite),
    }));
  }

  /** client.tryMaskToolOutputs and CONTENT_TRUNCATED: tool outputs replaced, then setHistory. */
  mask(marker: string): void {
    this.setHistory(
      this.contents((part) =>
        typeof part.functionResponse === 'object' && part.functionResponse !== null
          ? { functionResponse: { ...part.functionResponse, response: { output: marker } } }
          : part,
      ),
    );
  }

  /** GeminiClient.tryCompressChat on COMPRESSED: snapshot + ack + kept tail, startChat(resumed). */
  async compress(goal: string, keep: number): Promise<void> {
    this.tick();
    const conversation = this.recorder.getConversation();
    const filePath = this.recorder.getConversationFilePath();
    if (!conversation || !filePath) throw new Error('nothing to compress');
    const tail = this.contents().slice(-keep);
    await this.start(
      [
        { role: 'user', parts: [{ text: `<state_snapshot>\n<overall_goal>${goal}</overall_goal>\n</state_snapshot>` }] },
        { role: 'model', parts: [{ text: ACK }] },
        ...tail,
      ],
      { conversation, filePath },
    );
  }

  /** rewindCommand: recorder.rewindTo, then client.setHistory(convertSessionToClientHistory). */
  rewind(messageId: string): void {
    this.tick();
    const conversation = this.recorder.rewindTo(messageId);
    if (!conversation) throw new Error('rewind failed');
    this.history = convertSessionToClientHistory(conversation.messages) as Turn[];
    ensureStableToolIds(this.history);
    this.recorder.updateMessagesFromHistory(this.history);
  }

  /** useSessionResume: load the file, startChat(convertSessionToClientHistory, resumed). */
  async resume(): Promise<void> {
    this.tick();
    const filePath = this.recorder.getConversationFilePath();
    if (!filePath) throw new Error('no file to resume');
    const conversation = await this.version.load(filePath);
    if (!conversation) throw new Error('resume load failed');
    await this.start(convertSessionToClientHistory(conversation.messages) as Turn[], { conversation, filePath });
  }

  /** The id of the live user message whose text starts with `text`. */
  idOf(text: string): string {
    const found = this.history.find((turn) =>
      turn.content.parts.some((part) => typeof part.text === 'string' && part.text.startsWith(text)),
    );
    if (!found) throw new Error(`no live turn for ${text}`);
    return found.id;
  }
}

type Row = [who: string, text: string, reply: string];
type Scenario = {
  name: string;
  expect: Row[];
  run: (s: Session) => Promise<void>;
};

const user = (text: string, reply = `ok ${text}`): Row => ['user', text, reply];
const recap = (goal: string): Row => ['recap', `<state_snapshot>\n<overall_goal>${goal}</overall_goal>\n</state_snapshot>`, ''];

/** A prompt answered after one tool round. */
function toolTurn(s: Session, word: string): void {
  s.prompt(word);
  s.tool(`read-${word}`, 'read_file', { file_path: `${word}.ts` }, `export const ${word} = 1;`);
  s.reply(`ok ${word}`);
}
function plainTurn(s: Session, word: string, raw = `ok ${word}`): void {
  s.prompt(word);
  s.reply(raw);
}

const SCENARIOS: Scenario[] = [
  {
    name: 'compress1',
    // the kept tail's reply carries the whitespace and zero-width characters upstream strips
    expect: [user('alpaca'), user('bison'), user('cheetah', 'ok cheetah'), recap('alpaca to cheetah'), user('dingo')],
    run: async (s) => {
      toolTurn(s, 'alpaca');
      plainTurn(s, 'bison');
      s.prompt('cheetah');
      s.tool('todo-cheetah', 'write_todos', { todos: [{ description: 'Port the cheetah loader', status: 'pending' }] }, 'Updated.');
      s.reply('ok\u200B cheetah\n');
      await s.compress('alpaca to cheetah', 4);
      plainTurn(s, 'dingo');
    },
  },
  {
    name: 'compress2',
    expect: [user('alpaca'), user('bison'), recap('alpaca and bison'), user('cheetah'), recap('bison and cheetah'), user('dingo')],
    run: async (s) => {
      plainTurn(s, 'alpaca');
      plainTurn(s, 'bison');
      await s.compress('alpaca and bison', 2);
      plainTurn(s, 'cheetah');
      await s.compress('bison and cheetah', 2);
      plainTurn(s, 'dingo');
    },
  },
  {
    name: 'mask1',
    expect: [user('alpaca'), user('bison'), user('cheetah')],
    run: async (s) => {
      toolTurn(s, 'alpaca');
      toolTurn(s, 'bison');
      s.mask(MASKED);
      plainTurn(s, 'cheetah');
    },
  },
  {
    name: 'mask2',
    expect: [user('alpaca'), user('bison'), user('cheetah')],
    run: async (s) => {
      toolTurn(s, 'alpaca');
      s.mask(MASKED);
      toolTurn(s, 'bison');
      s.mask(MASKED);
      plainTurn(s, 'cheetah');
    },
  },
  {
    name: 'truncate',
    expect: [user('alpaca'), user('bison'), user('cheetah')],
    run: async (s) => {
      toolTurn(s, 'alpaca');
      plainTurn(s, 'bison');
      s.mask('Output too large. Full output saved to: /tmp/truncated-1.txt');
      plainTurn(s, 'cheetah');
    },
  },
  {
    name: 'load_history',
    expect: [user('alpaca'), user('bison'), user('cheetah'), user('dingo'), user('elephant')],
    run: async (s) => {
      plainTurn(s, 'alpaca');
      plainTurn(s, 'bison');
      const saved = s.contents();
      plainTurn(s, 'cheetah');
      plainTurn(s, 'dingo');
      s.setHistory(saved);
      plainTurn(s, 'elephant');
    },
  },
  {
    name: 'abort_first',
    expect: [user('bison')],
    run: async (s) => {
      s.prompt('alpaca');
      s.abort();
      plainTurn(s, 'bison');
    },
  },
  {
    name: 'abort_later',
    expect: [user('alpaca'), user('cheetah')],
    run: async (s) => {
      plainTurn(s, 'alpaca');
      s.prompt('bison');
      s.tool('shell-bison', 'run_shell_command', { command: 'rm -r bison' }, 'removed');
      s.abort();
      plainTurn(s, 'cheetah');
    },
  },
  {
    name: 'fail',
    expect: [user('alpaca'), user('cheetah')],
    run: async (s) => {
      plainTurn(s, 'alpaca');
      s.prompt('bison');
      // a failed typed send rolls back to its own start, which is where the prompt began
      s.abort();
      plainTurn(s, 'cheetah');
    },
  },
  {
    name: 'rewind_env',
    expect: [user('alpaca'), user('dingo')],
    run: async (s) => {
      plainTurn(s, 'alpaca');
      plainTurn(s, 'bison');
      await s.compress('alpaca and bison', 2);
      plainTurn(s, 'cheetah');
      s.rewind(ENV_ID);
      plainTurn(s, 'dingo');
    },
  },
  {
    name: 'rewind_snapshot',
    expect: [user('alpaca'), user('bison'), user('elephant')],
    run: async (s) => {
      plainTurn(s, 'alpaca');
      plainTurn(s, 'bison');
      plainTurn(s, 'cheetah');
      await s.compress('alpaca to cheetah', 2);
      plainTurn(s, 'dingo');
      s.rewind(s.idOf('<state_snapshot>'));
      plainTurn(s, 'elephant');
    },
  },
  {
    name: 'rewind_snapshot2',
    expect: [user('alpaca'), user('bison'), recap('alpaca and bison'), user('elephant')],
    run: async (s) => {
      plainTurn(s, 'alpaca');
      plainTurn(s, 'bison');
      await s.compress('alpaca and bison', 2);
      plainTurn(s, 'cheetah');
      await s.compress('bison and cheetah', 2);
      plainTurn(s, 'dingo');
      s.rewind(s.idOf('<state_snapshot>'));
      plainTurn(s, 'elephant');
    },
  },
  {
    name: 'rewind_copy',
    expect: [user('alpaca'), recap('alpaca and bison'), user('dingo')],
    run: async (s) => {
      plainTurn(s, 'alpaca');
      plainTurn(s, 'bison');
      await s.compress('alpaca and bison', 2);
      plainTurn(s, 'cheetah');
      s.rewind(s.idOf('bison'));
      plainTurn(s, 'dingo');
    },
  },
  {
    name: 'rewind_prompt',
    expect: [user('alpaca'), user('dingo')],
    run: async (s) => {
      plainTurn(s, 'alpaca');
      plainTurn(s, 'bison');
      plainTurn(s, 'cheetah');
      s.rewind(s.idOf('bison'));
      plainTurn(s, 'dingo');
    },
  },
  {
    name: 'resume_retype',
    expect: [user('alpaca'), user('bison'), user('bison'), user('cheetah')],
    run: async (s) => {
      plainTurn(s, 'alpaca');
      plainTurn(s, 'bison');
      await s.resume();
      plainTurn(s, 'bison');
      plainTurn(s, 'cheetah');
    },
  },
];

const expected: string[] = [];
for (const [v, version] of VERSIONS.entries()) {
  for (const [n, scenario] of SCENARIOS.entries()) {
    const tag = `${(v * 16 + n).toString(16).padStart(2, '0')}`.repeat(4);
    setIdPrefix(tag);
    const sessionId = `${tag}-${v}${n.toString(16).padStart(3, '0')}-4000-8000-${tag}${tag.slice(0, 4)}`;
    const day = `2026-0${v + 4}-${String(n + 1).padStart(2, '0')}`;
    const ctx: Context = {
      promptId: sessionId,
      config: {
        getProjectRoot: () => '/work/zoo',
        storage: { getProjectTempDir: () => path.join(OUT, 'hash8888synthetic') },
        getWorkspaceContext: () => ({ getDirectories: () => ['/work/zoo'] }),
      },
      toolRegistry: {
        getTool: (name) => ({ displayName: name, description: `${name} tool`, isOutputMarkdown: false }),
      },
    };
    const session = new Session(version, ctx, day);
    session.tick();
    await session.start([]);
    await scenario.run(session);
    const rows = scenario.expect.map((row) => `  ${JSON.stringify(row)}`).join(',\n');
    expected.push(` ${JSON.stringify(sessionId)}: {"flow": "${version.name}/${scenario.name}", "rows": [\n${rows}\n ]}`);
    const file = session.recorder.getConversationFilePath();
    console.log(version.name, scenario.name, file ? path.basename(file) : '?');
  }
}
fs.writeFileSync(path.join(OUT, 'expected.json'), `{\n${expected.join(',\n')}\n}\n`);
