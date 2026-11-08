//! Converted F32 weights read in place from the mapped safetensors file, and the
//! projections and embedding table that use either mapped or owned weights.
use candle_core::{CpuStorage, Layout, Result, Shape, Tensor};
use candle_nn::{linear_no_bias, Embedding, Linear, VarBuilder};
use serde::Deserialize;
use std::{collections::HashMap, sync::Arc};

/// The checkpoint stores the backbone's tensors without the `model.` prefix.
pub(crate) fn checkpoint_name(name: &str) -> &str {
    name.strip_prefix("model.").unwrap_or(name)
}

/// F32 tensors of a converted safetensors file, used where the file is mapped.
pub(crate) struct MappedWeights {
    map: Arc<memmap2::Mmap>,
    tensors: HashMap<String, (Vec<usize>, std::ops::Range<usize>)>,
}

impl MappedWeights {
    pub(crate) fn open(path: &std::path::Path) -> Result<Self> {
        #[derive(Deserialize)]
        struct Entry {
            dtype: String,
            shape: Vec<usize>,
            data_offsets: [usize; 2],
        }
        let invalid = || candle_core::Error::Msg("invalid converted weights".into());
        if !cfg!(target_endian = "little") {
            return Err(invalid());
        }
        let file = std::fs::File::open(path)?;
        // Provisioning replaces converted files only by renaming new ones over them,
        // so a mapping keeps the bytes that were verified when it was opened.
        let map = unsafe { memmap2::Mmap::map(&file)? };
        let length = map
            .get(..8)
            .map(|b| u64::from_le_bytes(b.try_into().unwrap()) as usize)
            .ok_or_else(invalid)?;
        let start = length
            .checked_add(8)
            .filter(|s| *s <= map.len())
            .ok_or_else(invalid)?;
        let header: HashMap<String, serde_json::Value> =
            serde_json::from_slice(&map[8..start]).map_err(|_| invalid())?;
        let mut tensors = HashMap::with_capacity(header.len());
        for (name, entry) in header {
            if name == "__metadata__" {
                continue;
            }
            let entry: Entry = serde_json::from_value(entry).map_err(|_| invalid())?;
            let [begin, end] = entry.data_offsets;
            let range = start + begin..start + end;
            let elements = entry.shape.iter().product::<usize>();
            if entry.dtype != "F32"
                || begin > end
                || range.end > map.len()
                || end - begin != elements * 4
                || !(map.as_ptr() as usize + range.start).is_multiple_of(4)
            {
                return Err(invalid());
            }
            tensors.insert(name, (entry.shape, range));
        }
        Ok(Self {
            map: Arc::new(map),
            tensors,
        })
    }
    #[cfg(unix)]
    pub(crate) fn mapping(&self) -> &memmap2::Mmap {
        &self.map
    }
    /// Byte ranges of every tensor a query reads, adjacent ones merged. A query
    /// reads only a few rows of the token embedding table, which is left out.
    pub(crate) fn query_ranges(&self) -> Vec<std::ops::Range<usize>> {
        let table = checkpoint_name("model.embeddings.tok_embeddings.weight");
        let mut ranges: Vec<_> = self
            .tensors
            .iter()
            .filter(|(name, _)| *name != table)
            .map(|(_, (_, range))| range.clone())
            .collect();
        ranges.sort_by_key(|r| r.start);
        let mut merged: Vec<std::ops::Range<usize>> = Vec::with_capacity(ranges.len());
        for range in ranges {
            match merged.last_mut() {
                Some(last) if last.end == range.start => last.end = range.end,
                _ => merged.push(range),
            }
        }
        merged
    }
    /// Maps the pages of `query_ranges` into this process ahead of inference, so
    /// its projections run without page faults.
    pub(crate) fn prefault(&self) {
        let mut sum = 0u8;
        for range in self.query_ranges() {
            #[cfg(target_os = "linux")]
            if self
                .map
                .advise_range(memmap2::Advice::PopulateRead, range.start, range.len())
                .is_ok()
            {
                continue;
            }
            for offset in range.step_by(4096) {
                sum ^= self.map[offset];
            }
        }
        std::hint::black_box(sum);
    }
    pub(super) fn get(&self, vb: &VarBuilder, shape: &[usize]) -> Result<MappedTensor> {
        let name = format!("{}.weight", vb.prefix());
        match self.tensors.get(checkpoint_name(&name)) {
            Some((actual, range)) if actual == shape => Ok(MappedTensor {
                map: self.map.clone(),
                range: range.clone(),
                shape: actual.clone(),
            }),
            _ => Err(candle_core::Error::Msg(format!(
                "converted weights lack {name} {shape:?}"
            ))),
        }
    }
}

#[derive(Clone)]
pub(super) struct MappedTensor {
    map: Arc<memmap2::Mmap>,
    range: std::ops::Range<usize>,
    shape: Vec<usize>,
}
impl MappedTensor {
    fn values(&self) -> &[f32] {
        let bytes = &self.map[self.range.clone()];
        // `MappedWeights::open` checked the F32 dtype, length and alignment of this
        // range on a little-endian target; every bit pattern is a valid f32.
        unsafe { std::slice::from_raw_parts(bytes.as_ptr().cast::<f32>(), bytes.len() / 4) }
    }
}

