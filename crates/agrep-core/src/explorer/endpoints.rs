//! The authorized history endpoints: parameter validation, then the shared core reads.
use super::{
    api,
    http::{Request, Response},
};
use crate::{
    data::DataRoot,
    query::{self, Filters, Options, Results},
    semantic::QueryWork,
};
use anyhow::Result;
use rusqlite::Connection;
use serde_json::{json, Value};

pub(super) fn status(c: &Connection, checked: bool) -> Result<Response> {
    let sessions: i64 = c.query_row("SELECT COUNT(*) FROM sessions", [], |r| r.get(0))?;
    let messages: i64 = c.query_row(
        "SELECT COUNT(*) FROM prose WHERE visible=1 AND who!='agent'",
        [],
        |r| r.get(0),
    )?;
    Ok(Response::json(
        200,
        json!({"messages":messages,"sessions":sessions,"indexed":true,"freshness":crate::render::freshness(checked)}),
    ))
}
pub(super) fn chats(c: &Connection, r: &Request) -> Result<Response> {
    let n = match r.param("n", "80").parse::<i64>() {
        Ok(n) => n.clamp(1, 200) as usize,
        Err(_) => return Ok(Response::error(400, "n must be an integer")),
    };
    let side = r.param("side", "0");
    if !["0", "1"].contains(&side.as_str()) {
        return Ok(Response::error(400, "side must be 0 or 1"));
    }
    Ok(Response::json(200, api::chats(c, n, side == "1")?))
}
pub(super) fn around(c: &Connection, r: &Request) -> Result<Response> {
    let id = r.param("session", "");
    if id.trim().is_empty() || id.len() > 4096 {
        return Ok(Response::error(400, "session is required"));
    }
    let (turn, radius) = match (
        r.param("turn", "0").parse::<i64>(),
        r.param("radius", "4").parse::<i64>(),
    ) {
        (Ok(t), Ok(r)) => (t, r.clamp(0, 12)),
        _ => return Ok(Response::error(400, "turn and radius must be integers")),
    };
    let window = match api::window(c, &id, turn, radius) {
        Ok(w) => w,
        Err(e) if e.downcast_ref::<rusqlite::Error>().is_none() => {
            return Ok(Response::error(400, &e.to_string()))
        }
        Err(e) => return Err(e),
    };
    Ok(match window {
        Some(v) => Response::json(200, v),
        None => Response::error(404, "chat is no longer indexed"),
    })
}

/// A validated search request.
struct Search {
    q: String,
    mode: String,
    sort: String,
    n: usize,
}
/// The search a request asks for, or the response refusing it.
fn search_request(root: &DataRoot, r: &Request) -> std::result::Result<Search, Response> {
    let q = r.param("q", "").trim().to_owned();
    if q.is_empty() {
        return Err(Response::error(400, "enter a search query"));
    }
    if q.len() > 4096 {
        return Err(Response::error(413, "search query is too large"));
    }
    let mode = r.param("mode", "keyword");
    if !["keyword", "semantic"].contains(&mode.as_str()) {
        return Err(Response::error(400, "mode must be keyword or semantic"));
    }
    let sort = r.param("sort", "score");
    if !["score", "time"].contains(&sort.as_str()) {
        return Err(Response::error(400, "sort must be score or time"));
    }
    if mode == "semantic" && sort != "score" {
        return Err(Response::error(
            400,
            "meaning search is relevance-ordered; use sort=score",
        ));
    }
    if mode == "semantic" && !crate::semantic::installed(root) {
        return Err(Response::error(
            400,
            "semantic model not installed: run agrep setup",
        ));
    }
    let n = match r.param("n", "40").parse::<i64>() {
        Ok(n) => n.clamp(1, 100) as usize,
        Err(_) => return Err(Response::error(400, "n must be an integer")),
    };
    Ok(Search { q, mode, sort, n })
}
pub(super) fn search(
    root: &DataRoot,
    c: &Connection,
    checked: bool,
    r: &Request,
) -> Result<Response> {
    let s = match search_request(root, r) {
        Ok(s) => s,
        Err(refusal) => return Ok(refusal),
    };
    let options = Options {
        pattern: s.q.clone(),
        regex: false,
        word: false,
        sort: s.sort.clone(),
        limit: s.n,
        exact: false,
        count_only: false,
        count_by_tier: false,
        content: false,
        chats: false,
        tools: root.config()?.tools,
        filters: Filters::default(),
    };
    let config = root.config()?;
    let use_semantic = s.mode == "semantic"
        || (s.sort == "score"
            && config.embeddings != "off"
            && crate::semantic::eligible(&s.q)
            && crate::semantic::installed(root));
    let lane = use_semantic.then(|| crate::semantic::begin(root, c, &options, true));
    let keyword = query::search(c, &options)?;
    let answer = match lane {
        Some(lane) => meaning(root, c, &options, &s, keyword, lane)?,
        None => Answer {
            result: keyword,
            semantic: None,
            notice: None,
        },
    };
    let result = answer.result;
    Ok(Response::json(
        200,
        json!({"query":s.q,"mode":s.mode,"sort":s.sort,"engine":if result.hits.iter().any(|h| h.matched=="semantic"){"keyword+semantic"}else{"keyword"},"notice":answer.notice,"semantic":answer.semantic,"freshness":crate::render::freshness(checked),"total":result.total,"totals_exact":result.total_basis=="exact","hits":result.hits}),
    ))
}
struct Answer {
    result: Results,
    /// The meaning lane's state, reported for an explicit meaning search.
    semantic: Option<Value>,
    notice: Option<&'static str>,
}
/// Joins the meaning lane to the keyword answer: an explicit meaning search answers
/// from it alone, an automatic hybrid merges it.
fn meaning(
    root: &DataRoot,
    c: &Connection,
    options: &Options,
    s: &Search,
    keyword: Results,
    lane: Result<QueryWork>,
) -> Result<Answer> {
    let explicit = s.mode == "semantic";
    Ok(
        match lane.and_then(|work| crate::semantic::finish(c, work)) {
            Ok(mut found) => {
                crate::semantic::mark_weak(
                    c,
                    options,
                    &mut found.result.hits,
                    !keyword.hits.is_empty(),
                )?;
                // An answered meaning lane always covers its snapshot.
                if explicit {
                    Answer {
                        result: found.result,
                        semantic: Some(json!(found.coverage)),
                        notice: None,
                    }
                } else {
                    Answer {
                        result: crate::hybrid::merge(keyword, Some(found.result), s.n, true),
                        semantic: None,
                        notice: None,
                    }
                }
            }
            // An explicit meaning search never answers with keyword rows: 0.4.9's
            // explorer returned none, with a state that sends the reader to grep
            // and without the cause, since the browser gets no local paths
            // (py/server.py:142-159, 513-515; py/search.py:581-599).
            Err(error) if explicit => {
                let status = crate::semantic::failed(&error);
                let mut unavailable =
                    json!({"state":status["state"],"complete":false,"fallback_recommended":true});
                if let Some(progress) = status.get("progress") {
                    unavailable["progress"] = progress.clone();
                }
                Answer {
                    result: query::Results::default(),
                    semantic: Some(unavailable),
                    notice: None,
                }
            }
            Err(error) if crate::semantic::is_building(&error) => Answer {
                result: keyword,
                semantic: None,
                notice: None,
            },
            Err(error) => {
                crate::logging::semantic_failure(root, &error);
                Answer {
                    result: keyword,
                    semantic: None,
                    notice: Some("meaning search unavailable; showing keyword matches"),
                }
            }
        },
    )
}
