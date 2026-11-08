//! Gemini CLI adapter: ~/.gemini/tmp/<project>/chats/session-*.jsonl (and legacy .json), plus
//! each subagent's chat at chats/<parent session id>/<agent id>.jsonl (a side session of its
//! parent; the parent's invoke_agent call record names the agent id)
//!
//! Shapes follow google-gemini/gemini-cli packages/core/src/services/chatRecordingService.ts
//! (fb972b2). Since #23749 a session is JSONL: a metadata line `{ sessionId, projectHash,
//! startTime, ... }`, then whole message records - re-recording a message (tool calls, tokens)
//! appends it again under the same `id`; the last write wins at its first position - plus
//! `$set` (metadata; until 2026-10 also whole-history `messages` checkpoints), `$rewindTo` (drop
//! that message and every later one; an unknown id drops them all) and `$patch` (content and
//! tool-result updates, `removeIds`, `orderIds`). Checkpoints and `removeIds` re-sync the file to
//! the model's context: an aborted or failed request removes its own turns, while compression
//! (`ChatCompressionService`, automatic past a token threshold or `/compress`), tool-output
//! masking, truncation and `/chat resume` re-record the context under new ids and remove every
//! earlier id. The adapter keeps the transcript (see `Fold`); a compression's `<state_snapshot>`
//! user turn becomes a recap row. Resuming a legacy session copies `X.json`
//! into `X.jsonl` beside it and leaves the `.json` behind, so a `.json` with a `.jsonl`
//! sibling is superseded and not a source. The legacy store is one JSON object per session:
//!   { sessionId, projectHash, startTime, lastUpdated, messages: [ ... ] }
//! each message is `{ id, timestamp (RFC3339), type, content, displayContent?, ... }` and
//! `content` is a string or, since early 2026, a Part[] such as `[{"text": ...}]`:
//!   type=="user"   -> the human's prompt; `displayContent` holds what was typed when the sent
//!                     request differs (expanded @file context). Tool results are recorded as
//!                     user turns of functionResponse parts, the environment preamble as a
//!                     `<session_context>` user turn and IDE mode's editor context as one of its
//!                     own - none is the human
//!   type=="gemini" -> the model's turn: `content` prose reply (thought parts excluded),
//!                     `model`, `toolCalls[]`, plus `thoughts` (reasoning, excluded) and `tokens`
//!   type=="info"   -> CLI system notices (auth flow, etc.) - not the user, skipped
//! a toolCall is `{ id, name, args, result: [{functionResponse:{response:{output}}}],
//! status, agentId? }`. The project: `<project>/.project_root` names the project path (the
//! registry marker gemini-cli writes before any chat; see `Projects`), the fallback is "gemini".

mod content;
mod decode;
mod discover;
mod emit;
mod fold;

use std::path::Path;

use crate::model::{Event, Message};
use decode::parse_file;
use discover::{is_session_name, session_files, side_parent, superseded, Projects};

pub fn collect(cache: &mut crate::intake_batch::IntakeBatch) -> (Vec<Message>, Vec<Event>) {
    let root = crate::ingest::home().join(".gemini").join("tmp");
    let (files, retired) = session_files(&root);
    let projects = Projects::discover(&root);
    let pass = crate::intake_batch::collect_cached_retiring_for(
        cache,
        "gemini",
        &files,
        &retired,
        move |path| parse_file(path, &projects),
    );
    (pass.messages, pass.events)
}

/// Registry entry (see ingest::registry). File store -> Stat.
pub struct Gemini;
impl crate::ingest::registry::Adapter for Gemini {
    fn name(&self) -> &'static str {
        "gemini"
    }
    fn fingerprint(&self) -> crate::ingest::registry::Fingerprint {
        crate::ingest::registry::Fingerprint::Stat
    }
    fn collect(&self, cache: &mut crate::intake_batch::IntakeBatch) -> (Vec<Message>, Vec<Event>) {
        collect(cache)
    }
    fn store_roots(&self) -> Vec<std::path::PathBuf> {
        vec![crate::ingest::home().join(".gemini").join("tmp")]
    }
    fn archive_content(&self, path: &std::path::Path) -> bool {
        side_parent(path).is_some()
            || (path
                .file_name()
                .and_then(|p| p.to_str())
                .is_some_and(is_session_name)
                && path
                    .parent()
                    .and_then(std::path::Path::file_name)
                    .is_some_and(|p| p == "chats"))
    }
    fn store_content(&self, path: &std::path::Path) -> bool {
        // chats/session-*.json(l) and chats/<parent>/<agent>.jsonl are conversations (see
        // `session_files`); logs/ and tool-outputs/ are telemetry
        if side_parent(path).is_some() {
            return true;
        }
        path.file_name()
            .and_then(|x| x.to_str())
            .is_some_and(is_session_name)
            && path
                .parent()
                .and_then(Path::file_name)
                .is_some_and(|dir| dir == "chats")
            && !superseded(path)
    }
}
