use super::graph_codec::{bucket, graph, pack_graph, Graph, GraphNode};
use super::references::{event_blob, event_parts, padding, reference_text, EventParts};
use super::Node;
use crate::model::Event;
use anyhow::Result;
use rayon::prelude::*;
use rusqlite::OptionalExtension;
use rusqlite::{params, Transaction};
use std::collections::{BTreeMap, HashMap};

/// Saved graph pages, packed in parallel batches: one parallel pack per small
/// source left the writer waiting on the pool for every source. A refresh saves
/// each source at most once, so no save reads a page that is still batched.
pub(crate) struct GraphPages {
    /// Small updates do not initialize workers just to compress one graph page.
    pool: &'static std::sync::LazyLock<rayon::ThreadPool>,
    batch: Vec<(i64, i64, Graph)>,
    nodes: usize,
}
impl GraphPages {
    const BATCH_NODES: usize = 8_192;
    pub(crate) fn new(pool: &'static std::sync::LazyLock<rayon::ThreadPool>) -> Self {
        Self {
            pool,
            batch: Vec::new(),
            nodes: 0,
        }
    }
    fn queue(
        &mut self,
        tx: &Transaction<'_>,
        source: i64,
        pages: BTreeMap<i64, Graph>,
    ) -> Result<()> {
        for (bucket, graph) in pages {
            self.nodes += graph.len();
            self.batch.push((source, bucket, graph));
        }
        if self.nodes >= Self::BATCH_NODES {
            self.flush(tx)?;
        }
        Ok(())
    }
    fn flush(&mut self, tx: &Transaction<'_>) -> Result<()> {
        let batch = std::mem::take(&mut self.batch);
        let pack = |(source, bucket, graph)| Ok((source, bucket, pack_graph(&graph)?));
        let packed: Vec<(i64, i64, Vec<u8>)> = if batch.len() <= 1 || self.nodes <= 512 {
            batch.into_iter().map(pack).collect::<Result<_>>()?
        } else {
            self.pool
                .install(|| batch.into_par_iter().map(pack).collect::<Result<_>>())?
        };
        self.nodes = 0;
        for (source, bucket, body) in packed {
            tx.prepare_cached("INSERT INTO parser_graph VALUES(?1,?2,?3) ON CONFLICT(source,bucket) DO UPDATE SET body=excluded.body")?.execute(params![source,bucket,body])?;
        }
        Ok(())
    }
    /// Inserts every batched page; required before the transaction commits.
    pub(crate) fn finish(mut self, tx: &Transaction<'_>) -> Result<()> {
        self.flush(tx)
    }
}
pub(crate) fn save_nodes(
    tx: &Transaction<'_>,
    source: i64,
    nodes: Vec<Node>,
    reset: bool,
    session: Option<(&str, &str)>,
    events: &[Event],
    pages: &mut GraphPages,
) -> Result<()> {
    if reset {
        tx.execute("DELETE FROM parser_graph WHERE source=?", [source])?;
    }
    let session = match session {
        Some((agent, session)) => tx
            .query_row(
                "SELECT id FROM event_sessions WHERE agent=?1 AND session=?2",
                params![agent, session],
                |r| r.get::<_, i64>(0),
            )
            .optional()?,
        None => None,
    };
    let mut changed: BTreeMap<i64, Graph> = BTreeMap::new();
    let writer = crate::tool_store::Writer::default();
    let cache = HashMap::new();
    let mut event_blobs: HashMap<String, (i64, i64)> = if reset {
        tx.prepare("SELECT call_id,id,blob FROM event_rows WHERE source=?1 AND active=1 AND blob IS NOT NULL")?
            .query_map(params![source], |r| Ok((r.get(0)?, (r.get(1)?,r.get(2)?))))?
            .collect::<rusqlite::Result<_>>()?
    } else {
        HashMap::new()
    };
    // Initial parsing already owns exactly the event revisions being stored.
    // Seed their lossless parser fields rather than immediately querying and
    // decompressing every newly written tool blob again.
    let mut event_cache = HashMap::new();
    if reset {
        for event in events {
            if let Some(&(id, _)) = event_blobs.get(event.call_id.as_str()) {
                event_cache.entry(id).or_insert_with(|| EventParts {
                    call: event.call_id.clone(),
                    name: event.name.clone(),
                    input: event.input.trim().to_owned(),
                    output: event.output.trim().to_owned(),
                    state: event.state.clone(),
                    input_chars: event.input_chars as i64,
                    output_chars: event.output_chars as i64,
                    output_bytes: event.output_bytes as i64,
                    ok: event.ok,
                });
            }
        }
    }
    let mut lookup = |call: &str| -> Result<Option<(i64, i64)>> {
        if let Some(blob) = event_blobs.get(call) {
            return Ok(Some(*blob));
        }
        let blob = if reset {
            None
        } else {
            event_blob(tx, source, session, call)?
        };
        if let Some(blob) = blob {
            event_blobs.insert(call.to_string(), blob);
        }
        Ok(blob)
    };
    let mut references = NodeReferences {
        tx,
        source,
        writer,
        cache,
        event_cache,
    };
    for node in nodes {
        let mut record = node.body;
        references.store(&mut record, &node.id, &mut lookup)?;
        if !matches!(
            record["type"].as_str(),
            Some("message" | "model_change" | "compaction")
        ) {
            record = serde_json::Value::Null;
        }
        if let Some(fields) = record.as_object_mut() {
            fields.remove("id");
            fields.remove("parentId");
            fields.remove("__agrep_ordinal");
        }
        let key = bucket(&node.id);
        if let std::collections::btree_map::Entry::Vacant(e) = changed.entry(key) {
            e.insert(if reset {
                Graph::new()
            } else {
                graph(tx, source, key)?
            });
        }
        changed.get_mut(&key).unwrap().insert(
            node.id,
            GraphNode {
                parent: node.parent,
                ordinal: node.ordinal,
                record,
            },
        );
    }
    pages.queue(tx, source, changed)
}

