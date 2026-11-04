//! Pinned, verified model provisioning and in-process CLS embedding.
use crate::{
    data::DataRoot,
    semantic_bert::{checkpoint_name, Config, MappedWeights, ModernBert},
    semantic_tokenizer::{self, TextTokenizer},
};
use anyhow::{anyhow, bail, Context, Result};
use candle_core::{DType, Device, IndexOp, Tensor};
use candle_nn::VarBuilder;
use sha2::{Digest, Sha256};
use std::{
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    path::{Path, PathBuf},
};
use unicode_normalization::UnicodeNormalization;

pub const MODEL_ID: &str = "granite-embedding-small-english-r2";
pub const REVISION: &str = "2ab6fa8ea2d674564defd37171ae19079b864b33";
pub const DIM: usize = 384;
pub const FILES: [(&str, u64, &str); 3] = [
    (
        "config.json",
        1315,
        "154518e6452854eb11123ce433d165f2773437aa0f36935d2204249b2c327198",
    ),
    (
        "tokenizer.json",
        3583228,
        "6c8aaa9a542084f2457eab775d4eeb51f92a70c0fd9de28d5edb0ddec3c08d30",
    ),
    (
        "model.safetensors",
        95332048,
        "dcbfaf2ee50d358763cbc0c08e5b045ecbf6aeb02dc0e86ffb910029bf9ebb5f",
    ),
];
pub fn directory(root: &DataRoot) -> PathBuf {
    std::env::var_os("AGREP_MODEL_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|| root.path().join("model").join(MODEL_ID))
}
pub fn installed(root: &DataRoot) -> bool {
    installed_at(&directory(root))
}
pub fn installed_at(path: &Path) -> bool {
    FILES.iter().all(|(name, len, _)| {
        path.join(name)
            .metadata()
            .is_ok_and(|m| m.is_file() && m.len() == *len)
    })
}

/// Start read-ahead before freshness and metadata work. The mapping owns no
/// mutable data and is only a performance hint; normal load verification still
/// decides which bytes may be used by inference. Darwin pages a WillNeed range
/// in before returning, so the hint runs on its own thread.
pub fn prefetch_query(root: &DataRoot) -> Option<std::thread::JoinHandle<()>> {
    #[cfg(unix)]
    {
        let path = directory(root);
        if !installed_at(&path) {
            return None;
        }
        std::thread::Builder::new()
            .name("agrep-model-read-ahead".into())
            .spawn(move || {
                let _span = crate::semantic_profile::span("model_read_ahead");
                let Ok(source) = stamps(&path, &FILES) else {
                    return;
                };
                // A query reads converted weights in place, apart from most of the token
                // embedding table; without them it copies the whole source checkpoint.
                let (weights, source_map);
                let (map, ranges) = if conversion_matches(&path, &source) {
                    let Ok(mapped) = MappedWeights::open(&path.join("weights-f32.safetensors"))
                    else {
                        return;
                    };
                    weights = mapped;
                    (weights.mapping(), weights.query_ranges())
                } else {
                    let Ok(file) = File::open(path.join("model.safetensors")) else {
                        return;
                    };
                    let Ok(map) = (unsafe { memmap2::Mmap::map(&file) }) else {
                        return;
                    };
                    source_map = map;
                    (&source_map, std::iter::once(0..source_map.len()).collect())
                };
                read_ahead(map, &ranges);
            })
            .ok()
    }
    #[cfg(not(unix))]
    {
        let _ = root;
        None
    }
}

/// Requests the ranges only when a sample of their pages is not resident: checking
/// every page of a hot model costs more than the rare partial eviction it would catch,
/// and pages missed by the sample are still read by the faults that need them.
#[cfg(unix)]
fn read_ahead(map: &memmap2::Mmap, ranges: &[std::ops::Range<usize>]) {
    const SAMPLES: usize = 256;
    let page = unsafe { libc::sysconf(libc::_SC_PAGESIZE) }.max(1) as usize;
    let bytes: usize = ranges.iter().map(|r| r.len()).sum();
    let mut sampled = 0;
    let mut resident = 0;
    for range in ranges {
        let count = (SAMPLES * range.len()).div_ceil(bytes.max(1)).max(1);
        for k in 0..count {
            let offset = (range.start + range.len() * k / count) / page * page;
            let mut state = 0u8;
            // mincore only observes this mapping and writes one byte for its page.
            let known = unsafe {
                libc::mincore(
                    map.as_ptr().add(offset).cast_mut().cast(),
                    1,
                    (&mut state as *mut u8).cast(),
                ) == 0
            };
            sampled += 1;
            resident += usize::from(known && state & 1 != 0);
        }
    }
    let requested = resident < sampled;
    if requested {
        for range in ranges {
            let _ = map.advise_range(memmap2::Advice::WillNeed, range.start, range.len());
        }
    }
    crate::semantic_profile::event(serde_json::json!({"model_read_ahead":{
        "bytes":bytes,"requested":requested,"sampled_pages":sampled,"resident_pages":resident
    }}));
}

/// Verify an installed artifact set, reusing only unchanged verified stamps.
pub fn verify_installed(root: &DataRoot) -> Result<()> {
    verify_installed_at(&directory(root))
}
pub fn verify_installed_at(path: &Path) -> Result<()> {
    if !cached_verification(path, &FILES) {
        let before = stamps(path, &FILES)?;
        for (name, size, hash) in FILES {
            verify(&path.join(name), size, hash)?;
        }
        if before != stamps(path, &FILES)? {
            bail!("model changed during SHA-256 verification");
        }
    }
    Ok(())
}
#[derive(serde::Serialize, serde::Deserialize, PartialEq, Eq)]
struct ArtifactStamp {
    name: String,
    sha256: String,
    bytes: u64,
    modified_ns: u128,
    identity: String,
}
#[derive(serde::Serialize, serde::Deserialize, PartialEq, Eq)]
struct Verified {
    revision: String,
    files: Vec<ArtifactStamp>,
}
#[derive(serde::Serialize, serde::Deserialize)]
struct Converted {
    version: u8,
    source: Verified,
    output: Verified,
}

fn conversion_matches(path: &Path, source: &Verified) -> bool {
    let marker = path.join("converted.json");
    if !marker.metadata().is_ok_and(|m| m.len() <= 8192) {
        return false;
    }
    let Ok(bytes) = fs::read(marker) else {
        return false;
    };
    let Ok(record) = serde_json::from_slice::<Converted>(&bytes) else {
        return false;
    };
    let expected = [
        "weights-f32.safetensors",
        "weights-f16.safetensors",
        "tokenizer-compact.json",
        "tokenizer-query.bin",
    ];
    if record.version != 2
        || record.source != *source
        || record
            .output
            .files
            .iter()
            .map(|s| s.name.as_str())
            .ne(expected)
    {
        return false;
    }
    let files: Vec<_> = record
        .output
        .files
        .iter()
        .map(|s| (s.name.as_str(), s.bytes, s.sha256.as_str()))
        .collect();
    stamps(path, &files).is_ok_and(|now| now == record.output)
}

fn convert_weights(path: &Path) -> Result<()> {
    let source = stamps(path, &FILES)?;
    if conversion_matches(path, &source) {
        return Ok(());
    }
    let mut files = Vec::new();
    for (name, dtype) in [
        ("weights-f32.safetensors", DType::F32),
        ("weights-f16.safetensors", DType::F16),
    ] {
        let tensors = candle_core::safetensors::load(path.join("model.safetensors"), &Device::Cpu)?;
        let tensors = tensors
            .into_iter()
            .map(|(k, v)| Ok((k, v.to_dtype(dtype)?)))
            .collect::<candle_core::Result<std::collections::HashMap<_, _>>>()?;
        let temp = path.join(format!("{name}.partial"));
        candle_core::safetensors::save(&tensors, &temp)?;
        OpenOptions::new().write(true).open(&temp)?.sync_all()?;
        fs::rename(temp, path.join(name))?;
        files.push((
            name,
            path.join(name).metadata()?.len(),
            digest(&path.join(name))?,
        ));
    }
    let name = "tokenizer-compact.json";
    let json: serde_json::Value = serde_json::from_slice(&fs::read(path.join("tokenizer.json"))?)?;
    let temp = path.join(format!("{name}.partial"));
    let mut file = File::create(&temp)?;
    file.write_all(&serde_json::to_vec(&json)?)?;
    file.sync_all()?;
    drop(file);
    fs::rename(temp, path.join(name))?;
    files.push((
        name,
        path.join(name).metadata()?.len(),
        digest(&path.join(name))?,
    ));
    let name = "tokenizer-query.bin";
    semantic_tokenizer::write_query(&path.join("tokenizer.json"), &path.join(name))?;
    files.push((
        name,
        path.join(name).metadata()?.len(),
        digest(&path.join(name))?,
    ));
    let tuples: Vec<_> = files.iter().map(|(n, b, h)| (*n, *b, h.as_str())).collect();
    let output = stamps(path, &tuples)?;
    if stamps(path, &FILES)? != source {
        bail!("model changed during weight conversion");
    }
    let record = Converted {
        version: 2,
        source,
        output,
    };
    let temp = path.join("converted.json.partial");
    let mut file = File::create(&temp)?;
    file.write_all(&serde_json::to_vec(&record)?)?;
    file.sync_all()?;
    drop(file);
    fs::rename(temp, path.join("converted.json"))?;
    #[cfg(unix)]
    File::open(path)?.sync_all()?;
    Ok(())
}
fn stamps(path: &Path, files: &[(&str, u64, &str)]) -> Result<Verified> {
    let mut out = Vec::new();
    for &(name, bytes, sha256) in files {
        let meta = path.join(name).metadata()?;
        if !meta.is_file() || meta.len() != bytes {
            bail!("incorrect model file size: {}", path.join(name).display());
        }
        #[cfg(unix)]
        let identity = {
            use std::os::unix::fs::MetadataExt;
            format!(
                "{}:{}:{}:{}",
                meta.dev(),
                meta.ino(),
                meta.ctime(),
                meta.ctime_nsec()
            )
        };
        #[cfg(windows)]
        let identity = {
            use std::os::windows::fs::MetadataExt;
            format!(
                "{}:{}:{}",
                meta.creation_time(),
                meta.last_write_time(),
                meta.file_attributes()
            )
        };
        #[cfg(not(any(unix, windows)))]
        let identity = String::new();
        out.push(ArtifactStamp {
            name: name.into(),
            sha256: sha256.into(),
            bytes,
            modified_ns: meta
                .modified()?
                .duration_since(std::time::UNIX_EPOCH)?
                .as_nanos(),
            identity,
        });
    }
    Ok(Verified {
        revision: REVISION.into(),
        files: out,
    })
}
fn cached_verification(path: &Path, files: &[(&str, u64, &str)]) -> bool {
    let marker = path.join("verified.json");
    if !marker.metadata().is_ok_and(|m| m.len() <= 8192) {
        return false;
    }
    let Ok(bytes) = fs::read(marker) else {
        return false;
    };
    let Ok(record) = serde_json::from_slice::<Verified>(&bytes) else {
        return false;
    };
    stamps(path, files).is_ok_and(|now| now == record)
}
fn record_verification(path: &Path, files: &[(&str, u64, &str)]) -> Result<()> {
    let before = stamps(path, files)?;
    for &(name, bytes, sha) in files {
        verify(&path.join(name), bytes, sha)?;
    }
    let after = stamps(path, files)?;
    if before != after {
        bail!("model changed during SHA-256 verification");
    }
    let temp = path.join("verified.json.partial");
    let mut f = File::create(&temp)?;
    f.write_all(&serde_json::to_vec(&after)?)?;
    f.sync_all()?;
    drop(f);
    fs::rename(temp, path.join("verified.json"))?;
    #[cfg(unix)]
    File::open(path)?.sync_all()?;
    Ok(())
}
fn verify(path: &Path, size: u64, expected: &str) -> Result<()> {
    let file = File::open(path)?;
    if file.metadata()?.len() != size {
        bail!("incorrect model file size: {}", path.display());
    }
    if digest_file(file)? != expected {
        bail!("model SHA-256 mismatch: {}", path.display());
    }
    Ok(())
}
fn digest(path: &Path) -> Result<String> {
    digest_file(File::open(path)?)
}
fn digest_file(mut file: File) -> Result<String> {
    let mut hash = Sha256::new();
    let mut buffer = [0u8; 65536];
    loop {
        let n = file.read(&mut buffer)?;
        if n == 0 {
            break;
        }
        hash.update(&buffer[..n]);
    }
    Ok(crate::store::hex_digest(hash.finalize()))
}
/// Only explicit setup, index --semantic and -s callers may invoke this function.
/// Network failures preserve already verified files and never publish partial bytes.
pub fn provision(root: &DataRoot) -> Result<()> {
    if crate::data::read_only() {
        bail!("data directory is read-only");
    }
    provision_at(&directory(root))
}
pub fn provision_at(path: &Path) -> Result<()> {
    crate::data::private_dir(path)?;
    if let Some(parent) = path
        .parent()
        .filter(|p| p.file_name().is_some_and(|s| s == "model"))
    {
        crate::data::private_dir(parent)?;
    }
    let lock = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .open(path.join("download.lock"))?;
    lock.lock()?;
    let agent: ureq::Agent = ureq::Agent::config_builder()
        .tls_config(
            ureq::tls::TlsConfig::builder()
                .unversioned_rustls_crypto_provider(std::sync::Arc::new(
                    rustls_rustcrypto::provider(),
                ))
                .build(),
        )
        .build()
        .into();
    for (name, size, digest) in FILES {
        let dest = path.join(name);
        if dest.exists() {
            verify(&dest, size, digest)?;
            continue;
        }
        let temp = path.join(format!("{name}.partial"));
        let outcome = (|| -> Result<()> {
            let url =
                format!("https://huggingface.co/ibm-granite/{MODEL_ID}/resolve/{REVISION}/{name}");
            let mut response = agent
                .get(&url)
                .call()
                .with_context(|| format!("download {name}"))?;
            let mut file = File::create(&temp)?;
            let n = std::io::copy(
                &mut response.body_mut().as_reader().take(size + 1),
                &mut file,
            )?;
            if n != size {
                bail!("incorrect model download size: {name}");
            }
            file.flush()?;
            file.sync_all()?;
            drop(file);
            verify(&temp, size, digest)?;
            fs::rename(&temp, &dest)?;
            Ok(())
        })();
        if outcome.is_err() {
            let _ = fs::remove_file(&temp);
        }
        outcome?;
    }
    record_verification(path, &FILES)?;
    convert_weights(path)?;
    Ok(())
}

pub struct Embedder {
    tok: TextTokenizer,
    model: ModernBert,
    device: Device,
    token_budget: usize,
    attention_budget: usize,
    max_padding: usize,
    mapped: bool,
}
fn batch_end(
    ids: &[Vec<u32>],
    order: &[usize],
    start: usize,
    token_budget: usize,
    attention_budget: usize,
    max_padding: usize,
) -> usize {
    let mut end = start + 1;
    let mut real_tokens = ids[order[start]].len();
    while end < order.len()
        && (end - start + 1) * ids[order[end]].len() <= token_budget
        && (end - start + 1) * ids[order[end]].len().pow(2) <= attention_budget
        && (end - start + 1) * ids[order[end]].len() * (100 - max_padding)
            <= (real_tokens + ids[order[end]].len()) * 100
    {
        real_tokens += ids[order[end]].len();
        end += 1;
    }
    end
}
impl Embedder {
    /// Bulk embedding: every weight is copied into tensors once per process.
    pub fn load(path: &Path, device: Device) -> Result<Self> {
        Self::open(path, device, false)
    }
    /// The query path's F32 CPU model. Converted projection and embedding weights
    /// are used where the file is mapped instead of being copied; the arithmetic
    /// is the bulk CPU model's, bit for bit.
    pub fn load_query(path: &Path) -> Result<Self> {
        Self::open(path, Device::Cpu, true)
    }
    fn open(path: &Path, device: Device, query: bool) -> Result<Self> {
        let _span = crate::semantic_profile::span("model_load");
        let load_start = std::time::Instant::now();
        let verified = cached_verification(path, &FILES);
        {
            let _span =
                (!verified).then(|| crate::semantic_profile::span("model_hash_verification"));
            verify_installed_at(path)?;
        }
        let verified_ms = load_start.elapsed().as_secs_f64() * 1000.;
        let converted = conversion_matches(path, &stamps(path, &FILES)?);
        let mapped = query && converted;
        crate::semantic_profile::event(serde_json::json!({"model_cache":{
            "verified":verified,"converted":converted,"writes":0,"metal":device.is_metal(),"mapped":mapped
        }}));
        let dtype = if device.is_metal() {
            DType::F16
        } else {
            DType::F32
        };
        let (model, tok, weights_ms, tokenizer_ms) = std::thread::scope(|scope| -> Result<_> {
            let tokenizer = scope.spawn(|| -> Result<_> {
                let _span = crate::semantic_profile::span("tokenizer_load");
                let start = std::time::Instant::now();
                // Explicit provisioning writes the query tables beside the other
                // converted files; bulk embedding builds the library's model.
                let mut tok = if query && converted {
                    TextTokenizer::Query(Box::new(semantic_tokenizer::load_query(
                        &path.join("tokenizer-query.bin"),
                    )?))
                } else {
                    TextTokenizer::Library(Box::new(semantic_tokenizer::load(&path.join(
                        if converted {
                            "tokenizer-compact.json"
                        } else {
                            "tokenizer.json"
                        },
                    ))?))
                };
                tok.window(1024)?;
                Ok((tok, start.elapsed().as_secs_f64() * 1000.))
            });
            let start = std::time::Instant::now();
            let config: Config = serde_json::from_slice(&fs::read(path.join("config.json"))?)?;
            // Verified source, or metadata-bound output of explicit provisioning.
            let vb = unsafe {
                let name = if converted {
                    if dtype == DType::F16 {
                        "weights-f16.safetensors"
                    } else {
                        "weights-f32.safetensors"
                    }
                } else {
                    "model.safetensors"
                };
                VarBuilder::from_mmaped_safetensors(&[path.join(name)], dtype, &device)?
            }
            .rename_f(|name| checkpoint_name(name).to_owned());
            let (model, weights) = {
                let _span = crate::semantic_profile::span("weights_load");
                let weights = mapped
                    .then(|| MappedWeights::open(&path.join("weights-f32.safetensors")))
                    .transpose()?;
                (ModernBert::load(vb, &config, weights.as_ref())?, weights)
            };
            if crate::semantic_profile::enabled() {
                device.synchronize()?;
            }
            let weights_ms = start.elapsed().as_secs_f64() * 1000.;
            if let Some(weights) = weights {
                // Otherwise idle while the tokenizer loads on its own thread.
                let _span = crate::semantic_profile::span("weights_prefault");
                weights.prefault();
            }
            let (tok, tokenizer_ms) = tokenizer
                .join()
                .map_err(|_| anyhow!("tokenizer loading failed"))??;
            Ok((model, tok, weights_ms, tokenizer_ms))
        })?;
        if crate::semantic_profile::timing_enabled() {
            if crate::semantic_profile::enabled() {
                device.synchronize()?;
            }
            eprintln!(
                "{}",
                serde_json::json!({"semantic_load":{"verify_ms":verified_ms,"tokenizer_ms":tokenizer_ms,"weights_ms":weights_ms,"total_ms":load_start.elapsed().as_secs_f64()*1000.}})
            );
        }
        // Amortize matmuls in the bounded CPU pool and persistent Metal arena; the
        // attention cap independently bounds long-row work.
        let token_budget = 16384;
        let attention_budget = if device.is_metal() {
            8 * 1024 * 1024
        } else {
            4 * 1024 * 1024
        };
        Ok(Self {
            tok,
            model,
            device,
            token_budget,
            attention_budget,
            max_padding: 25,
            mapped,
        })
    }
    /// Whether projection and embedding weights are used in place in the mapped file.
    #[doc(hidden)]
    pub fn mapped(&self) -> bool {
        self.mapped
    }
    pub fn query(root: &DataRoot) -> Result<Self> {
        Self::load_query(&directory(root))
    }
    pub fn background(root: &DataRoot) -> Result<Self> {
        Self::load(
            &directory(root),
            crate::semantic_device::background_device(),
        )
    }
    /// Experimental window comparison for the standalone quality/performance harness.
    /// Product callers use the fixed 1024-token contract from `load`.
    pub fn with_token_limit(mut self, limit: usize) -> Result<Self> {
        if !(2..=1024).contains(&limit) {
            bail!("token limit must be 2..=1024");
        }
        self.tok.window(limit)?;
        Ok(self)
    }
    pub fn token_ids(&self, text: &str) -> Result<Vec<u32>> {
        self.tok.ids(text)
    }
    /// Scheduling-only sweep for the standalone performance harness.
    pub fn with_batch_tokens(mut self, tokens: usize) -> Result<Self> {
        if tokens == 0 {
            bail!("batch token count must be positive");
        }
        self.token_budget = tokens;
        Ok(self)
    }
    #[doc(hidden)]
    pub fn with_max_padding(mut self, percent: usize) -> Result<Self> {
        if percent > 25 {
            bail!("padding experiment may only tighten the existing bound")
        }
        self.max_padding = percent;
        Ok(self)
    }
    pub fn embed(&self, texts: &[String]) -> Result<Vec<Vec<f32>>> {
        if self.device.is_cpu() && texts.len() >= 32 {
            // Both streams use the same bounded Rayon pool. While one reaches
            // a serial operation, the other can occupy an otherwise idle core.
            // Stripe rows so a length-ordered source cannot leave one stream
            // with all the long rows. Each stream still buckets by token count.
            let even = texts.iter().step_by(2).collect::<Vec<_>>();
            let odd = texts.iter().skip(1).step_by(2).collect::<Vec<_>>();
            let (left, right) = rayon::join(|| self.embed_inner(&even), || self.embed_inner(&odd));
            let mut left = left?.into_iter();
            let mut right = right?.into_iter();
            let output = (0..texts.len())
                .map(|i| {
                    if i % 2 == 0 {
                        left.next().unwrap()
                    } else {
                        right.next().unwrap()
                    }
                })
                .collect();
            return Ok(output);
        }
        self.embed_inner(texts)
    }
    fn embed_inner(&self, texts: &[impl AsRef<str>]) -> Result<Vec<Vec<f32>>> {
        let started = std::time::Instant::now();
        let ids: Vec<_> = {
            let _span = crate::semantic_profile::span("tokenize");
            texts
                .iter()
                .map(|s| self.token_ids(s.as_ref()))
                .collect::<Result<_>>()?
        };
        if crate::semantic_profile::enabled() {
            eprintln!(
                "{}",
                serde_json::json!({"tokenize_ms":started.elapsed().as_secs_f64()*1000.,"rows":texts.len(),"tokens":ids.iter().map(Vec::len).sum::<usize>()})
            );
        }
        let mut order: Vec<_> = (0..texts.len()).collect();
        order.sort_by_key(|&i| ids[i].len());
        let mut out = vec![vec![]; texts.len()];
        self.model.prepare_arena(self.token_budget.max(1024))?;
        let mut start = 0;
        while start < order.len() {
            let end = batch_end(
                &ids,
                &order,
                start,
                self.token_budget,
                self.attention_budget,
                self.max_padding,
            );
            let batch = &order[start..end];
            let width = batch.iter().map(|&i| ids[i].len()).max().unwrap();
            if crate::semantic_profile::enabled() {
                eprintln!(
                    "{}",
                    serde_json::json!({"batch_rows":batch.len(),"padded_tokens":batch.len()*width,"tokens":batch.iter().map(|&i|ids[i].len()).sum::<usize>()})
                );
            }
            let mut padded = vec![50283u32; batch.len() * width];
            let mut mask = vec![0u32; padded.len()];
            for (j, &i) in batch.iter().enumerate() {
                padded[j * width..j * width + ids[i].len()].copy_from_slice(&ids[i]);
                mask[j * width..j * width + ids[i].len()].fill(1);
            }
            let xs = Tensor::from_vec(padded, (batch.len(), width), &self.device)?;
            let mask = Tensor::from_vec(mask, (batch.len(), width), &self.device)?;
            let _span = crate::semantic_profile::span("query_inference");
            let lengths = batch.iter().map(|i| ids[*i].len()).collect::<Vec<_>>();
            let cls = if let Some(values) = self.model.arena_embeddings(&xs, &lengths)? {
                values
            } else {
                self.model
                    .forward(&xs, &mask, &lengths)?
                    .i((.., 0, ..))?
                    .to_dtype(DType::F32)?
                    .to_vec2::<f32>()?
            };
            for (&i, mut row) in batch.iter().zip(cls) {
                let norm = row.iter().map(|x| x * x).sum::<f32>().sqrt();
                if row.len() != DIM || !norm.is_finite() || norm <= 1e-12 {
                    bail!("invalid embedding norm for row {i}");
                }
                row.iter_mut().for_each(|x| *x /= norm);
                out[i] = row;
            }
            start = end;
        }
        Ok(out)
    }
}

/// Reference byte-budgeted, overlapping character windows, with first-eight plus
/// uniformly sampled tail when a single row exceeds 32 chunks.
pub fn chunks(agent: &str, who: &str, text: &str) -> Vec<String> {
    let text = if agent == "pi" && who == "recap" {
        strip_recap(text)
    } else {
        text
    };
    let chars: Vec<_> = text.chars().collect();
    let total = chars.len();
    let mut spans = Vec::new();
    let mut start = 0;
    loop {
        let mut end = (start + 1022).min(total);
        loop {
            if end < total {
                if let Some(i) = (start + (end - start) / 2..end).rfind(|&i| chars[i] == '\n') {
                    end = i + 1;
                }
            }
            let chunk: String = chars[start..end].iter().collect();
            let bytes = chunk.nfc().collect::<String>().len();
            if bytes <= 1022 {
                break;
            }
            end = start + (end - start) * 1022 / bytes;
        }
        spans.push((start, end));
        if end >= total {
            break;
        }
        start = end - 102.min((end - start) / 2);
    }
    let selected: Vec<_> = if spans.len() <= 32 {
        spans
    } else {
        spans[..8]
            .iter()
            .copied()
            .chain((0..24).map(|i| spans[8 + i * (spans.len() - 9) / 23]))
            .collect()
    };
    selected
        .into_iter()
        .map(|(a, b)| chars[a..b].iter().collect())
        .collect()
}
fn strip_recap(text: &str) -> &str {
    static HEADING: std::sync::LazyLock<regex::Regex> = std::sync::LazyLock::new(|| {
        regex::Regex::new(r"(?m)^[^\S\n]*\S[^\n]{0,80}\n={3,}[^\S\n]*$").unwrap()
    });
    let end = text.char_indices().nth(4096).map_or(text.len(), |(i, _)| i);
    HEADING
        .find(&text[..end])
        .map_or(text, |m| &text[m.start()..])
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn read_ahead_is_optional_and_does_not_provision_a_missing_model() {
        let temp = tempfile::tempdir().unwrap();
        let root = DataRoot::open(temp.path().join("data"), true).unwrap();
        assert!(prefetch_query(&root).is_none());
        assert!(!directory(&root).exists());
    }
    #[test]
    fn batching_separates_long_rows_without_changing_the_window() {
        let ids: Vec<_> = [8, 8, 32, 32, 1024, 1024]
            .into_iter()
            .map(|n| vec![1; n])
            .collect();
        let order: Vec<_> = (0..ids.len()).collect();
        assert_eq!(batch_end(&ids, &order, 0, 16384, 4194304, 25), 2);
        assert_eq!(batch_end(&ids, &order, 2, 16384, 4194304, 25), 4);
        assert_eq!(batch_end(&ids, &order, 4, 16384, 4194304, 25), 6);
        assert_eq!(batch_end(&ids, &order, 4, 1024, 4194304, 25), 5);
    }
    #[test]
    fn tighter_padding_splits_lengths_and_keeps_full_rows() {
        let ids = vec![vec![1; 10], vec![1; 14], vec![1; 1024]];
        let order = vec![0, 1, 2];
        assert_eq!(batch_end(&ids, &order, 0, 16384, 4194304, 25), 2);
        assert_eq!(batch_end(&ids, &order, 0, 16384, 4194304, 5), 1);
        assert_eq!(batch_end(&ids, &order, 2, 16384, 4194304, 5), 3);
    }
    #[test]
    fn converted_cache_is_bound_to_all_sources_and_outputs() {
        let t = tempfile::tempdir().unwrap();
        fs::write(t.path().join("source"), b"abc").unwrap();
        let inputs = [("source", 3, "source-hash")];
        let outputs = [
            ("weights-f32.safetensors", 3, "a"),
            ("weights-f16.safetensors", 3, "b"),
            ("tokenizer-compact.json", 3, "c"),
            ("tokenizer-query.bin", 3, "d"),
        ];
        for (name, _, _) in outputs {
            fs::write(t.path().join(name), b"123").unwrap();
        }
        let record = Converted {
            version: 2,
            source: stamps(t.path(), &inputs).unwrap(),
            output: stamps(t.path(), &outputs).unwrap(),
        };
        fs::write(
            t.path().join("converted.json"),
            serde_json::to_vec(&record).unwrap(),
        )
        .unwrap();
        let source = stamps(t.path(), &inputs).unwrap();
        assert!(conversion_matches(t.path(), &source));
        // A first-format record, without query tokenizer tables, is converted again.
        let old = Converted {
            version: 1,
            source: stamps(t.path(), &inputs).unwrap(),
            output: stamps(t.path(), &outputs[..3]).unwrap(),
        };
        fs::write(
            t.path().join("converted.json"),
            serde_json::to_vec(&old).unwrap(),
        )
        .unwrap();
        assert!(!conversion_matches(t.path(), &source));
        fs::write(
            t.path().join("converted.json"),
            serde_json::to_vec(&record).unwrap(),
        )
        .unwrap();
        assert!(conversion_matches(t.path(), &source));
        fs::write(t.path().join("weights-f32.safetensors"), b"changed").unwrap();
        assert!(!conversion_matches(t.path(), &source));
        fs::write(t.path().join("weights-f32.safetensors"), b"123").unwrap();
        let record = Converted {
            version: 2,
            source: stamps(t.path(), &inputs).unwrap(),
            output: stamps(t.path(), &outputs).unwrap(),
        };
        fs::write(
            t.path().join("converted.json"),
            serde_json::to_vec(&record).unwrap(),
        )
        .unwrap();
        assert!(conversion_matches(t.path(), &source));
        fs::write(t.path().join("source"), b"other").unwrap();
        assert!(!conversion_matches(
            t.path(),
            &stamps(t.path(), &[("source", 5, "source-hash")]).unwrap()
        ));
    }
    #[test]
    fn reference_chunks() {
        assert_eq!(
            chunks("pi", "recap", "Resume instructions\nFILES\n=====\nwork"),
            ["FILES\n=====\nwork"]
        );
        assert_eq!(chunks("codex", "recap", "preamble"), ["preamble"]);
        assert_eq!(chunks("pi", "user", ""), [""]);
        for text in ["x".repeat(50000), "你好🦀e\u{301}".repeat(5000)] {
            let c = chunks("pi", "user", &text);
            assert_eq!(c.len(), 32);
            assert!(c.iter().all(|s| s.nfc().collect::<String>().len() <= 1022));
            assert!(text.ends_with(c.last().unwrap()));
        }
        let c = chunks("pi", "user", &("a".repeat(700) + "\n" + &"b".repeat(700)));
        assert_eq!(c[0].len(), 701);
        assert_eq!(
            c[1].chars().take(102).collect::<String>(),
            "a".repeat(101) + "\n"
        );
    }
    #[test]
    fn changed_model_bytes_invalidate_the_verified_metadata_cache() {
        let t = tempfile::tempdir().unwrap();
        let p = t.path().join("weights");
        fs::write(&p, b"abc").unwrap();
        let sha = crate::store::digest("abc");
        let files = [("weights", 3, sha.as_str())];
        assert!(!cached_verification(t.path(), &files));
        record_verification(t.path(), &files).unwrap();
        assert!(cached_verification(t.path(), &files));
        fs::write(&p, b"abd").unwrap();
        File::options()
            .write(true)
            .open(&p)
            .unwrap()
            .set_modified(std::time::UNIX_EPOCH + std::time::Duration::from_secs(123))
            .unwrap();
        assert!(!cached_verification(t.path(), &files));
        assert!(verify(&p, 3, &sha).is_err());
    }
    #[test]
    fn hash_verification_rejects_corruption() {
        let t = tempfile::tempdir().unwrap();
        let p = t.path().join("x");
        fs::write(&p, b"ok").unwrap();
        verify(&p, 2, &crate::store::digest("ok")).unwrap();
        assert!(verify(&p, 2, &crate::store::digest("no")).is_err());
        assert!(verify(&p, 3, "").is_err());
    }
}
