//! The embedding index store: a memory-mapped `embeddings.f32` matrix plus the parallel `ids`
//! file that names each row.
//!
//! The embedding contract (shared verbatim with the Python sidecar):
//!   - `data/embeddings.f32` : raw little-endian f32, row-major, N rows x D cols, each ROW
//!     L2-normalized. D = 256 (Matryoshka truncation of Qwen3-Embedding-0.6B's 1024-d, renormalized).
//!   - `data/embeddings.ids` : UTF-8, one message id per line; row `r` of the matrix corresponds to
//!     line `r` here. The ids file is the authority on row order (it need not match messages.jsonl).
//!   - `data/query.f32` : a single D-dim L2-normalized f32 vector (the current search query).
//!
//! Because every row and the query are L2-normalized, cosine similarity == dot product, which is
//! exactly what [`crate::search::top_k`] computes.

use std::fs;
use std::path::Path;

use anyhow::{anyhow, bail, Context, Result};
use memmap2::Mmap;

use crate::search;

/// Default embedding dimensionality if the index isn't self-describing (legacy fallback).
pub const DIM: usize = 256;

/// Read the embedding dim from `dir/embeddings.meta` (a plain integer written by the sidecar).
/// Falls back to [`DIM`] if the file is missing or unparseable, so older indexes still open.
pub fn read_dim(dir: &Path) -> usize {
    std::fs::read_to_string(dir.join("embeddings.meta"))
        .ok()
        .and_then(|s| s.split_whitespace().next().map(str::to_string))
        .and_then(|s| s.parse::<usize>().ok())
        .filter(|&d| d > 0)
        .unwrap_or(DIM)
}

/// A read-only, memory-mapped embedding matrix and its row-id sidecar.
#[derive(Debug)]
pub struct Index {
    /// Vector width (cols). Equals [`DIM`] under the current contract.
    pub dim: usize,
    /// Row labels; `ids[r]` names row `r`. Authoritative for row order.
    pub ids: Vec<String>,
    /// The raw `embeddings.f32` bytes, mmapped. Interpreted as row-major f32 via [`Index::as_f32`].
    pub mat: Mmap,
}

impl Index {
    /// Open the index living in `dir`: mmap `embeddings.f32`, read `embeddings.ids` (one id per
    /// line), infer N from `file_len / (dim * 4)`, and assert `N == ids.len()`. The dimension is
    /// read from `embeddings.meta` (self-describing index), falling back to [`DIM`] for older indexes.
    pub fn open(dir: &Path) -> Result<Index> {
        Self::open_with_dim(dir, read_dim(dir))
    }

    /// Same as [`Index::open`] but with an explicit dimension (used in tests / future contracts).
    pub fn open_with_dim(dir: &Path, dim: usize) -> Result<Index> {
        if dim == 0 {
            bail!("index dim must be non-zero");
        }
        let mat_path = dir.join("embeddings.f32");
        let ids_path = dir.join("embeddings.ids");

        let file = fs::File::open(&mat_path)
            .with_context(|| format!("opening {} (run the embed step first?)", mat_path.display()))?;
        // SAFETY: we only ever read through the map and the backing file is not mutated by us while
        // open. The contract treats embeddings.f32 as immutable once written by the sidecar.
        let mat = unsafe { Mmap::map(&file) }
            .with_context(|| format!("mmapping {}", mat_path.display()))?;

        let stride = dim * 4;
        if mat.len() % stride != 0 {
            bail!(
                "{} is {} bytes, not a multiple of the row stride {} (dim {} x 4)",
                mat_path.display(),
                mat.len(),
                stride,
                dim
            );
        }
        let rows = mat.len() / stride;

        let ids_text = fs::read_to_string(&ids_path)
            .with_context(|| format!("reading {} (run the embed step first?)", ids_path.display()))?;
        // One id per line; trailing newline is fine. Empty lines are not expected but are skipped
        // defensively rather than producing a phantom empty id.
        let ids: Vec<String> = ids_text
            .lines()
            .map(|l| l.trim_end_matches('\r'))
            .filter(|l| !l.is_empty())
            .map(|l| l.to_string())
            .collect();

        if ids.len() != rows {
            bail!(
                "row/id count mismatch: {} has {} rows but {} has {} ids",
                mat_path.display(),
                rows,
                ids_path.display(),
                ids.len()
            );
        }

        Ok(Index { dim, ids, mat })
    }

    /// Number of rows (vectors) in the matrix.
    pub fn rows(&self) -> usize {
        if self.dim == 0 {
            0
        } else {
            self.mat.len() / (self.dim * 4)
        }
    }

