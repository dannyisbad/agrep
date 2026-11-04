//! Surface validation and evidence for requested dimensions absent from the index.
use crate::query::Filters;
use anyhow::{bail, Result};
use rusqlite::Connection;
use serde_json::{json, Value};
pub fn speakers(value: &str) -> Result<String> {
    let mut out = Vec::new();
    for word in value.split(',') {
        let word = word.trim();
        let word = if word == "you" { "user" } else { word };
        if ![
            "user",
            "agent",
            "subagent",
            "tool",
            "control",
            "synthetic",
            "recap",
            "harness",
        ]
        .contains(&word)
        {
            bail!("unknown speaker '{word}' - valid: user, subagent, agent, tool, control, synthetic, recap, harness")
        }
        if !out.contains(&word) {
            out.push(word)
        }
    }
    Ok(out.join(","))
}
pub fn validate(f: &mut Filters) -> Result<()> {
    for (name, value) in [
        ("--agent", &f.agent),
        ("--project", &f.project),
        ("--exclude-project", &f.exclude_project),
        ("--model", &f.model),
        ("--chat", &f.chat),
        ("--who", &f.who),
        ("--no-who", &f.no_who),
    ] {
        if value.as_ref().is_some_and(|v| v.trim().is_empty()) {
            bail!("{name} cannot be empty")
        }
    }
    if let Some(v) = &f.who {
        f.who = Some(speakers(v)?)
    }
    if let Some(v) = &f.no_who {
        f.no_who = Some(speakers(v)?)
    }
    if let Some(agents) = &f.agent {
        f.agent = Some(
            agents
                .split(',')
                .map(|a| match a.trim().to_ascii_lowercase().as_str() {
                    "omp" | "oh-my-pi" => "pi".to_owned(),
                    "agy" => "antigravity".to_owned(),
                    other => other.to_owned(),
                })
                .collect::<Vec<_>>()
                .join(","),
        );
    }
    if let Some(agents) = &f.agent {
        for a in agents.split(',') {
            if !crate::ingest::registry::ADAPTERS
                .iter()
                .any(|adapter| adapter.name() == a)
            {
                bail!("unknown agent '{a}'")
            }
        }
    }
    Ok(())
}
/// 0.4.9's filter dimensions with their plural labels (py/surface_policy.py:843-848).
const LABELS: [(&str, &str); 4] = [
    ("agent", "agents"),
    ("project", "projects"),
    ("model", "models"),
    ("who", "speakers"),
];
pub fn dimensions(c: &Connection, f: &Filters) -> Result<Value> {
    let mut absent = Vec::new();
    for (flag, dimension, col, value, table) in [
        ("--agent", "agent", "agent", &f.agent, "sessions"),
        ("--project", "project", "project", &f.project, "sessions"),
        ("--model", "model", "model", &f.model, "prose"),
        ("--who", "who", "who", &f.who, "prose"),
    ] {
        let Some(value) = value else { continue };
        let tools = || -> Result<bool> {
            Ok(c.query_row("SELECT EXISTS(SELECT 1 FROM tools)", [], |r| r.get(0))?)
        };
        // `who` has no index: a requested speaker that occurs ends its scan at the first
        // row, so only an empty dimension pays for listing every speaker.
        if dimension == "who" {
            let mut any = false;
            for speaker in value.split(',') {
                any |= if speaker == "tool" {
                    tools()?
                } else {
                    c.query_row(
                        "SELECT EXISTS(SELECT 1 FROM prose WHERE who=?1)",
                        [speaker],
                        |r| r.get(0),
                    )?
                };
                if any {
                    break;
                }
            }
            if any {
                continue;
            }
        }
        let mut q = c.prepare(&format!(
            "SELECT DISTINCT {col} FROM {table} WHERE {col}!='' ORDER BY {col}"
        ))?;
        let mut values: Vec<String> = q
            .query_map([], |r| r.get(0))?
            .collect::<rusqlite::Result<_>>()?;
        if dimension == "who" && tools()? {
            values.push("tool".into());
            values.sort();
        }
        let pattern = if dimension == "model" && f.model_soft {
            format!("*{}*", value.to_lowercase())
        } else {
            value.to_lowercase()
        };
        let matcher = globset::Glob::new(&pattern)?.compile_matcher();
        let found = values.iter().any(|v| {
            matcher.is_match(v.to_lowercase())
                || (dimension == "project"
                    && matcher.is_match(crate::render::project(v).to_lowercase()))
                || (matches!(dimension, "agent" | "who") && value.split(',').any(|a| a == v))
        });
        if !found {
            let known = if values.len() <= KNOWN_SHOWN {
                values.iter().collect()
            } else {
                nearest(value, &values)
            };
            let complete = known.len() == values.len();
            absent.push(json!({"flag":flag,"dimension":dimension,"value":value,"indexed_values":values.len(),"known":known,"known_complete":complete}));
        }
    }
    Ok(json!({"empty_dimensions":absent,"checked":true}))
}
const KNOWN_SHOWN: usize = 6;
/// 0.4.9's `_nearest_values` (py/surface_policy.py:869-875): the values a typo most likely
/// meant, by difflib similarity of the case-folded strings, ties by value.
fn nearest<'a>(value: &str, values: &'a [String]) -> Vec<&'a String> {
    let folded: Vec<char> = crate::unicode_v16::case_fold(value).chars().collect();
    let mut ranked: Vec<(f64, &String)> = values
        .iter()
        .map(|v| {
            let other: Vec<char> = crate::unicode_v16::case_fold(v).chars().collect();
            (similarity(&folded, &other), v)
        })
        .collect();
    ranked.sort_by(|a, b| b.0.total_cmp(&a.0).then_with(|| a.1.cmp(b.1)));
    ranked
        .into_iter()
        .take(KNOWN_SHOWN)
        .map(|(_, v)| v)
        .collect()
}
/// Python's `difflib.SequenceMatcher(None, a, b).ratio()`, autojunk included: blocks are
/// chosen exactly as `find_longest_match` chooses them, since the choice changes the total.
fn similarity(a: &[char], b: &[char]) -> f64 {
    use std::collections::HashMap;
    let mut b2j: HashMap<char, Vec<usize>> = HashMap::new();
    for (j, c) in b.iter().enumerate() {
        b2j.entry(*c).or_default().push(j);
    }
    if b.len() >= 200 {
        let popular = b.len() / 100 + 1;
        b2j.retain(|_, at| at.len() <= popular);
    }
    let longest = |alo: usize, ahi: usize, blo: usize, bhi: usize| {
        let (mut i0, mut j0, mut size) = (alo, blo, 0);
        let mut run: HashMap<usize, usize> = HashMap::new();
        for (i, c) in a.iter().enumerate().take(ahi).skip(alo) {
            let mut next = HashMap::new();
            for &j in b2j.get(c).map(Vec::as_slice).unwrap_or_default() {
                if j < blo {
                    continue;
                }
                if j >= bhi {
                    break;
                }
                let k = j.checked_sub(1).and_then(|p| run.get(&p)).unwrap_or(&0) + 1;
                next.insert(j, k);
                if k > size {
                    (i0, j0, size) = (i + 1 - k, j + 1 - k, k);
                }
            }
            run = next;
        }
        while i0 > alo && j0 > blo && a[i0 - 1] == b[j0 - 1] {
            (i0, j0, size) = (i0 - 1, j0 - 1, size + 1);
        }
        while i0 + size < ahi && j0 + size < bhi && a[i0 + size] == b[j0 + size] {
            size += 1;
        }
        (i0, j0, size)
    };
    let mut matches = 0;
    let mut queue = vec![(0, a.len(), 0, b.len())];
    while let Some((alo, ahi, blo, bhi)) = queue.pop() {
        let (i, j, k) = longest(alo, ahi, blo, bhi);
        if k > 0 {
            matches += k;
            if alo < i && blo < j {
                queue.push((alo, i, blo, j));
            }
            if i + k < ahi && j + k < bhi {
                queue.push((i + k, ahi, j + k, bhi));
            }
        }
    }
    match a.len() + b.len() {
        0 => 1.0,
        total => 2.0 * matches as f64 / total as f64,
    }
}
/// 0.4.9's `empty_dimension_line` (py/surface_policy.py:896-910).
pub fn disclose(coverage: &Value) {
    if let Some(rows) = coverage["empty_dimensions"].as_array() {
        for r in rows {
            let dimension = r["dimension"].as_str().unwrap_or_default();
            let label = LABELS
                .iter()
                .find(|(d, _)| *d == dimension)
                .map_or(dimension, |(_, label)| label);
            let known = r["known"]
                .as_array()
                .into_iter()
                .flatten()
                .filter_map(Value::as_str)
                .collect::<Vec<_>>()
                .join(", ");
            let known = crate::surface::safe(&known, false);
            let held = if known.is_empty() {
                format!("the index holds no {label} at all")
            } else if r["known_complete"] == true {
                format!("indexed {label}: {known}")
            } else {
                format!("{} indexed {label}, closest: {known}", r["indexed_values"])
            };
            eprintln!(
                "{} {} isn't in the index - {held}",
                r["flag"].as_str().unwrap_or_default(),
                crate::surface::safe(r["value"].as_str().unwrap_or_default(), false),
            );
        }
    }
}
/// Small covering index for model validation; built by explicit indexing/setup.
pub fn initialize(root: &crate::data::DataRoot) -> Result<()> {
    let _lock = root.lock(true)?;
    crate::store::writer(root)?
        .execute_batch("CREATE INDEX IF NOT EXISTS prose_model ON prose(model)")?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn speaker_alias_is_canonical() {
        assert_eq!(speakers("you,agent").unwrap(), "user,agent");
        assert!(speakers("user,").is_err());
    }
    // Expected values are 0.4.9's own `_nearest_values` and difflib under Python 3.14.
    #[test]
    fn closest_values_rank_as_0_4_9() {
        let owned = |v: &[&str]| v.iter().map(|s| s.to_string()).collect::<Vec<_>>();
        let chars = |s: &str| s.chars().collect::<Vec<_>>();
        let long = format!("{}{}/x", "ab".repeat(60), "c".repeat(90));
        let qc = format!("q{}", "c".repeat(199));
        assert_eq!(similarity(&chars("ccc"), &chars(&long)), 0.0);
        assert_eq!(similarity(&chars("qc"), &chars(&qc)), 0.019801980198019802);
        assert_eq!(
            similarity(&chars("claude-opus5"), &chars("claude-opus-5")),
            0.96
        );
        assert_eq!(similarity(&[], &[]), 1.0);
        let mut models: Vec<String> = (0..40).map(|i| format!("model-{i}")).collect();
        models.push("claude-opus-5".into());
        assert_eq!(
            nearest("claude-opus5", &models),
            [
                "claude-opus-5",
                "model-5",
                "model-15",
                "model-25",
                "model-35",
                "model-0"
            ]
        );
        let projects = owned(&[
            "/work/alpha",
            "/work/beta",
            "/work/legacy",
            "/work/upstream",
            "/home/u/Straße",
            "/home/u/STRASSE-old",
            "/srv/gamma",
            "/srv/delta",
        ]);
        assert_eq!(
            nearest("strasse", &projects),
            [
                "/home/u/Straße",
                "/home/u/STRASSE-old",
                "/work/upstream",
                "/srv/delta",
                "/srv/gamma",
                "/work/beta"
            ]
        );
        let ties = owned(&["bca", "acb", "cab", "abc", "bac", "cba", "zzz"]);
        assert_eq!(
            nearest("CBA", &ties),
            ["cba", "acb", "bac", "bca", "cab", "abc"]
        );
        let mut junk = owned(&["a", "b", "d", "e", "f", "g"]);
        junk.push(long.clone());
        assert_eq!(
            nearest("ccc", &junk),
            [
                &"a".to_string(),
                &long,
                &"b".into(),
                &"d".into(),
                &"e".into(),
                &"f".into()
            ]
        );
        let mut extended = owned(&["qz", "zz", "yy", "xx", "ww", "vv"]);
        extended.push(qc.clone());
        assert_eq!(
            nearest("qc", &extended),
            [
                &"qz".to_string(),
                &qc,
                &"vv".into(),
                &"ww".into(),
                &"xx".into(),
                &"yy".into()
            ]
        );
    }
}
