// Drives gemini-cli's own ChatRecordingService the way GeminiClient/GeminiChat do, under both the
// current recorder (fb972b2) and the `$set.messages` one released before it (361b0bb), each with
// and without getHistory()'s coalescing, and writes one session file per flow plus the transcript
// a person would read (expected.json). Run through record.sh, which fetches those recorders; each
// flow's `expect` is written by hand.
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
const INTERRUPTED = '[The previous response was interrupted before it completed.]';
const CANCELLED = '[Operation Cancelled] Reason: User cancelled the operation.';
const DENIED = '[Operation Cancelled] Reason: User denied execution.';
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

/**
 * stripThoughts then coalesceConsecutiveRoles (geminiChat.ts 1813-1881): thought parts go, with
 * turns left without parts, then one turn per run of a role, its parts concatenated.
 */
function coalesce(contents: Content[]): Content[] {
  const out: Content[] = [];
  const stripped = contents
    .map((content) => ({ role: content.role, parts: content.parts.filter((part) => !part.thought) }))
    .filter((content) => content.parts.length > 0);
  for (const content of stripped) {
    const last = out.at(-1);
    if (last && last.role === content.role) last.parts = [...last.parts, ...content.parts];
    else out.push({ role: content.role, parts: content.parts });
  }
  return out;
}

/** The reply text upstream records: zero-width characters and HTML comments go (1552-1563). */
function responseText(raw: string): string {
  return raw.replace(/[\u200B-\u200D\uFEFF\u200E\u200F]/g, '').replace(/<!--[\s\S]*?-->/g, '').trim();
}

/** GeminiClient + GeminiChat, reduced to the calls that reach the recorder. */
class Session {
  history: Turn[] = [];
  recorder!: Recorder;
  private promptStart: number | undefined;
  private minute = 0;
  /** The file as an index saw it while the session was under way (see `midway`). */
  seen: { size: number; body: string } | undefined;

  constructor(
    readonly version: Version,
    readonly ctx: Context,
    readonly day: string,
    // getHistoryTurns (geminiChat.ts 1179-1194) coalesces for Gemini 2, Gemini 3 and custom
    // models, the default `auto` included, and contextManagement's scrubHistory always does
    readonly coalesced: boolean,
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
    // closeUnansweredToolResponseTurn: a cancelled tool's response gets an unrecorded closer
    const last = this.history.at(-1);
    if (last?.content.role === 'user' && last.content.parts.some((part) => 'functionResponse' in part)) {
      this.history.push({ id: randomUUID(), content: { role: 'model', parts: [{ text: INTERRUPTED }] } });
    }
    const parts = [{ text }];
    const id = this.recorder.recordMessage({ model: MODEL, type: 'user', content: parts });
    this.history.push({ id, content: { role: 'user', parts } });
  }

  /** A model text response: the record keeps upstream's cleaned `responseText`. */
  reply(raw: string): void {
    this.tick();
    const id = this.recorder.recordMessage({ model: MODEL, type: 'gemini', content: responseText(raw) });
    this.history.push({ id, content: { role: 'model', parts: [{ text: raw }] } });
  }

  /**
   * A model function call and its completion record (recordCompletedToolCalls), which merges into
   * the model's record (chatRecordingService.ts 1133-1157) beside any text streamed before the call.
   */
  call(callId: string, name: string, args: Record<string, unknown>, output: string, status = 'success', lead = ''): void {
    this.tick();
    const id = this.recorder.recordMessage({ model: MODEL, type: 'gemini', content: responseText(lead) });
    const call = { functionCall: { id: callId, name, args } };
    this.history.push({ id, content: { role: 'model', parts: lead ? [{ text: lead }, call] : [call] } });
    const response = [{ functionResponse: { id: callId, name, response: { output } } }];
    this.recorder.recordToolCalls(MODEL, [
      { id: callId, name, args, result: response, status, timestamp: new Date().toISOString() },
    ]);
  }

  /** The function response sent back, or after Esc handed to addHistory: both record it. */
  respond(callId: string, name: string, output: string): void {
    const response = [{ functionResponse: { id: callId, name, response: { output } } }];
    const rid = this.recorder.recordSyntheticMessage('user', response);
    this.history.push({ id: rid, content: { role: 'user', parts: response } });
  }

  tool(callId: string, name: string, args: Record<string, unknown>, output: string): void {
    this.call(callId, name, args, output);
    this.respond(callId, name, output);
  }

