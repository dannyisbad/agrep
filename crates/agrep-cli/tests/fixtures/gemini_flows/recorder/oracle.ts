// The conversation a person had, kept beside a Session as a ledger of recorded messages and the
// rows they open: every history turn knows the messages it came from, so a rollback or /rewind
// deletes exactly what left the context, a context rewrite deletes nothing, and a copy of a
// deleted message is a new one. Independent of the adapter: used to check driver.ts's hand-written
// expectations and to score fuzzed flows.
import { responseText, Session, type Content, type Context, type Part, type Resumed, type Turn } from './session.ts';
import { randomUUID } from './utils/fakeCrypto.ts';

export type Row = { kind: 'user' | 'recap'; text: string; pieces: Msg[]; deleted: boolean };
export type Msg = {
  kind: 'prompt' | 'recap' | 'reply' | 'result' | 'ctx';
  row: Row | null;
  text: string;
  calls: string[];
  deleted: boolean;
};
export type Expect = { rows: Array<[string, string, string]>; events: string[] };

export class Tracked extends Session {
  rows: Row[] = [];
  /** The messages each history turn (by id) carries. */
  origins = new Map<string, Msg[]>();
  /** The messages each Content handed out by `contents()` carries, for setHistory. */
  carried = new WeakMap<Content, Msg[]>();
  trackedChildren: Tracked[] = [];

  private lastRow(): Row | null {
    for (let i = this.rows.length - 1; i >= 0; i--) if (!this.rows[i].deleted) return this.rows[i];
    return null;
  }

  private newRow(kind: 'user' | 'recap', text: string): Msg {
    const row: Row = { kind, text, pieces: [], deleted: false };
    this.rows.push(row);
    return { kind: kind === 'user' ? 'prompt' : 'recap', row, text, calls: [], deleted: false };
  }

  private newReply(text: string, calls: string[] = []): Msg {
    const row = this.lastRow();
    const msg: Msg = { kind: 'reply', row, text, calls, deleted: false };
    if (row) row.pieces.push(msg);
    return msg;
  }

  /** The messages turns at `from` and after carry are undone with them. */
  private undoFrom(from: number): void {
    for (const turn of this.history.slice(from)) {
      for (const msg of this.origins.get(turn.id) ?? []) this.undo(msg);
    }
  }

  private undo(msg: Msg): void {
    msg.deleted = true;
    if (msg.row && (msg.kind === 'prompt' || msg.kind === 'recap')) {
      msg.row.deleted = true;
      for (const piece of msg.row.pieces) piece.deleted = true;
    }
  }

  override async start(extra: Array<Turn | Content>, resumed?: Resumed): Promise<void> {
    await super.start(extra, resumed);
    // turns with ids kept theirs (resume, rewind); fresh ones are copies of what they carried
    for (const [i, turn] of this.history.entries()) {
      if (this.origins.has(turn.id)) continue;
      const item = extra[i - (this.history.length - extra.length)];
      const content = item && !('id' in item) ? item : undefined;
      this.adopt(turn, content);
    }
  }

  /** A re-recorded turn: it carries its origins unless one was undone, then a copy of it is new. */
  private adopt(turn: Turn, content: Content | undefined): void {
    const msgs = content ? this.carried.get(content) ?? [] : [];
    if (!content) {
      this.origins.set(turn.id, []);
      return;
    }
    const text = content.parts.map((p) => (typeof p.text === 'string' ? p.text : '')).join('');
    if (msgs.length === 0) {
      // the environment turn, the snapshot or the acknowledgement of a compression
      if (content.role === 'user' && text.includes('<state_snapshot>')) {
        this.origins.set(turn.id, [this.newRow('recap', text)]);
      } else {
        this.origins.set(turn.id, []);
      }
      return;
    }
    const out: Msg[] = [];
    for (const msg of msgs) {
      if (!msg.deleted) {
        out.push(msg);
        continue;
      }
      if (msg.kind === 'prompt' || msg.kind === 'recap') {
        const text = msg.text;
        out.push(this.newRow(msg.kind === 'prompt' ? 'user' : 'recap', text));
      } else if (msg.kind === 'reply') {
        out.push(this.newReply(responseText(msg.text)));
      } else {
        out.push({ ...msg, deleted: false, row: this.lastRow() });
      }
    }
    this.origins.set(turn.id, out);
  }

  override prompt(text: string): void {
    const before = this.history.length;
    super.prompt(text);
    const added = this.history.slice(before);
    const promptTurn = added[added.length - 1];
    for (const turn of added.slice(0, -1)) this.origins.set(turn.id, []);
    this.origins.set(promptTurn.id, [this.newRow('user', text)]);
  }

  override reply(raw: string): void {
    super.reply(raw);
    const turn = this.history[this.history.length - 1];
    this.origins.set(turn.id, [this.newReply(responseText(raw))]);
  }

  override call(callId: string, name: string, args: Record<string, unknown>, output: string, status = 'success', lead = '', trail = ''): void {
    super.call(callId, name, args, output, status, lead, trail);
    const turn = this.history[this.history.length - 1];
    this.origins.set(turn.id, [this.newReply(responseText(lead + trail), [callId])]);
  }

  override respond(callId: string, name: string, output: string): void {
    super.respond(callId, name, output);
    const turn = this.history[this.history.length - 1];
    this.origins.set(turn.id, [{ kind: 'result', row: this.lastRow(), text: '', calls: [], deleted: false }]);
  }

