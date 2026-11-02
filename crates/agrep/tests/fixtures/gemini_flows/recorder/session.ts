// GeminiClient + GeminiChat reduced to the calls that reach gemini-cli's own ChatRecordingService,
// under both the current recorder (fb972b2) and the `$set.messages` one released before it
// (361b0bb), each with and without getHistory()'s coalescing. driver.ts runs the recorded flows
// through it; see record.sh, which fetches those recorders.
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
import { randomUUID } from './utils/fakeCrypto.ts';

export type Part = Record<string, unknown>;
export type Content = { role: string; parts: Part[] };
export type Turn = { id: string; content: Content };
export type Resumed = { conversation: ConversationRecord; filePath: string };

/** The surface both recorder versions share. */
export interface Recorder {
  initialize(resumed?: Resumed, kind?: 'main' | 'subagent'): Promise<void>;
  updateMessagesFromHistory(history: readonly Turn[]): void;
  recordMessage(message: { model: string | undefined; type: string; content: unknown }): string;
  recordSyntheticMessage(type: string, content: unknown): string;
  recordToolCalls(model: string, toolCalls: ToolCallRecord[]): void;
  getConversation(): ConversationRecord | null;
  getConversationFilePath(): string | null;
  rewindTo(messageId: string): ConversationRecord | null;
}
export type Version = {
  name: string;
  make: (ctx: Context) => Recorder;
  load: (file: string) => Promise<ConversationRecord | null>;
};