  /**
   * useHistory().addItem: every info, warning and error item the UI shows is recorded as a session
   * message too (useHistoryManager.ts 91-124 at fb972b2), never part of the model's history.
   */
  notice(type: 'info' | 'warning' | 'error', text: string): void {
    this.recorder.recordMessage({ model: undefined, type, content: text });
  }

  /**
   * Esc while a tool runs: cancelOngoingRequest's notice (915-923) comes first, then the scheduler
   * completes the cancelled call (useToolScheduler.ts 210-222, useGeminiStream.ts 352-363) and
   * recordToolCalls, finding a notice last, starts a record of its own for it (chatRecordingService
   * 1133-1157); then the cancelled result goes in through addHistory. Text the model streamed
   * before the call stays in the first record, cleaned, and in the history's turn, raw.
   */
  escTool(callId: string, name: string, args: Record<string, unknown>, lead = ''): void {
    this.tick();
    const id = this.recorder.recordMessage({ model: MODEL, type: 'gemini', content: responseText(lead) });
    const call = { functionCall: { id: callId, name, args } };
    this.history.push({ id, content: { role: 'model', parts: lead ? [{ text: lead }, call] : [call] } });
    this.notice('info', 'Request cancelled.');
    const response = [{ functionResponse: { id: callId, name, response: { output: CANCELLED } } }];
    this.recorder.recordToolCalls(MODEL, [
      { id: callId, name, args, result: response, status: 'cancelled', timestamp: new Date().toISOString() },
    ]);
    this.respond(callId, name, CANCELLED);
  }

  /**
   * A prompt whose tool call the person declines. useGeminiStream notes getHistory().length before
   * sending (1742-1745 at fb972b2) and, with every tool cancelled, records 'Request cancelled.'
   * and sets the history back to that length when it is longer (2105-2147); coalesced, the first
   * prompt is merged into the environment turn and stays. Auto-compression in the same turn
   * (processTurn, client.ts 703) changes the length the target is measured against.
   */
  async declined(
    text: string,
    callId: string,
    name: string,
    args: Record<string, unknown>,
    compressed?: { goal: string; keep: number },
    lead = '',
  ): Promise<void> {
    const before = this.contents().length;
    if (compressed) await this.compress(compressed.goal, compressed.keep);
    this.prompt(text);
    this.call(callId, name, args, DENIED, 'cancelled', lead);
    this.notice('info', 'Request cancelled.');
    if (this.contents().length > before) this.setHistory(this.contents().slice(0, before));
  }

  /**
   * A call declined in a later round. submitQuery notes the length for every send, the tool
   * results' too (1742-1745, 2189-2195), so the rollback keeps the prompt and its first call.
   */
  declinedLater(
    text: string,
    first: { callId: string; name: string; args: Record<string, unknown>; output: string },
    declined: { callId: string; name: string; args: Record<string, unknown> },
    lead: string,
  ): void {
    this.prompt(text);
    this.call(first.callId, first.name, first.args, first.output);
    const before = this.contents().length;
    this.respond(first.callId, first.name, first.output);
    this.call(declined.callId, declined.name, declined.args, DENIED, 'cancelled', lead);
    this.notice('info', 'Request cancelled.');
    if (this.contents().length > before) this.setHistory(this.contents().slice(0, before));
  }

  /** sendMessageStream in IDE mode: the editor context goes in as its own turn (addHistory). */
  ide(json: string): void {
    const parts = [{ text: `Here is the user's editor context as a JSON object. This is for your information only.\n\`\`\`json\n${json}\n\`\`\`` }];
    const id = this.recorder.recordSyntheticMessage('user', parts);
    this.history.push({ id, content: { role: 'user', parts } });
  }

  /** The SessionStart hook, at startup or resume: addHistory of its context (AppContainer.tsx 489-507). */
  hook(context: string): void {
    const parts = [{ text: `<hook_context>${context}</hook_context>` }];
    const id = this.recorder.recordSyntheticMessage('user', parts);
    this.history.push({ id, content: { role: 'user', parts } });
  }

