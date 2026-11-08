//! Persisted JSONL parser checkpoints and pi's native entry graph.
use crate::{
    intake_batch::ReadOutcome,
    model::{Event, Message},
};
use serde::{Deserialize, Serialize};
#[derive(Serialize, Deserialize)]
struct Cursor {
    offset: u64,
    records: usize,
    identity: String,
    state: String,
    first_chunk: [u8; 32],
    last_chunk: [u8; 32],
    header: String,
    leaf: Option<String>,
}
pub struct Node {
    pub id: String,
    pub parent: Option<String>,
    pub ordinal: usize,
    pub body: serde_json::Value,
}
pub struct Parsed {
    pub messages: Vec<Message>,
    pub events: Vec<Event>,
    pub outcome: ReadOutcome,
    pub cursor: Vec<u8>,
    pub nodes: Vec<Node>,
    pub append: bool,
    pub reset_nodes: bool,
    pub bytes_read: usize,
    pub bytes_parsed: usize,
}

mod checkpoint;
mod graph_codec;
mod graph_write;
mod parse;
mod references;
mod stream;

pub use graph_codec::{pack_node, unpack_node};
pub(crate) use graph_write::{save_nodes, GraphPages};
pub use parse::parse;
pub use stream::supports;