export type Context = {
  promptId: string;
  /** Set for a subagent's recorder, which nests its file under this id (757-773 at fb972b2). */
  parentSessionId?: string;
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

export const MODEL = 'gemini-2.5-pro';
export const ENV_ID = deriveStableId(['environment-context']);
export const ENV_TEXT = '<session_context>\nThis is the Gemini CLI.\n</session_context>';
export const ACK = 'Got it. Thanks for the additional context!';
export const INTERRUPTED = '[The previous response was interrupted before it completed.]';
export const CANCELLED = '[Operation Cancelled] Reason: User cancelled the operation.';
export const DENIED = '[Operation Cancelled] Reason: User denied execution.';
export const MASKED = '<tool_output_masked>\noutput hidden to save context\n</tool_output_masked>';
/** The codebase investigator's templated query (codebase-investigator.ts 128-130), shortened. */
export const INVESTIGATE = 'Your task is to do a deep investigation of the codebase for the following user objective:\n';

// Both classes expose the Recorder surface; their declared private fields differ by version.
export const VERSIONS: Version[] = [
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
export function coalesce(contents: Content[]): Content[] {
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

/** The reply text upstream records: zero-width characters go, then HTML comments until none is left (1552-1563). */
export function responseText(raw: string): string {
  let text = raw.replace(/[\u200B-\u200D\uFEFF\u200E\u200F]/g, '');
  let previous: string;
  do {
    previous = text;
    text = text.replace(/<!--[\s\S]*?-->/g, '');
  } while (text !== previous);
  return text.trim();
}

/** GeminiClient + GeminiChat, reduced to the calls that reach the recorder. */
export class Session {
  history: Turn[] = [];
  recorder!: Recorder;
  protected promptStart: number | undefined;
  minute = 0;
  /** The file as an index saw it while the session was under way (see `midway`). */
  seen: { size: number; body: string } | undefined;
  /** The subagent sessions this one invoked, in order (see `subagent`). */
  children: Session[] = [];

  constructor(
    readonly version: Version,
    readonly ctx: Context,
    readonly day: string,
    // getHistoryTurns (geminiChat.ts 1179-1194) coalesces for Gemini 2, Gemini 3 and custom
    // models, the default `auto` included, and contextManagement's scrubHistory always does
    readonly coalesced: boolean,
    readonly kind: 'main' | 'subagent' = 'main',
  ) {}

  tick(): string {
    this.minute += 1;
    const iso = `${this.day}T09:${String(this.minute).padStart(2, '0')}:00.000Z`;
    clock = RealDate.parse(iso);
    return iso;
  }

  /**
   * GeminiClient.startChat: env turn first, Content turns get fresh ids, then init + sync. A
   * subagent's chat starts from its definition's initial messages alone (local-executor.ts
   * 1104-1123): no environment turn.
   */
  async start(extra: Array<Turn | Content>, resumed?: Resumed): Promise<void> {
    const first = extra[0];
    const withEnv: Array<Turn | Content> =
      this.kind === 'subagent' || (first && 'id' in first && first.id === ENV_ID)
        ? [...extra]
        : [{ id: ENV_ID, content: { role: 'user', parts: [{ text: ENV_TEXT }] } }, ...extra];
    this.history = withEnv.map((item) => ('id' in item ? item : { id: randomUUID(), content: item }));
    ensureStableToolIds(this.history);
    this.recorder = this.version.make(this.ctx);
    await this.recorder.initialize(resumed, this.kind);
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
   * A model turn's parts as the history keeps them: text streamed before the call, the call, and
   * text streamed after it (consolidatedParts, geminiChat.ts 1520-1546); the record keeps every
   * text part joined and cleaned (1548-1563).
   */
  private modelTurn(call: Part, lead: string, trail: string): { id: string; parts: Part[] } {
    const id = this.recorder.recordMessage({ model: MODEL, type: 'gemini', content: responseText(lead + trail) });
    const parts: Part[] = [];
    if (lead) parts.push({ text: lead });
    parts.push(call);
    if (trail) parts.push({ text: trail });
    return { id, parts };
  }

  /**
   * A model function call and its completion record (recordCompletedToolCalls), which merges into
   * the model's record (chatRecordingService.ts 1133-1157) beside any text streamed around the call.
   */
  call(callId: string, name: string, args: Record<string, unknown>, output: string, status = 'success', lead = '', trail = ''): void {
    this.tick();
    const { id, parts } = this.modelTurn({ functionCall: { id: callId, name, args } }, lead, trail);
    this.history.push({ id, content: { role: 'model', parts } });
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
   * An invoke_agent call (agent-tool.ts 43-80 at fb972b2) that runs a subagent to completion:
   * its chat records under chats/<this session id>/<agent id>.jsonl (chatRecordingService.ts
   * 757-799, kind 'subagent', the workspace directories in its metadata) while the call runs,
   * then the parent's completion record names the agent id (geminiChat.ts recordCompletedToolCalls,
   * `agentId` from the tool's result data; local-invocation.ts 316-323) and carries the result the
   * tool returned. The subagent's final complete_task response is never sent back (local-executor
   * 405-412), so its file ends on that call's record.
   */
  async subagent(
    callId: string,
    agentName: string,
    objective: string,
    run: (child: Session) => Promise<void> | void,
    result: string,
    lead = '',
  ): Promise<Session> {
    this.tick();
    const args = { agent_name: agentName, prompt: objective };
    const { id, parts } = this.modelTurn({ functionCall: { id: callId, name: 'invoke_agent', args } }, lead, '');
    this.history.push({ id, content: { role: 'model', parts } });
    const agentId = randomUUID();
    const ctx: Context = { ...this.ctx, promptId: agentId, parentSessionId: this.ctx.promptId };
    const child = new Session(this.version, ctx, this.day, this.coalesced, 'subagent');
    child.minute = this.minute;
    await child.start([]);
    await run(child);
    this.minute = child.minute;
    this.children.push(child);
    const output = `Subagent '${agentName}' finished.\nTermination Reason: GOAL\nResult:\n${result}`;
    const response = [{ functionResponse: { id: callId, name: 'invoke_agent', response: { output } } }];
    this.recorder.recordToolCalls(MODEL, [
      { id: callId, name: 'invoke_agent', args, result: response, status: 'success', timestamp: new Date().toISOString(), agentId },
    ]);
    this.respond(callId, 'invoke_agent', output);
    return child;
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
   * around the call stays in the first record, cleaned and joined, and in the history's turn, raw.
   */
  escTool(callId: string, name: string, args: Record<string, unknown>, lead = '', trail = ''): void {
    this.tick();
    const { id, parts } = this.modelTurn({ functionCall: { id: callId, name, args } }, lead, trail);
    this.history.push({ id, content: { role: 'model', parts } });
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
