// Drives gemini-cli's own ChatRecordingService (session.ts) under both the
// current recorder (fb972b2) and the `$set.messages` one released before it (361b0bb), each with
// and without getHistory()'s coalescing, and writes one session file per flow plus the transcript
// a person would read (expected.json). Run through record.sh, which fetches those recorders; each
// flow's `expect` is written by hand.

import * as fs from 'node:fs';
import * as path from 'node:path';
import { setIdPrefix } from './utils/fakeCrypto.ts';
import { ENV_ID, INVESTIGATE, MASKED, Session, VERSIONS, type Context } from './session.ts';

const OUT = process.argv[2];

type Row = [who: string, text: string, reply: string];
type Scenario = {
  name: string;
  expect: Row[];
  /** The call ids of the tool events left, sorted, where a flow pins them. */
  events?: string[];
  /** The subagent sessions the flow invokes, in order: each one's rows and pinned events. */
  children?: Array<{ expect: Row[]; events?: string[] }>;
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
  {
    // an invoke_agent call runs a subagent, whose chat records as its own file under the parent's
    // id; the parent's call record links to it by agent id and carries the result it returned
    name: 'subagent',
    expect: [user('alpaca'), user('cheetah')],
    events: ['agent-alpaca', 'read-cheetah'],
    children: [{
      expect: [['subagent', `${INVESTIGATE}Where does alpaca live?`, 'Searching for alpaca. Found it in zoo.ts.']],
      events: ['done-alpaca', 'grep-alpaca'],
    }],
    run: async (s) => {
      s.prompt('alpaca');
      await s.subagent('agent-alpaca', 'codebase_investigator', 'Where does alpaca live?', (child) => {
        child.prompt(`${INVESTIGATE}Where does alpaca live?`);
        child.call('grep-alpaca', 'grep_search', { pattern: 'alpaca' }, 'zoo.ts:1:alpaca', 'success', 'Searching for alpaca.');
        child.respond('grep-alpaca', 'grep_search', 'zoo.ts:1:alpaca');
        child.call('done-alpaca', 'complete_task', { result: 'alpaca lives in zoo.ts' }, 'Task completed.', 'success', 'Found it in zoo.ts.');
      }, 'alpaca lives in zoo.ts');
      s.reply('ok alpaca');
      toolTurn(s, 'cheetah');
    },
  },
  {
    // two subagents in one session, one of them after a compression that copies the first's call;
    // /rewind then undoes the second prompt and its call, never the first subagent's chat
    name: 'subagent_compress_rewind',
    expect: [user('alpaca'), recap('alpaca only'), user('cheetah')],
    events: ['agent-alpaca', 'read-cheetah'],
    children: [
      { expect: [['subagent', `${INVESTIGATE}Where does alpaca live?`, 'Found it in zoo.ts.']], events: ['done-alpaca'] },
      { expect: [['subagent', `${INVESTIGATE}Where does bison live?`, 'Found it in zoo.ts.']], events: ['done-bison'] },
    ],
    run: async (s) => {
      s.prompt('alpaca');
      await s.subagent('agent-alpaca', 'codebase_investigator', 'Where does alpaca live?', (child) => {
        child.prompt(`${INVESTIGATE}Where does alpaca live?`);
        child.call('done-alpaca', 'complete_task', { result: 'alpaca lives in zoo.ts' }, 'Task completed.', 'success', 'Found it in zoo.ts.');
      }, 'alpaca lives in zoo.ts');
      s.reply('ok alpaca');
      await s.compress('alpaca only', 2);
      s.prompt('bison');
      await s.subagent('agent-bison', 'codebase_investigator', 'Where does bison live?', (child) => {
        child.prompt(`${INVESTIGATE}Where does bison live?`);
        child.call('done-bison', 'complete_task', { result: 'bison lives in zoo.ts' }, 'Task completed.', 'success', 'Found it in zoo.ts.');
      }, 'bison lives in zoo.ts');
      s.reply('ok bison');
      s.rewind(s.idOf('bison'));
      toolTurn(s, 'cheetah');
    },
  },
  {
    // Esc on a turn whose model text follows the call, then a mask copies the turn
    name: 'esc_trail_mask',
    expect: [user('alpaca'), user('bison', 'Reading bison now.'), user('cheetah'), user('dingo')],
    events: ['read-alpaca', 'read-bison'],
    run: async (s) => {
      toolTurn(s, 'alpaca');
      s.prompt('bison');
      s.escTool('read-bison', 'read_file', { file_path: 'bison.ts' }, '', 'Reading bison now.');
      plainTurn(s, 'cheetah');
      s.mask(MASKED);
      plainTurn(s, 'dingo');
    },
  },
  {
    // Esc on a turn with text on both sides of the call, then /chat resume of a save from before it
    name: 'esc_both_resume',
    expect: [user('alpaca'), user('bison', 'I will read bison.Reading bison now.'), user('cheetah'), user('dingo')],
    events: ['read-bison'],
    run: async (s) => {
      plainTurn(s, 'alpaca');
      const saved = s.save('s7');
      s.prompt('bison');
      s.escTool('read-bison', 'read_file', { file_path: 'bison.ts' }, 'I will read bison.', 'Reading bison now.');
      plainTurn(s, 'cheetah');
      s.setHistory(saved);
      plainTurn(s, 'dingo');
    },
  },
  {
    // Esc with trailing text, then compression copies the turn into the kept tail, then /rewind
    // to the Esc'd prompt undoes it with its cancelled call
    name: 'esc_trail_compress_rewind',
    expect: [user('alpaca'), recap('alpaca to cheetah'), user('dingo')],
    events: ['read-alpaca'],
    run: async (s) => {
      toolTurn(s, 'alpaca');
      s.prompt('bison');
      s.escTool('read-bison', 'read_file', { file_path: 'bison.ts' }, 'I will read bison.', 'Reading bison now.');
      plainTurn(s, 'cheetah');
      await s.compress('alpaca to cheetah', 8);
      s.rewind(s.idOf('bison'));
      plainTurn(s, 'dingo');
    },
  },
  {
    // a completed tool round with text after the call, masked and rewound past
    name: 'trail_mask_rewind',
    expect: [user('alpaca', 'Reading alpaca.Read alpaca. ok alpaca'), user('cheetah')],
    events: ['read-alpaca'],
    run: async (s) => {
      s.prompt('alpaca');
      s.call('read-alpaca', 'read_file', { file_path: 'alpaca.ts' }, 'export const alpaca = 1;', 'success', 'Reading alpaca.', 'Read alpaca.');
      s.respond('read-alpaca', 'read_file', 'export const alpaca = 1;');
      s.reply('ok alpaca');
      plainTurn(s, 'bison');
      s.mask(MASKED);
      s.rewind(s.idOf('bison'));
      plainTurn(s, 'cheetah');
    },
  },
  {
    // /rewind undoes a turn whose reply the model streamed with a comment and a zero-width
    // character; /chat resume of a save from before brings the turn back as a copy of the raw
    // history text, which reads as the cleaned reply upstream recorded
    name: 'raw_reply_chat_resume_rewound',
    expect: [user('alpaca'), user('dingo')],
    run: async (s) => {
      plainTurn(s, 'alpaca');
      plainTurn(s, 'dingo', 'ok <!-- dingo -->dingo\u200B');
      const saved = s.save('s8');
      s.rewind(s.idOf('dingo'));
      s.setHistory(saved);
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
      const entry = (id: string, name: string, expect: Row[], events?: string[], extra = ''): string => {
        const rows = expect.map((row) => `  ${JSON.stringify(row)}`).join(',\n');
        const pinned = events ? `, "events": ${JSON.stringify(events)}` : '';
        const body = rows ? `[\n${rows}\n ]` : '[]';
        return ` ${JSON.stringify(id)}: {"flow": "${name}", "rows": ${body}${pinned}${extra}}`;
      };
      const file = session.recorder.getConversationFilePath();
      const seen = session.seen;
      // a warm index of the whole file only stands for the session if the file only grew
      if (seen && !(file && fs.readFileSync(file, 'utf8').startsWith(seen.body))) throw new Error(`${flow} rewrote`);
      const midway = seen ? `, "midway": ${seen.size}` : '';
      expected.push(entry(sessionId, flow, scenario.expect, scenario.events, midway));
      const children = scenario.children ?? [];
      if (children.length !== session.children.length) throw new Error(`${flow}: ${session.children.length} children`);
      for (const [c, child] of session.children.entries()) {
        const { expect, events } = children[c];
        const parent = `, "parent": ${JSON.stringify(sessionId)}`;
        expected.push(entry(child.ctx.promptId, `${flow}/subagent${c + 1}`, expect, events, parent));
      }
      console.log(flow, file ? path.basename(file) : '?');
    }
  }
}
fs.writeFileSync(path.join(OUT, 'expected.json'), `{\n${expected.join(',\n')}\n}\n`);