/// `x · wᵀ` for row-major `x` (m×k) and `w` (n×k), through the same BLAS call
/// and arguments Candle's CPU matmul uses for `Linear`, so results are identical.
/// `gemm` runs on the calling thread: Candle would hand every product of a short
/// query to the Rayon pool and wait for it; `projection` already splits long inputs
/// into row blocks across the pool. Each output element's dot product is computed
/// the same way with or without threads, which `semantic_eval` checks bit for bit.
fn matmul_transposed(x: &[f32], m: usize, k: usize, w: &[f32], n: usize) -> Vec<f32> {
    let mut out = vec![0f32; m * n];
    #[cfg(target_os = "macos")]
    unsafe {
        unsafe extern "C" {
            #[link_name = "sgemm_"]
            fn sgemm(
                transa: *const std::ffi::c_char,
                transb: *const std::ffi::c_char,
                m: *const i32,
                n: *const i32,
                k: *const i32,
                alpha: *const f32,
                a: *const f32,
                lda: *const i32,
                b: *const f32,
                ldb: *const i32,
                beta: *const f32,
                c: *mut f32,
                ldc: *const i32,
            );
        }
        let (m, n, k) = (m as i32, n as i32, k as i32);
        sgemm(
            &(b'T' as std::ffi::c_char),
            &(b'N' as std::ffi::c_char),
            &n,
            &m,
            &k,
            &1.,
            w.as_ptr(),
            &k,
            x.as_ptr(),
            &k,
            &0.,
            out.as_mut_ptr(),
            &n,
        );
    }
    #[cfg(not(target_os = "macos"))]
    unsafe {
        gemm::gemm(
            m,
            n,
            k,
            out.as_mut_ptr(),
            1,
            n as isize,
            false,
            x.as_ptr(),
            1,
            k as isize,
            w.as_ptr(),
            k as isize,
            1,
            0f32,
            1f32,
            false,
            false,
            false,
            gemm::Parallelism::None,
        );
    }
    out
}

/// A bias-free linear layer whose weight stays in the mapped file.
pub(super) struct MappedLinear(MappedTensor);
impl candle_core::CustomOp1 for MappedLinear {
    fn name(&self) -> &'static str {
        "mapped-linear"
    }
    fn cpu_fwd(&self, storage: &CpuStorage, layout: &Layout) -> Result<(CpuStorage, Shape)> {
        let CpuStorage::F32(values) = storage else {
            candle_core::bail!("mapped linear requires F32");
        };
        let (start, end) = layout.contiguous_offsets().ok_or_else(|| {
            candle_core::Error::Msg("mapped linear requires contiguous input".into())
        })?;
        let (n, k) = (self.0.shape[0], self.0.shape[1]);
        let mut dims = layout.dims().to_vec();
        match dims.last_mut() {
            Some(last) if *last == k => *last = n,
            _ => candle_core::bail!("mapped linear input width"),
        }
        let x = &values[start..end];
        let out = matmul_transposed(x, x.len() / k, k, self.0.values(), n);
        Ok((CpuStorage::F32(out), Shape::from(dims)))
    }
}

#[derive(Clone)]
pub(super) enum Projection {
    Owned(Linear),
    Mapped(Arc<MappedLinear>),
}
impl Projection {
    pub(super) fn load(
        in_dim: usize,
        out_dim: usize,
        vb: VarBuilder,
        mapped: Option<&MappedWeights>,
    ) -> Result<Self> {
        Ok(match mapped {
            Some(weights) => Self::Mapped(Arc::new(MappedLinear(
                weights.get(&vb, &[out_dim, in_dim])?,
            ))),
            None => Self::Owned(linear_no_bias(in_dim, out_dim, vb)?),
        })
    }
    pub(super) fn forward(&self, xs: &Tensor) -> Result<Tensor> {
        match self {
            Self::Owned(linear) => xs.apply(linear),
            Self::Mapped(linear) => xs.contiguous()?.apply_op1_no_bwd(linear.as_ref()),
        }
    }
    pub(super) fn outputs(&self) -> Result<usize> {
        match self {
            Self::Owned(linear) => linear.weight().dim(0),
            Self::Mapped(linear) => Ok(linear.0.shape[0]),
        }
    }
    #[cfg(target_os = "macos")]
    pub(super) fn weight(&self) -> Result<&Tensor> {
        match self {
            Self::Owned(linear) => Ok(linear.weight()),
            Self::Mapped(_) => candle_core::bail!("mapped weights stay on the CPU"),
        }
    }
}

#[derive(Clone)]
pub(super) enum Embeddings {
    Owned(Embedding),
    Mapped(MappedTensor),
}
impl Embeddings {
    pub(super) fn forward(&self, ids: &Tensor) -> Result<Tensor> {
        let table = match self {
            Self::Owned(embedding) => return ids.apply(embedding),
            Self::Mapped(table) => table,
        };
        let width = table.shape[1];
        let values = table.values();
        let mut dims = ids.dims().to_vec();
        dims.push(width);
        let indexes = ids.flatten_all()?.to_vec1::<u32>()?;
        let mut rows = Vec::with_capacity(indexes.len() * width);
        for id in indexes {
            let start = id as usize * width;
            rows.extend_from_slice(values.get(start..start + width).ok_or_else(|| {
                candle_core::Error::Msg(format!("token id {id} outside the embedding table"))
            })?);
        }
        Tensor::from_vec(rows, dims, ids.device())
    }
    #[cfg(target_os = "macos")]
    pub(super) fn table(&self) -> Result<&Tensor> {
        match self {
            Self::Owned(embedding) => Ok(embedding.embeddings()),
            Self::Mapped(_) => candle_core::bail!("mapped weights stay on the CPU"),
        }
    }
}
