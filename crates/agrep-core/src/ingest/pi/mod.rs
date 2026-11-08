//! pi / oh-my-pi adapter: <root>/agent/sessions/<cwd-slug>/<session>.jsonl
//!
//! Two stores share one format: `~/.pi` (pi) and `~/.omp` (the oh-my-pi fork).
//! Each session is JSONL of `type`-tagged records. The first line is a `session`
//! header carrying `version` (1|2|3), `id`, and `cwd`; every later record has an
//! `id`, a `parentId`, and an ISO `timestamp`.
//!
//! v2/v3 are TREES, not lists: branching appends children off an earlier entry
//! in the same file, so the tail of the file is the current leaf and the active
//! branch is the parent chain walked back from it (pi's own buildContextEntries).
//! Abandoned branches stay in the file and are deliberately not indexed - pi's
//! resume never shows them. v1 has no ids and is read in file order.
//!
//! Records that matter here: `message` (role user/assistant/toolResult, content
//! blocks text/thinking/toolCall) and `compaction`, whose summary becomes a
//! recap row so `agrep postcompact` works without a hook. `model_change` applies
//! forward. Reasoning (`thinking`) is excluded like every other adapter.
//! The fork adds fields (shortSummary, fromExtension, developer role, named
//! sessions like advisor sidecars); unknown records and roles are skipped, not
//! failed on. Sidecars receive `synthetic: true` user-role mirrors of the
//! watched session's transcript; those are sidechain copies, never user rows.
//! A sidecar assistant left anchorless by those skips is the sidecar's own
//! voice (advisor advisories): its text is indexed as its own row.
//! A sidechat container is keyed off the root's filename, not its header, so a root whose
//! header id was rewritten keeps the filename id as a session alias (`session_alias`).
//!
//! Layout: `discovery` finds session files and reads identity from their paths,
//! `read` loads a whole file, `projection` and `record` turn JSONL lines into the
//! compact records the parser keeps, `branch` walks the active branch, and `parse`
//! turns that branch into rows and tool events (`parse::resume` carries the
//! checkpoint an append continues from).

mod branch;
mod discovery;
mod parse;
mod projection;
mod read;
mod record;

use std::path::{Path, PathBuf};

use crate::model::{Event, Message};

pub(crate) use discovery::{session_files, source_order};
pub(crate) use parse::resume_prepared;
pub(crate) use projection::{
    optional_projected_record_file, projected_record, validate_record, validate_record_reader,
};
pub use record::compact_record;
pub(crate) use record::PreparedRecord;

/// What one parsed session yields: rows, tool events, and how completely the
/// file was read.
type Parsed = (Vec<Message>, Vec<Event>, crate::intake_batch::ReadOutcome);

fn parse_pi(path: &Path) -> Parsed {
    read::parse_file("pi", path, discovery::side_parent(path))
}

fn collect_store(
    cache: &mut crate::intake_batch::IntakeBatch,
    agent: &'static str,
    root: &Path,
    parse: fn(&Path) -> Parsed,
) -> (Vec<Message>, Vec<Event>) {
    let files = cache
        .preflight_source_paths(root)
        .unwrap_or_else(|| session_files(agent, root));
    let pass = crate::intake_batch::collect_cached_for(cache, agent, &files, parse);
    (pass.messages, pass.events)
}

/// Registry entry (see ingest::registry). File store -> Stat.
///
/// One agent, two homes and each home's cold archive: oh-my-pi is a fork of the
/// same format, and the owner named both "pi", so every session lands under the
/// same label rather than a second entry nobody would think to filter by.
pub struct Pi;
impl crate::ingest::registry::Adapter for Pi {
    fn name(&self) -> &'static str {
        "pi"
    }
    fn fingerprint(&self) -> crate::ingest::registry::Fingerprint {
        crate::ingest::registry::Fingerprint::Stat
    }
    fn collect(&self, cache: &mut crate::intake_batch::IntakeBatch) -> (Vec<Message>, Vec<Event>) {
        let mut messages = Vec::new();
        let mut events = Vec::new();
        for root in discovery::session_roots() {
            let (rows, found) = collect_store(cache, "pi", &root, parse_pi);
            messages.extend(rows);
            events.extend(found);
        }
        (messages, events)
    }
    fn store_roots(&self) -> Vec<PathBuf> {
        discovery::session_roots().collect()
    }
    fn store_content(&self, path: &Path) -> bool {
        discovery::is_session_file(path)
    }
    fn session_alias(&self, path: &Path, session: &str) -> Option<String> {
        discovery::session_alias(path, session)
    }
}