struct NodeReferences<'a, 'conn> {
    tx: &'a Transaction<'conn>,
    source: i64,
    writer: crate::tool_store::Writer,
    cache: HashMap<i64, String>,
    event_cache: HashMap<i64, EventParts>,
}
impl NodeReferences<'_, '_> {
    fn store(
        &mut self,
        record: &mut serde_json::Value,
        id: &str,
        lookup: &mut impl FnMut(&str) -> Result<Option<(i64, i64)>>,
    ) -> Result<()> {
        let tx = self.tx;
        let source = self.source;
        let call = record["message"]["toolCallId"]
            .as_str()
            .unwrap_or(id)
            .to_owned();
        let role = record["message"]["role"].as_str().unwrap_or("").to_owned();
        if role == "toolResult" {
            let preferred = lookup(&call)?;
            let marker = if let (Some((id, _)), Some(original)) =
                (preferred, record["message"]["content"].as_str())
            {
                let p = event_parts(tx, id, &mut self.event_cache)?;
                padding(original, &p.output).map(
                    |(left, right)| serde_json::json!({"__agrep_event_output":[id,left,right]}),
                )
            } else {
                None
            };
            if let Some(marker) = marker {
                record["message"] = marker;
            } else {
                reference_text(
                    tx,
                    source,
                    &mut record["message"]["content"],
                    preferred.map(|(_, blob)| blob),
                    &mut self.writer,
                    &mut self.cache,
                )?;
            }
        } else if role == "bashExecution" {
            let preferred = lookup(&call)?;
            reference_text(
                tx,
                source,
                &mut record["message"]["output"],
                preferred.map(|(_, blob)| blob),
                &mut self.writer,
                &mut self.cache,
            )?;
            reference_text(
                tx,
                source,
                &mut record["message"]["command"],
                preferred.map(|(_, blob)| blob),
                &mut self.writer,
                &mut self.cache,
            )?;
        } else if role == "assistant" {
            if let Some(blocks) = record["message"]["content"].as_array_mut() {
                for block in blocks {
                    if block["type"] == "toolCall" {
                        let call = block["id"].as_str().unwrap_or("");
                        let preferred = lookup(call)?;
                        let marker = if let (Some((id, _)), Some(original)) =
                            (preferred, block["__agrep_input"].as_str())
                        {
                            let p = event_parts(tx, id, &mut self.event_cache)?;
                            if block["name"].as_str() == Some(p.name.as_str())
                                && block["__agrep_state"].as_str() == Some(p.state.as_str())
                            {
                                padding(original,&p.input).map(|(left,right)|serde_json::json!({"__agrep_event_call":[id,left,right]}))
                            } else {
                                None
                            }
                        } else {
                            None
                        };
                        if let Some(marker) = marker {
                            *block = marker;
                        } else {
                            reference_text(
                                tx,
                                source,
                                &mut block["__agrep_input"],
                                preferred.map(|(_, blob)| blob),
                                &mut self.writer,
                                &mut self.cache,
                            )?;
                        }
                    }
                }
            }
        }
        Ok(())
    }
}
