//! The audit's problems, each source named once: a damaged source fails in its stored
//! refresh, and again in the census or the full audit's reparse.
use serde_json::{json, Value};
use std::collections::HashMap;

/// Where a problem was seen, least specific first: a stored error is as of the last
/// refresh, the census only stamped the source, and the reparse read it just now.
#[derive(Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub(super) enum Seen {
    Stored,
    Census,
    Reparse,
}

/// Problems in the order their sources were first named, each with its most specific
/// reason.
#[derive(Default)]
pub(super) struct Problems {
    list: Vec<(Value, Seen)>,
    by_path: HashMap<String, usize>,
}

impl Problems {
    pub(super) fn source(&mut self, path: impl Into<Value>, reason: impl Into<Value>, seen: Seen) {
        let problem = json!({"path":path.into(),"reason":reason.into()});
        let Some(path) = problem["path"].as_str().map(str::to_owned) else {
            self.list.push((problem, seen));
            return;
        };
        match self.by_path.get(&path) {
            Some(&at) if seen > self.list[at].1 => self.list[at] = (problem, seen),
            Some(_) => {}
            None => {
                self.by_path.insert(path, self.list.len());
                self.list.push((problem, seen));
            }
        }
    }

    /// A problem no one source holds.
    pub(super) fn other(&mut self, problem: Value) {
        self.list.push((problem, Seen::Reparse));
    }

    pub(super) fn is_empty(&self) -> bool {
        self.list.is_empty()
    }

    pub(super) fn into_values(self) -> Vec<Value> {
        self.list.into_iter().map(|(problem, _)| problem).collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_source_is_named_once_where_first_found_with_its_freshest_reason() {
        let mut problems = Problems::default();
        problems.source("b", "stored b", Seen::Stored);
        problems.source("a", "census a", Seen::Census);
        problems.source("a", "stored a", Seen::Stored);
        problems.other(json!({"agent":"codex","reason":"source read Partial"}));
        problems.source("b", "reparse b", Seen::Reparse);
        problems.source("b", "second reparse b", Seen::Reparse);
        problems.source("c", "reparse c", Seen::Reparse);
        assert_eq!(
            problems.into_values(),
            [
                json!({"path":"b","reason":"reparse b"}),
                json!({"path":"a","reason":"census a"}),
                json!({"agent":"codex","reason":"source read Partial"}),
                json!({"path":"c","reason":"reparse c"}),
            ]
        );
    }
}
