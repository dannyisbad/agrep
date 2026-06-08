//! tilt-core — ingest, scan, score. The instant (lexical) tier lives entirely here;
//! the GPU semantic layer is a separate Python sidecar that writes back into the cache.

pub mod model;
pub mod lexicon;
pub mod simd;
pub mod scan;
pub mod score;
pub mod ingest;
pub mod cache;

pub use model::Message;
pub use scan::{Hits, Scanner};
pub use score::{Score, Tag};