  /**
   * A read_file of audio answered and replied to. The tool's result carries the data under
   * __binary_injection__ (generateContentResponseUtilities.ts 96-150); sendMessageStream records
   * the result, then removes it and records a thought acknowledgement and the data as an info
   * message, which the history keeps as a user turn (geminiChat.ts 575-622).
   */
  binaryTool(callId: string, word: string): void {
    this.tick();
    const name = 'read_file';
    const args = { file_path: `${word}.mp3` };
    const id = this.recorder.recordMessage({ model: MODEL, type: 'gemini', content: '' });
    this.history.push({ id, content: { role: 'model', parts: [{ functionCall: { id: callId, name, args } }] } });
    const data = [{ inlineData: { mimeType: 'audio/mpeg', data: 'SUQzBAAAAAAA' } }];
    const output = 'Binary content (audio/mpeg) read successfully. Content will be injected for analysis in the next sequence.';
    const response: Part[] = [{ functionResponse: { id: callId, name, response: { output, __binary_injection__: data } } }];
    this.recorder.recordToolCalls(MODEL, [
      { id: callId, name, args, result: structuredClone(response), status: 'success', timestamp: new Date().toISOString() },
    ]);
    const rid = this.recorder.recordSyntheticMessage('user', response);
    delete ((response[0].functionResponse as Part).response as Part).__binary_injection__;
    this.history.push({ id: rid, content: { role: 'user', parts: response } });
    const ack = [{ text: 'Binary content received. Proceeding with analysis.', thought: true, thoughtSignature: 'skip_thought_signature_validator' }];
    const aid = this.recorder.recordSyntheticMessage('gemini', ack);
    this.history.push({ id: aid, content: { role: 'model', parts: ack } });
    const bid = this.recorder.recordSyntheticMessage('info', data);
    this.history.push({ id: bid, content: { role: 'user', parts: data } });
  }

  /** Esc while a reply streams: the notice (915-923), then sendMessageStream's rollback. */
  abort(): void {
    this.notice('info', 'Request cancelled.');
    this.rollBack();
  }

  /** A failed request: the rollback, then handleErrorEvent's error item (1217-1238). */
  fail(): void {
    this.rollBack();
    this.notice('error', '[API Error: The model is overloaded. Please try again later.]');
  }