    /// Reinterpret the mmapped bytes as a row-major `&[f32]`.
    ///
    /// The fast path is a zero-copy `bytemuck::cast_slice` when the mapping is 4-byte aligned (the
    /// common case — OS page maps are page-aligned). If a map ever comes back unaligned we fall
    /// back to decoding into an owned `Vec<f32>` via `from_le_bytes`, which also makes endianness
    /// explicit (the contract specifies little-endian).
    fn as_f32(&self) -> std::borrow::Cow<'_, [f32]> {
        let bytes: &[u8] = &self.mat;
        if cfg!(target_endian = "little") && (bytes.as_ptr() as usize) % std::mem::align_of::<f32>() == 0 {
            // SAFETY: alignment checked above; len is a multiple of 4 (verified in `open`).
            let floats: &[f32] = bytemuck::cast_slice(bytes);
            std::borrow::Cow::Borrowed(floats)
        } else {
            let decoded: Vec<f32> = bytes
                .chunks_exact(4)
                .map(|c| f32::from_le_bytes([c[0], c[1], c[2], c[3]]))
                .collect();
            std::borrow::Cow::Owned(decoded)
        }
    }

    /// Brute-force cosine search: return the top-`k` `(id, score)` pairs, descending. `query` must
    /// be `dim`-long and L2-normalized (the embedding contract guarantees the matrix rows are).
    pub fn search(&self, query: &[f32], k: usize) -> Vec<(String, f32)> {
        if query.len() != self.dim {
            return Vec::new();
        }
        let floats = self.as_f32();
        search::top_k(query, &floats, self.dim, k)
            .into_iter()
            .map(|(row, score)| (self.ids[row].clone(), score))
            .collect()
    }
}

/// Load `data/query.f32`: a single `dim`-wide little-endian f32 vector. Errors (not panics) if the
/// file is missing or the wrong size, so the CLI can print a "run the embed step" hint.
pub fn load_query(path: &Path, dim: usize) -> Result<Vec<f32>> {
    let bytes = fs::read(path)
        .with_context(|| format!("reading {} (run the embed step first?)", path.display()))?;
    if bytes.len() != dim * 4 {
        return Err(anyhow!(
            "{} is {} bytes; expected exactly {} ({} f32 query dims)",
            path.display(),
            bytes.len(),
            dim * 4,
            dim
        ));
    }
    let q: Vec<f32> = bytes
        .chunks_exact(4)
        .map(|c| f32::from_le_bytes([c[0], c[1], c[2], c[3]]))
        .collect();
    Ok(q)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    fn write_le(path: &Path, floats: &[f32]) {
        let mut f = fs::File::create(path).unwrap();
        for x in floats {
            f.write_all(&x.to_le_bytes()).unwrap();
        }
    }

    #[test]
    fn open_search_roundtrip() {
        let dim = 4usize;
        let dir = std::env::temp_dir().join(format!("tilt-index-test-{}", std::process::id()));
        fs::create_dir_all(&dir).unwrap();

        // Three orthonormal-ish rows so the nearest neighbour is unambiguous.
        let rows: Vec<Vec<f32>> = vec![
            vec![1.0, 0.0, 0.0, 0.0],
            vec![0.0, 1.0, 0.0, 0.0],
            vec![0.0, 0.0, 1.0, 0.0],
        ];
        let flat: Vec<f32> = rows.iter().flatten().copied().collect();
        write_le(&dir.join("embeddings.f32"), &flat);
        fs::write(&dir.join("embeddings.ids"), "id-a\nid-b\nid-c\n").unwrap();

        let idx = Index::open_with_dim(&dir, dim).unwrap();
        assert_eq!(idx.rows(), 3);
        assert_eq!(idx.ids, vec!["id-a", "id-b", "id-c"]);

        // Query aligned with row 1 -> "id-b" must rank first with score ~1.0.
        let q = vec![0.0, 1.0, 0.0, 0.0];
        let hits = idx.search(&q, 2);
        assert_eq!(hits.len(), 2);
        assert_eq!(hits[0].0, "id-b");
        assert!((hits[0].1 - 1.0).abs() < 1e-4);
        assert!(hits[0].1 >= hits[1].1);

        // Query of the wrong width returns empty, no panic.
        assert!(idx.search(&[1.0, 0.0], 2).is_empty());

        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn id_count_mismatch_errors() {
        let dim = 2usize;
        let dir = std::env::temp_dir().join(format!("tilt-index-mismatch-{}", std::process::id()));
        fs::create_dir_all(&dir).unwrap();
        write_le(&dir.join("embeddings.f32"), &[1.0, 0.0, 0.0, 1.0]); // 2 rows
        fs::write(&dir.join("embeddings.ids"), "only-one\n").unwrap(); // 1 id
        let err = Index::open_with_dim(&dir, dim).unwrap_err();
        assert!(err.to_string().contains("mismatch"), "{err}");
        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn load_query_size_check() {
        let dir = std::env::temp_dir().join(format!("tilt-query-test-{}", std::process::id()));
        fs::create_dir_all(&dir).unwrap();
        let qp = dir.join("query.f32");
        write_le(&qp, &[0.5, 0.5, 0.5, 0.5]);
        let q = load_query(&qp, 4).unwrap();
        assert_eq!(q, vec![0.5, 0.5, 0.5, 0.5]);
        // Wrong dim -> error, not panic.
        assert!(load_query(&qp, 8).is_err());
        fs::remove_dir_all(&dir).ok();
    }
}