  override escTool(callId: string, name: string, args: Record<string, unknown>, lead = '', trail = ''): void {
    const before = this.history.length;
    super.escTool(callId, name, args, lead, trail);
    const [model, result] = this.history.slice(before);
    this.origins.set(model.id, [this.newReply(responseText(lead + trail), [callId])]);
    this.origins.set(result.id, [{ kind: 'result', row: this.lastRow(), text: '', calls: [], deleted: false }]);
  }

  override ide(json: string): void {
    super.ide(json);
    this.origins.set(this.history[this.history.length - 1].id, []);
  }

  override hook(context: string): void {
    super.hook(context);
    this.origins.set(this.history[this.history.length - 1].id, []);
  }

  override binaryTool(callId: string, word: string): void {
    const before = this.history.length;
    super.binaryTool(callId, word);
    const [model, result, ack, data] = this.history.slice(before);
    this.origins.set(model.id, [this.newReply('', [callId])]);
    for (const turn of [result, ack, data]) {
      this.origins.set(turn.id, [{ kind: 'result', row: this.lastRow(), text: '', calls: [], deleted: false }]);
    }
  }

  override rollBack(): void {
    if (this.promptStart === undefined) throw new Error('no prompt to roll back');
    this.undoFrom(this.promptStart);
    super.rollBack();
  }

  override setHistory(contents: Content[]): void {
    super.setHistory(contents);
    for (const [i, turn] of this.history.entries()) this.adopt(turn, contents[i]);
  }

  override contents(rewrite: (part: Part) => Part = (part) => part): Content[] {
    const contents = super.contents(rewrite);
    // the same coalescing, so each Content maps to the run of turns it joined
    const stripped = this.history
      .map((turn) => ({ turn, parts: turn.content.parts.filter((part) => !part.thought) }))
      .filter((item) => item.parts.length > 0);
    const runs: Turn[][] = [];
    if (this.coalesced) {
      let last: { role: string; turns: Turn[] } | undefined;
      for (const item of stripped) {
        if (last && last.role === item.turn.content.role) last.turns.push(item.turn);
        else {
          last = { role: item.turn.content.role, turns: [item.turn] };
          runs.push(last.turns);
        }
      }
    } else {
      for (const turn of this.history) runs.push([turn]);
    }
    if (runs.length !== contents.length) throw new Error(`oracle: ${runs.length} runs for ${contents.length} contents`);
    for (const [i, content] of contents.entries()) {
      this.carried.set(content, runs[i].flatMap((turn) => this.origins.get(turn.id) ?? []));
    }
    return contents;
  }

  override rewind(messageId: string): void {
    const at = this.history.findIndex((turn) => turn.id === messageId);
    if (at < 0) throw new Error('rewind target not live');
    this.undoFrom(at);
    super.rewind(messageId);
    for (const turn of this.history) if (!this.origins.has(turn.id)) this.origins.set(turn.id, []);
  }

  override async resume(): Promise<void> {
    await super.resume();
    for (const turn of this.history) if (!this.origins.has(turn.id)) this.origins.set(turn.id, []);
  }

  override async subagent(
    callId: string,
    agentName: string,
    objective: string,
    run: (child: Session) => Promise<void> | void,
    result: string,
    lead = '',
  ): Promise<Session> {
    this.tick();
    const args = { agent_name: agentName, prompt: objective };
    const id = this.recorder.recordMessage({ model: 'gemini-2.5-pro', type: 'gemini', content: responseText(lead) });
    const call = { functionCall: { id: callId, name: 'invoke_agent', args } };
    this.history.push({ id, content: { role: 'model', parts: lead ? [{ text: lead }, call] : [call] } });
    this.origins.set(id, [this.newReply(responseText(lead), [callId])]);
    const agentId = randomUUID();
    const ctx: Context = { ...this.ctx, promptId: agentId, parentSessionId: this.ctx.promptId };
    const child = new Tracked(this.version, ctx, this.day, this.coalesced, 'subagent');
    child.minute = this.minute;
    await child.start([]);
    await run(child);
    this.minute = child.minute;
    this.children.push(child);
    this.trackedChildren.push(child);
    const output = `Subagent '${agentName}' finished.\nTermination Reason: GOAL\nResult:\n${result}`;
    const response = [{ functionResponse: { id: callId, name: 'invoke_agent', response: { output } } }];
    this.recorder.recordToolCalls('gemini-2.5-pro', [
      { id: callId, name: 'invoke_agent', args, result: response, status: 'success', timestamp: new Date().toISOString(), agentId },
    ]);
    this.respond(callId, 'invoke_agent', output);
    return child;
  }

  /** What the index should show: live rows in order, each with its live replies and calls. */
  expect(): Expect {
    const rows: Array<[string, string, string]> = [];
    const events: string[] = [];
    for (const row of this.rows) {
      if (row.deleted) continue;
      const live = row.pieces.filter((piece) => !piece.deleted);
      const reply = live.map((piece) => piece.text.trim()).filter((text) => text).join(' ');
      rows.push([this.kind === 'subagent' ? 'subagent' : row.kind, row.text, reply]);
      for (const piece of live) events.push(...piece.calls);
    }
    events.sort();
    return { rows, events };
  }
}