  /** The `finally` of sendMessageStream: abort or failure rolls back to before the prompt. */
  rollBack(): void {
    this.tick();
    if (this.promptStart === undefined) throw new Error('no prompt to roll back');
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

  /** GeminiChat.getHistory(), which saves, masking and compression's kept tail start from. */
  contents(rewrite: (part: Part) => Part = (part) => part): Content[] {
    const contents = this.history.map((turn) => ({
      role: turn.content.role,
      parts: structuredClone(turn.content.parts).map(rewrite),
    }));
    return this.coalesced ? coalesce(contents) : contents;
  }

  /** /chat save: the checkpoint, and the info item its result shows (chatCommand.ts 139-149). */
  save(tag: string): Content[] {
    this.notice('info', `Conversation checkpoint saved with tag: ${tag}.`);
    return this.contents();
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
    const tail = keep > 0 ? this.contents().slice(-keep) : [];
    const before = this.recorder;
    await this.start(
      [
        { role: 'user', parts: [{ text: `<state_snapshot>\n<overall_goal>${goal}</overall_goal>\n</state_snapshot>` }] },
        { role: 'model', parts: [{ text: ACK }] },
        ...tail,
      ],
      { conversation, filePath },
    );
    // handleChatCompressionEvent's item (1370-1398) goes through the recorder addItem was bound to
    // (AppContainer.tsx 235-237), the one tryCompressChat just replaced (client.ts 1236-1250)
    before.recordMessage({ model: undefined, type: 'info', content: 'Context compressed from 74% to 21%.' });
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

  /** Marks the file as an index reads it now, for a later warm index of the whole file. */
  midway(): void {
    const file = this.recorder.getConversationFilePath();
    if (!file) throw new Error('no file yet');
    const body = fs.readFileSync(file, 'utf8');
    this.seen = { size: Buffer.byteLength(body), body };
  }
}

type Row = [who: string, text: string, reply: string];
type Scenario = {
  name: string;
  expect: Row[];
  /** The call ids of the tool events left, sorted, where a flow pins them. */
  events?: string[];
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
      const saved = s.save('s1');
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
      s.fail();
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
  {
    // Esc while a tool runs, a later prompt, then Esc while a prompt streams: the abort records
    // the unrecorded interrupted-turn closer before removing the aborted turn
    name: 'esc_tool',
    expect: [user('alpaca'), user('bison', ''), user('cheetah'), user('elephant')],
    run: async (s) => {
      plainTurn(s, 'alpaca');
      s.prompt('bison');
      s.escTool('read-bison', 'read_file', { file_path: 'bison.ts' });
      plainTurn(s, 'cheetah');
      s.prompt('dingo');
      s.tool('todo-dingo', 'write_todos', { todos: [{ description: 'Drop the prod table', status: 'pending' }] }, 'Updated.');
      s.abort();
      plainTurn(s, 'elephant');
    },
  },
  {
    name: 'esc_tool_mask',
    expect: [user('alpaca'), user('bison', ''), user('cheetah'), user('elephant')],
    run: async (s) => {
      plainTurn(s, 'alpaca');
      s.prompt('bison');
      s.escTool('read-bison', 'read_file', { file_path: 'bison.ts' });
      plainTurn(s, 'cheetah');
      s.prompt('dingo');
      s.abort();
      s.mask(MASKED);
      plainTurn(s, 'elephant');
    },
  },
  {
    name: 'esc_tool_mask_only',
    expect: [user('alpaca'), user('bison', ''), user('cheetah'), user('dingo')],
    run: async (s) => {
      plainTurn(s, 'alpaca');
      s.prompt('bison');
      s.escTool('read-bison', 'read_file', { file_path: 'bison.ts' });
      plainTurn(s, 'cheetah');
      s.mask(MASKED);
      plainTurn(s, 'dingo');
    },
  },
  {
    // /chat save, more work, /chat resume, then /rewind to the resumed prompt
    name: 'chat_resume_rewind',
    expect: [user('fix b', 'Done.'), user('cheetah')],
    run: async (s) => {
      s.prompt('fix a');
      s.tool('read-a', 'read_file', { file_path: 'a.ts' }, 'export const a = 1;');
      s.reply('Done.');
      const saved = s.save('s2');
      s.prompt('fix b');
      s.tool('read-b', 'read_file', { file_path: 'b.ts' }, 'export const b = 1;');
      s.reply('Done.');
      s.setHistory(saved);
      s.rewind(s.idOf('fix a'));
      plainTurn(s, 'cheetah');
    },
  },
  {
    // auto-compression between a tool call and its response; the reply after it files under the
    // recap, as claude's and codex's post-compaction replies do (postcompact replays it there)
    name: 'compress_mid_tool',
    expect: [user('alpaca'), user('bison', ''), [...recap('alpaca and bison').slice(0, 2), 'ok bison'] as Row, user('cheetah')],
    run: async (s) => {
      plainTurn(s, 'alpaca');
      s.prompt('bison');
      s.call('read-bison', 'read_file', { file_path: 'bison.ts' }, 'export const bison = 1;');
      await s.compress('alpaca and bison', 2);
      s.respond('read-bison', 'read_file', 'export const bison = 1;');
      s.reply('ok bison');
      plainTurn(s, 'cheetah');
    },
  },
  {
    // /chat resume of a save from before a compression copies turns that left the context
    name: 'chat_resume_after_compress',
    expect: [user('alpaca'), user('bison'), user('cheetah'), recap('cheetah only'), user('dingo')],
    run: async (s) => {
      plainTurn(s, 'alpaca');
      toolTurn(s, 'bison');
      const saved = s.save('s3');
      plainTurn(s, 'cheetah');
      await s.compress('cheetah only', 2);
      s.setHistory(saved);
      plainTurn(s, 'dingo');
    },
  },
  {
    // a save made after /rewind lacks the environment message that compression puts back
    name: 'chat_resume_after_rewind',
    expect: [user('alpaca'), user('cheetah'), recap('cheetah only'), user('dingo')],
    run: async (s) => {
      plainTurn(s, 'alpaca');
      plainTurn(s, 'bison');
      s.rewind(s.idOf('bison'));
      const saved = s.save('s4');
      plainTurn(s, 'cheetah');
      await s.compress('cheetah only', 2);
      s.setHistory(saved);
      plainTurn(s, 'dingo');
    },
  },
  {
    // coalesced, the mask re-records the environment and the first prompt as one copy
    name: 'mask_rewind_first',
    expect: [user('cheetah')],
    run: async (s) => {
      plainTurn(s, 'alpaca');
      toolTurn(s, 'bison');
      s.mask(MASKED);
      s.rewind(s.idOf('alpaca'));
      plainTurn(s, 'cheetah');
    },
  },
  {
    // --resume drops the never-recorded closer, so a cancelled tool's result and the next prompt
    // are consecutive user turns that a coalesced mask re-records as one
    name: 'esc_resume_mask_rewind',
    expect: [user('alpaca'), user('bison', ''), user('dingo')],
    run: async (s) => {
      plainTurn(s, 'alpaca');
      s.prompt('bison');
      s.escTool('read-bison', 'read_file', { file_path: 'bison.ts' });
      plainTurn(s, 'cheetah');
      await s.resume();
      s.mask(MASKED);
      s.rewind(s.idOf('cheetah'));
      plainTurn(s, 'dingo');
    },
  },
  {
    // IDE mode: each prompt follows its own editor-context turn, never a row
    name: 'ide_context_mask',
    expect: [user('alpaca'), user('bison'), user('cheetah')],
    run: async (s) => {
      s.ide('{"activeFile":"alpaca.ts"}');
      toolTurn(s, 'alpaca');
      s.ide('{"activeFile":"bison.ts"}');
      toolTurn(s, 'bison');
      s.mask(MASKED);
      s.ide('{"activeFile":"cheetah.ts"}');
      plainTurn(s, 'cheetah');
    },
  },
  {
    // each --resume re-derives the cancelled tool's result under the id the first one recorded it
    // with, so the history holds it twice while the file maps it once; the second also drops the
    // closer, and a coalesced mask re-records all three results with the next prompt
    name: 'esc_resume_twice_mask_rewind',
    expect: [user('alpaca'), user('bison', ''), user('dingo')],
    run: async (s) => {
      plainTurn(s, 'alpaca');
      s.prompt('bison');
      s.escTool('read-bison', 'read_file', { file_path: 'bison.ts' });
      await s.resume();
      plainTurn(s, 'cheetah');
      await s.resume();
      s.mask(MASKED);
      s.rewind(s.idOf('cheetah'));
      plainTurn(s, 'dingo');
    },
  },
  {
    // the first /chat resume pairs with the oldest copy of the saved turn, which /rewind then
    // undoes; the second resume brings the turn back rather than a copy of the undone one
    name: 'chat_resume_twice_around_rewind',
    expect: [user('bison'), recap('bison only'), user('cheetah'), user('alpaca'), user('dingo')],
    run: async (s) => {
      plainTurn(s, 'alpaca');
      const saved = s.save('s5');
      s.mask(MASKED);
      plainTurn(s, 'bison');
      await s.compress('bison only', 2);
      s.setHistory(saved);
      s.rewind(s.idOf('alpaca'));
      plainTurn(s, 'cheetah');
      s.setHistory(saved);
      plainTurn(s, 'dingo');
    },
  },
  {
    // /chat resume restores a prompt /rewind took away, the environment coalesced into it
    name: 'chat_resume_rewound_first',
    expect: [user('alpaca'), user('bison')],
    run: async (s) => {
      toolTurn(s, 'alpaca');
      const saved = s.save('s6');
      s.rewind(s.idOf('alpaca'));
      s.setHistory(saved);
      plainTurn(s, 'bison');
    },
  },
  {
    // coalesced, the decline re-records the environment and the declined prompt as one copy;
    // /rewind's conversion later leaves that copy out, which undoes nothing
    name: 'decline_rewind',
    expect: [user('alpaca', ''), user('cheetah')],
    run: async (s) => {
      await s.declined('alpaca', 'shell-alpaca', 'run_shell_command', { command: 'rm -r alpaca' });
      plainTurn(s, 'bison');
      s.rewind(s.idOf('bison'));
      plainTurn(s, 'cheetah');
    },
  },
  {
    name: 'decline_twice_rewind',
    expect: [user('alpaca', ''), user('bison', ''), user('dingo')],
    run: async (s) => {
      await s.declined('alpaca', 'shell-alpaca', 'run_shell_command', { command: 'rm -r alpaca' });
      await s.declined('bison', 'shell-bison', 'run_shell_command', { command: 'rm -r bison' });
      plainTurn(s, 'cheetah');
      s.rewind(s.idOf('cheetah'));
      plainTurn(s, 'dingo');
    },
  },
  {
    name: 'decline_later_resume_rewind',
    expect: [user('alpaca'), user('bison', ''), user('dingo')],
    run: async (s) => {
      plainTurn(s, 'alpaca');
      await s.declined('bison', 'shell-bison', 'run_shell_command', { command: 'rm -r bison' });
      await s.resume();
      plainTurn(s, 'cheetah');
      s.rewind(s.idOf('cheetah'));
      plainTurn(s, 'dingo');
    },
  },
  {
    // /rewind to the first prompt empties the context, so the decline's own rollback is a pure one
    // that also removes the 'Request cancelled.' notice recorded after the declined turn
    name: 'decline_after_rewind_first',
    expect: [user('bison', ''), user('cheetah')],
    run: async (s) => {
      plainTurn(s, 'alpaca');
      s.rewind(s.idOf('alpaca'));
      await s.declined('bison', 'shell-bison', 'run_shell_command', { command: 'rm -r bison' });
      plainTurn(s, 'cheetah');
    },
  },
  {
    // compression in the declined turn leaves it unrolled; an Esc'd prompt's rollback then takes
    // the notice, and the person's /rewind ends on the declined turn yet undoes it
    name: 'decline_compressed_esc_rewind',
    expect: [user('alpaca'), user('bison'), user('cheetah'), recap('alpaca to cheetah'), user('fox')],
    run: async (s) => {
      plainTurn(s, 'alpaca');
      plainTurn(s, 'bison');
      plainTurn(s, 'cheetah');
      await s.declined('dingo', 'shell-dingo', 'run_shell_command', { command: 'rm -r dingo' }, {
        goal: 'alpaca to cheetah',
        keep: 2,
      });
      s.prompt('elephant');
      s.abort();
      s.rewind(s.idOf('dingo'));
      plainTurn(s, 'fox');
    },
  },
  {
    // the binary data is an info message the history keeps as a user turn; compression copies it
    // as a user message, and /rewind to a copy of the prompt takes the whole chain back
    name: 'binary_compress_rewind',
    expect: [user('alpaca'), recap('alpaca and bison'), recap('bison and cheetah'), user('dingo')],
    run: async (s) => {
      plainTurn(s, 'alpaca');
      s.prompt('bison');
      s.binaryTool('read-bison', 'bison');
      s.reply('ok bison');
      await s.compress('alpaca and bison', 6);
      s.prompt('cheetah');
      s.binaryTool('read-cheetah', 'cheetah');
      s.reply('ok cheetah');
      await s.compress('bison and cheetah', 12);
      s.rewind(s.idOf('bison'));
      plainTurn(s, 'dingo');
    },
  },
  {
    // --resume leaves out a hook-led copy, so the replies around it become consecutive model
    // turns that a coalesced mask re-records as one
    name: 'hook_resume_merged_replies',
    expect: [user('alpaca'), user('bison'), user('cheetah')],
    run: async (s) => {
      s.hook('startup');
      plainTurn(s, 'alpaca');
      s.mask(MASKED);
      await s.resume();
      s.hook('resumed');
      plainTurn(s, 'bison');
      s.mask(MASKED);
      await s.resume();
      s.hook('resumed again');
      s.mask(MASKED);
      plainTurn(s, 'cheetah');
    },
  },
  {
    // coalesced, the decline after a resume keeps its prompt merged into the hook's turn, and the
    // next --resume leaves that copy out without undoing the prompt
    name: 'hook_resume_decline',
    expect: [user('alpaca'), user('bison', ''), user('cheetah')],
    run: async (s) => {
      s.hook('startup');
      plainTurn(s, 'alpaca');
      await s.resume();
      s.hook('resumed');
      await s.declined('bison', 'shell-bison', 'run_shell_command', { command: 'rm -r bison' });
      await s.resume();
      s.hook('resumed again');
      plainTurn(s, 'cheetah');
    },
  },
  {
    // a re-sync writes the history's raw reply back over the cleaned one upstream recorded
    name: 'raw_reply_after_abort',
    expect: [user('alpaca'), user('cheetah')],
    run: async (s) => {
      plainTurn(s, 'alpaca', 'ok <!-- note -->alpaca\u200B\n');
      s.prompt('bison');
      s.abort();
      plainTurn(s, 'cheetah');
    },
  },
  {
    // the `$set.messages` recorder skips the second resume's re-sync when the counts match, so
    // the first resume's hook stays in the file's map though the history the mask copies lacks it
    name: 'hook_resume_twice_esc_rewind',
    expect: [user('alpaca'), user('bison', ''), user('dingo')],
    run: async (s) => {
      s.hook('startup');
      plainTurn(s, 'alpaca');
      s.prompt('bison');
      s.escTool('read-bison', 'read_file', { file_path: 'bison.ts' });
      await s.resume();
      s.hook('resumed');
      await s.resume();
      s.hook('resumed again');
      plainTurn(s, 'cheetah');
      s.mask(MASKED);
      s.rewind(s.idOf('cheetah'));
      plainTurn(s, 'dingo');
    },
  },
  {
    // uncoalesced, the hook leaves the compressed history one turn short of the rollback target,
    // so the rollback takes the call but leaves the declined prompt in the model's view
    name: 'hook_decline_compressed_longer',
    expect: [user('alpaca'), user('bison'), recap('alpaca and bison'), user('cheetah', ''), user('dingo'), user('elephant')],
    run: async (s) => {
      s.hook('startup');
      plainTurn(s, 'alpaca');
      plainTurn(s, 'bison');
      await s.declined('cheetah', 'shell-cheetah', 'run_shell_command', { command: 'rm -r cheetah' }, {
        goal: 'alpaca and bison',
        keep: 2,
      });
      plainTurn(s, 'dingo');
      s.mask(MASKED);
      plainTurn(s, 'elephant');
    },
  },
  {
    // the abort's re-sync writes the Esc'd turn back with its call and raw text, over a record
    // holding the cleaned text only; the mask then copies that turn
    name: 'esc_preamble_abort_mask',
    expect: [user('alpaca'), user('bison', 'I will read bison.'), user('cheetah'), user('elephant')],
    run: async (s) => {
      plainTurn(s, 'alpaca');
      s.prompt('bison');
      s.escTool('read-bison', 'read_file', { file_path: 'bison.ts' }, 'I will read <!-- x -->bison.\u200B');
      plainTurn(s, 'cheetah');
      s.prompt('dingo');
      s.abort();
      s.mask(MASKED);
      plainTurn(s, 'elephant');
    },
  },
  {
    // the stream answering an audio read fails, which rolls nothing back (geminiChat.ts 812), so
    // coalesced, the next prompt joins the read's result and data in one copy, without the ack
    name: 'binary_fail_mask_rewind',
    expect: [user('alpaca'), user('bison', ''), user('dingo')],
    run: async (s) => {
      plainTurn(s, 'alpaca');
      s.prompt('bison');
      s.binaryTool('read-bison', 'bison');
      s.notice('error', '[API Error: The model is overloaded. Please try again later.]');
      plainTurn(s, 'cheetah');
      s.mask(MASKED);
      s.rewind(s.idOf('cheetah'));
      plainTurn(s, 'dingo');
    },
  },
  {
    // after Esc on a call alone and an abort's re-sync, the mask's copy of the turn pairs with
    // the calls' own record, and /rewind past the turn takes the cancelled call with it
    name: 'esc_abort_mask_rewind',
    expect: [user('alpaca'), user('dingo')],
    events: [],
    run: async (s) => {
      plainTurn(s, 'alpaca');
      s.prompt('bison');
      s.escTool('read-bison', 'read_file', { file_path: 'bison.ts' });
      s.prompt('cheetah');
      s.abort();
      s.mask(MASKED);
      s.rewind(s.idOf('bison'));
      plainTurn(s, 'dingo');
    },
  },
  {
    // a failed prompt's re-sync takes the Esc'd tool's calls record out of the map; /rewind to
    // the Esc'd prompt undoes it with the prompt
    name: 'esc_fail_rewind',
    expect: [user('alpaca'), user('dingo')],
    events: [],
    run: async (s) => {
      plainTurn(s, 'alpaca');
      s.prompt('bison');
      s.escTool('grep-bison', 'grep_search', { pattern: 'bison' }, 'I will search for bison.');
      s.prompt('cheetah');
      s.fail();
      s.rewind(s.idOf('bison'));
      plainTurn(s, 'dingo');
    },
  },
  {
    // a call declined in a later round keeps the prompt, its first call and the declined turn
    name: 'decline_later',
    expect: [user('alpaca'), user('bison', 'I will remove bison.'), user('cheetah')],
    events: ['read-bison', 'shell-bison'],
    run: async (s) => {
      plainTurn(s, 'alpaca');
      s.declinedLater(
        'bison',
        { callId: 'read-bison', name: 'read_file', args: { file_path: 'bison.ts' }, output: 'export const bison = 1;' },
        { callId: 'shell-bison', name: 'run_shell_command', args: { command: 'rm -r bison' } },
        'I will remove <!-- x -->bison.',
      );
      plainTurn(s, 'cheetah');
    },
  },
  {
    // the rollback leaves the declined turn out of the map, uncopied; /rewind to its prompt
    // undoes the turn's text and call with the rest of the exchange
    name: 'decline_later_rewind',
    expect: [user('alpaca'), user('cheetah')],
    events: [],
    run: async (s) => {
      plainTurn(s, 'alpaca');
      s.declinedLater(
        'bison',
        { callId: 'read-bison', name: 'read_file', args: { file_path: 'bison.ts' }, output: 'export const bison = 1;' },
        { callId: 'shell-bison', name: 'run_shell_command', args: { command: 'rm -r bison' } },
        'I will remove <!-- x -->bison.',
      );
      s.rewind(s.idOf('bison'));
      plainTurn(s, 'cheetah');
    },
  },
  {
    // uncoalesced, compression in the declined turn leaves the history one turn short of the
    // target, so the rollback keeps the prompt; coalesced, compressing it all leaves the turn
    // unrolled. Either way /rewind to the prompt undoes the declined turn's text and call
    name: 'decline_compressed_rewind',
    expect: [user('alpaca'), user('bison'), recap('alpaca and bison'), user('dingo')],
    events: [],
    run: async (s) => {
      plainTurn(s, 'alpaca');
      s.ide('{"file":"bison.ts"}');
      plainTurn(s, 'bison');
      const compressed = { goal: 'alpaca and bison', keep: s.coalesced ? 0 : 2 };
      const args = { command: 'rm -r cheetah' };
      await s.declined('cheetah', 'shell-cheetah', 'run_shell_command', args, compressed, 'I will remove cheetah.');
      s.rewind(s.idOf('cheetah'));
      plainTurn(s, 'dingo');
    },
  },
  {
    // indexed after a later round's decline, then rewound to its only prompt: nothing is left
    name: 'decline_later_rewind_all',
    expect: [],
    events: [],
    run: async (s) => {
      s.declinedLater(
        'bison',
        { callId: 'read-bison', name: 'read_file', args: { file_path: 'bison.ts' }, output: 'export const bison = 1;' },
        { callId: 'shell-bison', name: 'run_shell_command', args: { command: 'rm -r bison' } },
        'I will remove <!-- x -->bison.',
      );
      s.midway();
      s.rewind(s.idOf('bison'));
    },
  },
  {
    // indexed after an Esc'd tool and a failed prompt, then rewound to the first prompt
    name: 'esc_fail_rewind_all',
    expect: [],
    events: [],
    run: async (s) => {
      s.prompt('bison');
      s.escTool('grep-bison', 'grep_search', { pattern: 'bison' }, 'I will search for bison.');
      s.prompt('cheetah');
      s.fail();
      s.midway();
      s.rewind(s.idOf('bison'));
    },
  },
  {
    // indexed while the only prompt's reply streamed, which Esc then rolled back
    name: 'abort_only',
    expect: [],
    events: [],
    run: async (s) => {
      s.prompt('alpaca');
      s.midway();
      s.abort();
    },
  },
];

const expected: string[] = [];
for (const coalesced of [false, true]) {
  for (const [r, version] of VERSIONS.entries()) {
    const v = (coalesced ? VERSIONS.length : 0) + r;
    for (const [n, scenario] of SCENARIOS.entries()) {
      // the first sixteen flows keep the ids their fixtures were recorded with; each block of
      // sixteen after them has its own codes, and flows past the 31st day their own months
      const block = Math.floor(n / 16);
      const code = block === 0 ? v * 16 + n : 0x40 * block + v * 16 + (n % 16);
      const tag = `${code.toString(16).padStart(2, '0')}`.repeat(4);
      setIdPrefix(tag);
      const sessionId = `${tag}-${v}${n.toString(16).padStart(3, '0')}-4000-8000-${tag}${tag.slice(0, 4)}`;
      const month = String(n < 31 ? v + 4 : v + 8).padStart(2, '0');
      const day = `2026-${month}-${String(n < 31 ? n + 1 : n - 30).padStart(2, '0')}`;
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
      const session = new Session(version, ctx, day, coalesced);
      session.tick();
      await session.start([]);
      await scenario.run(session);
      const flow = `${version.name}${coalesced ? '+coalesced' : ''}/${scenario.name}`;
      const rows = scenario.expect.map((row) => `  ${JSON.stringify(row)}`).join(',\n');
      const events = scenario.events ? `, "events": ${JSON.stringify(scenario.events)}` : '';
      const file = session.recorder.getConversationFilePath();
      const seen = session.seen;
      // a warm index of the whole file only stands for the session if the file only grew
      if (seen && !(file && fs.readFileSync(file, 'utf8').startsWith(seen.body))) throw new Error(`${flow} rewrote`);
      const midway = seen ? `, "midway": ${seen.size}` : '';
      const body = rows ? `[\n${rows}\n ]` : '[]';
      expected.push(` ${JSON.stringify(sessionId)}: {"flow": "${flow}", "rows": ${body}${events}${midway}}`);
      console.log(flow, file ? path.basename(file) : '?');
    }
  }
}
fs.writeFileSync(path.join(OUT, 'expected.json'), `{\n${expected.join(',\n')}\n}\n`);
