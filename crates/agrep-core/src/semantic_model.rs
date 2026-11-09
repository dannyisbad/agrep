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
const DOWNLOADS: [(&str, u64, &str); 3] = [
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
/// The complete installed runtime set. Converted bytes are pinned independently
/// of the disposable downloads, so verification never needs a second checkpoint.
pub const FILES: [(&str, u64, &str); 4] = [
    DOWNLOADS[0],
    (
        "weights-f32.safetensors",
        190656984,
        "d0cc63d6a469f4686f1f517fb45c9752eeffe01f490dcc0cf29c9953009ef1fe",
    ),
    (
        "tokenizer-compact.json",
        1571945,
        "f64652c0d4292921662f8a34068ed38c9db4a8e78daae667eaff88a30494ef8b",
    ),
    (
        "tokenizer-query.bin",
        1574699,
        "dc30764c4899f2a90fb9c2759bed05186c7e695ca3f4ae94771b76255f1ddd83",
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
/// A download or conversion stopped partway: some model files are present, but not the
/// complete pinned set [`installed`] requires.
pub fn incomplete(root: &DataRoot) -> bool {
    let path = directory(root);
    !installed_at(&path)
        && fs::read_dir(&path).is_ok_and(|mut entries| {
            entries.any(|e| e.is_ok_and(|e| e.file_name() != "download.lock"))
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
                let Ok(weights) = MappedWeights::open(&path.join("weights-f32.safetensors")) else {
                    return;
                };
                read_ahead(weights.mapping(), &weights.query_ranges());
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
fn publish_converted(path: &Path, artifact: (&str, u64, &str)) -> Result<()> {
    let (name, size, sha) = artifact;
    let temp = path.join(format!("{name}.partial"));
    OpenOptions::new().write(true).open(&temp)?.sync_all()?;
    verify(&temp, size, sha)?;
    fs::rename(temp, path.join(name))?;
    Ok(())
}

fn convert_weights(path: &Path) -> Result<()> {
    let source = stamps(path, &DOWNLOADS)?;
    let tensors = candle_core::safetensors::load(path.join("model.safetensors"), &Device::Cpu)?;
    let tensors = tensors
        .into_iter()
        .map(|(k, v)| Ok((k, v.to_dtype(DType::F32)?)))
        .collect::<candle_core::Result<std::collections::HashMap<_, _>>>()?;
    // Safetensors sorts tensor names before serialization, making the digest stable.
    candle_core::safetensors::save(&tensors, path.join("weights-f32.safetensors.partial"))?;
    publish_converted(path, FILES[1])?;
    let json: serde_json::Value = serde_json::from_slice(&fs::read(path.join("tokenizer.json"))?)?;
    fs::write(
        path.join("tokenizer-compact.json.partial"),
        serde_json::to_vec(&json)?,
    )?;
    publish_converted(path, FILES[2])?;
    semantic_tokenizer::write_query(
        &path.join("tokenizer.json"),
        &path.join("tokenizer-query.bin.partial"),
    )?;
    publish_converted(path, FILES[3])?;
    if stamps(path, &DOWNLOADS)? != source {
        bail!("model changed during weight conversion");
    }
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
/// Installed model bytes that fail their pinned size or SHA-256: only a reinstall changes
/// the verdict, unlike an I/O or loading failure.
#[derive(Debug)]
pub struct Rejected(String);
impl std::fmt::Display for Rejected {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}
impl std::error::Error for Rejected {}
fn verify(path: &Path, size: u64, expected: &str) -> Result<()> {
    let file = File::open(path)?;
    if file.metadata()?.len() != size {
        return Err(Rejected(format!("incorrect model file size: {}", path.display())).into());
    }
    if digest_file(file)? != expected {
        return Err(Rejected(format!("model SHA-256 mismatch: {}", path.display())).into());
    }
    Ok(())
}
/// Whether background work already rejected these exact installed files, so it neither
/// loads them again nor logs the same failure on every scheduled run.
pub fn rejected(root: &DataRoot) -> bool {
    let path = directory(root);
    let Ok(bytes) = fs::read(path.join("rejected.json")) else {
        return false;
    };
    serde_json::from_slice::<Verified>(&bytes)
        .is_ok_and(|record| stamps(&path, &FILES).is_ok_and(|now| now == record))
}
/// Records the installed files' stamps after their verification failed; replacing any of
/// them, as `agrep setup` does, clears the verdict.
pub fn record_rejection(root: &DataRoot) -> Result<()> {
    let path = directory(root);
    crate::safe_fs::write_atomic(
        &path.join("rejected.json"),
        &serde_json::to_vec(&stamps(&path, &FILES)?)?,
        true,
    )
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
/// `progress` hears the bytes downloaded so far and the bytes this install downloads.
pub fn provision(root: &DataRoot, progress: &mut dyn FnMut(u64, u64)) -> Result<()> {
    if crate::data::read_only() {
        bail!("data directory is read-only");
    }
    provision_with(&directory(root), progress)
}
pub fn provision_at(path: &Path) -> Result<()> {
    provision_with(path, &mut |_, _| {})
}
fn provision_with(path: &Path, progress: &mut dyn FnMut(u64, u64)) -> Result<()> {
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
    if verify_installed_at(path).is_err() {
        download_sources(path, progress)?;
        convert_weights(path)?;
    }
    if !cached_verification(path, &FILES) {
        record_verification(path, &FILES)?;
    }
    // Delete the source only after independently pinned runtime files are verified.
    for name in [
        "model.safetensors",
        "tokenizer.json",
        "weights-f16.safetensors",
        "converted.json",
        "weights-f16.safetensors.partial",
        "converted.json.partial",
        "verified.json.partial",
        "rejected.json",
        "tokenizer-query.partial",
        "tokenizer-query.bin.partial.partial",
    ]
    .into_iter()
    .map(str::to_owned)
    .chain(
        DOWNLOADS
            .iter()
            .chain(&FILES)
            .map(|(name, _, _)| format!("{name}.partial")),
    ) {
        match fs::remove_file(path.join(name)) {
            Ok(()) => (),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => (),
            Err(e) => return Err(e.into()),
        }
    }
    #[cfg(unix)]
    File::open(path)?.sync_all()?;
    Ok(())
}

/// Counts the bytes read through it for a progress report.
struct Counted<'a, R> {
    inner: R,
    done: &'a mut u64,
    total: u64,
    progress: &'a mut dyn FnMut(u64, u64),
}
impl<R: Read> Read for Counted<'_, R> {
    fn read(&mut self, buf: &mut [u8]) -> std::io::Result<usize> {
        let n = self.inner.read(buf)?;
        *self.done += n as u64;
        (self.progress)(*self.done, self.total);
        Ok(n)
    }
}
fn download_sources(path: &Path, progress: &mut dyn FnMut(u64, u64)) -> Result<()> {
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
    let needed: Vec<_> = DOWNLOADS
        .into_iter()
        .filter(|&(name, size, digest)| verify(&path.join(name), size, digest).is_err())
        .collect();
    let total = needed.iter().map(|&(_, size, _)| size).sum();
    let mut done = 0;
    for (name, size, digest) in needed {
        let dest = path.join(name);
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
                &mut Counted {
                    inner: response.body_mut().as_reader().take(size + 1),
                    done: &mut done,
                    total,
                    progress: &mut *progress,
                },
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
#[derive(Default)]
pub(crate) struct InlineBatch {
    pub vectors: Vec<Vec<f32>>,
    pub cpu_unit: f64,
    pub span_unit: f64,
}
fn cpu_seconds() -> Option<f64> {
    #[cfg(unix)]
    unsafe {
        let mut usage: libc::rusage = std::mem::zeroed();
        if libc::getrusage(libc::RUSAGE_SELF, &mut usage) == 0 {
            return Some(
                usage.ru_utime.tv_sec as f64
                    + usage.ru_utime.tv_usec as f64 / 1e6
                    + usage.ru_stime.tv_sec as f64
                    + usage.ru_stime.tv_usec as f64 / 1e6,
            );
        }
    }
    #[cfg(windows)]
    unsafe {
        use windows_sys::Win32::{
            Foundation::FILETIME,
            System::Threading::{GetCurrentProcess, GetProcessTimes},
        };
        let mut times = [FILETIME {
            dwLowDateTime: 0,
            dwHighDateTime: 0,
        }; 4];
        let [created, exited, kernel, user] = &mut times;
        if GetProcessTimes(GetCurrentProcess(), created, exited, kernel, user) != 0 {
            let ticks = |t: &FILETIME| ((t.dwHighDateTime as u64) << 32) | t.dwLowDateTime as u64;
            return Some((ticks(kernel) + ticks(user)) as f64 / 1e7);
        }
    }
    None
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
/// The tokenizer with the 1024-token window, and its load time in milliseconds.
fn load_tokenizer(path: &Path, query: bool) -> Result<(TextTokenizer, f64)> {
    let _span = crate::semantic_profile::span("tokenizer_load");
    let start = std::time::Instant::now();
    // Explicit provisioning writes the query tables beside the other
    // converted files; bulk embedding builds the library's model.
    let mut tok = if query {
        TextTokenizer::Query(Box::new(semantic_tokenizer::load_query(
            &path.join("tokenizer-query.bin"),
        )?))
    } else {
        TextTokenizer::Library(Box::new(semantic_tokenizer::load(
            &path.join("tokenizer-compact.json"),
        )?))
    };
    tok.window(1024)?;
    Ok((tok, start.elapsed().as_secs_f64() * 1000.))
}
/// The backbone, and with `mapped` the weights it reads in place.
fn load_model(
    path: &Path,
    device: &Device,
    dtype: DType,
    mapped: bool,
) -> Result<(ModernBert, Option<MappedWeights>)> {
    let config: Config = serde_json::from_slice(&fs::read(path.join("config.json"))?)?;
    // The same F32 checkpoint backs mapped queries and bulk CPU tensors.
    // Metal converts it to F16 once at load, retaining no second file.
    let vb = unsafe {
        VarBuilder::from_mmaped_safetensors(&[path.join("weights-f32.safetensors")], dtype, device)?
    }
    .rename_f(|name| checkpoint_name(name).to_owned());
    let _span = crate::semantic_profile::span("weights_load");
    let weights = mapped
        .then(|| MappedWeights::open(&path.join("weights-f32.safetensors")))
        .transpose()?;
    Ok((ModernBert::load(vb, &config, weights.as_ref())?, weights))
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
        let mapped = query;
        crate::semantic_profile::event(serde_json::json!({"model_cache":{
            "verified":verified,"converted":true,"writes":0,"metal":device.is_metal(),"mapped":mapped
        }}));
        let dtype = if device.is_metal() {
            DType::F16
        } else {
            DType::F32
        };
        let (model, tok, weights_ms, tokenizer_ms) = std::thread::scope(|scope| -> Result<_> {
            let tokenizer = scope.spawn(|| load_tokenizer(path, query));
            let start = std::time::Instant::now();
            let (model, weights) = load_model(path, &device, dtype, mapped)?;
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
            crate::errln!(
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
    /// Parallel single-sequence inference retains the query model's arithmetic.
    pub fn embed_inline(&self, texts: &[String]) -> Result<Vec<Vec<f32>>> {
        Ok(self.inline_batch(texts)?.vectors)
    }
    pub fn inline_streams() -> usize {
        rayon::current_num_threads()
            .min(std::thread::available_parallelism().map_or(1, usize::from))
            .max(1)
    }
    pub(crate) fn inline_batch(&self, texts: &[String]) -> Result<InlineBatch> {
        use rayon::prelude::*;
        if !self.mapped || !self.device.is_cpu() {
            bail!("inline semantic embedding requires the mapped query model");
        }
        let streams = Self::inline_streams().min(texts.len());
        if streams == 0 {
            return Ok(InlineBatch::default());
        }
        let cpu = cpu_seconds();
        let started = std::time::Instant::now();
        let parts = (0..streams)
            .into_par_iter()
            .map(|offset| {
                texts
                    .iter()
                    .enumerate()
                    .skip(offset)
                    .step_by(streams)
                    .map(|(i, text)| {
                        let units = self.work_units(std::slice::from_ref(text))?;
                        let tick = std::time::Instant::now();
                        let vector = self.embed(std::slice::from_ref(text))?.remove(0);
                        Ok((i, vector, units, tick.elapsed().as_secs_f64() / units))
                    })
                    .collect::<Result<Vec<_>>>()
            })
            .collect::<Result<Vec<_>>>()?;
        let elapsed = started.elapsed().as_secs_f64();
        let used = cpu
            .zip(cpu_seconds())
            .map_or(elapsed * streams as f64, |(a, b)| (b - a).max(elapsed));
        let mut out = InlineBatch {
            vectors: vec![vec![]; texts.len()],
            ..InlineBatch::default()
        };
        let mut units = 0.;
        for (i, vector, weight, span) in parts.into_iter().flatten() {
            out.vectors[i] = vector;
            units += weight;
            out.span_unit = out.span_unit.max(span);
        }
        out.cpu_unit = used / units;
        Ok(out)
    }
    pub fn work_units(&self, texts: &[String]) -> Result<f64> {
        texts
            .iter()
            .map(|text| {
                let tokens = self.token_ids(text)?.len() as f64;
                Ok(((tokens / 200.) * (1. + tokens / 1024.) / (1. + 200. / 1024.)).max(0.25))
            })
            .sum()
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
            crate::errln!(
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
                crate::errln!(
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
    fn installed_requires_every_runtime_artifact_with_its_pinned_size() {
        let t = tempfile::tempdir().unwrap();
        assert!(!installed_at(t.path()));
        assert!(FILES.iter().map(|(_, size, _)| size).sum::<u64>() < 200_000_000);
        for (name, size, _) in FILES {
            File::create(t.path().join(name))
                .unwrap()
                .set_len(size)
                .unwrap();
        }
        assert!(installed_at(t.path()));
        for (name, size, _) in FILES {
            let p = t.path().join(name);
            fs::remove_file(&p).unwrap();
            assert!(!installed_at(t.path()));
            let file = File::create(&p).unwrap();
            file.set_len(size + 1).unwrap();
            assert!(!installed_at(t.path()));
            file.set_len(size).unwrap();
            assert!(installed_at(t.path()));
        }
    }
    #[test]
    fn converted_digest_is_checked_before_replacing_an_installed_file() {
        let t = tempfile::tempdir().unwrap();
        let artifact = ("weights", 3, crate::store::digest("new"));
        let spec = (artifact.0, artifact.1, artifact.2.as_str());
        fs::write(t.path().join("weights"), b"old").unwrap();
        fs::write(t.path().join("weights.partial"), b"bad").unwrap();
        assert!(publish_converted(t.path(), spec).is_err());
        assert_eq!(fs::read(t.path().join("weights")).unwrap(), b"old");
        fs::write(t.path().join("weights.partial"), b"new").unwrap();
        publish_converted(t.path(), spec).unwrap();
        assert_eq!(fs::read(t.path().join("weights")).unwrap(), b"new");
        assert!(!t.path().join("weights.partial").exists());
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
        assert!(verify(&p, 2, &crate::store::digest("no"))
            .unwrap_err()
            .is::<Rejected>());
        assert!(verify(&p, 3, "").unwrap_err().is::<Rejected>());
        assert!(!verify(&t.path().join("absent"), 2, "")
            .unwrap_err()
            .is::<Rejected>());
    }
    // Background work rejected a corrupt model on every scheduled run, logging the same
    // mismatch each time (QA finding 9); the verdict now lasts until the files change.
    #[test]
    fn a_rejection_lasts_until_the_model_files_change() {
        let t = tempfile::tempdir().unwrap();
        let root = DataRoot::open(t.path().join("data"), true).unwrap();
        let path = root.path().join("model").join(MODEL_ID);
        assert!(!incomplete(&root));
        fs::create_dir_all(&path).unwrap();
        fs::write(path.join("download.lock"), b"").unwrap();
        assert!(!incomplete(&root));
        for (name, size, _) in FILES {
            File::create(path.join(name))
                .unwrap()
                .set_len(size)
                .unwrap();
            assert_eq!(incomplete(&root), !installed(&root));
        }
        assert!(installed(&root) && !rejected(&root));
        record_rejection(&root).unwrap();
        assert!(rejected(&root));
        let config = path.join(FILES[0].0);
        fs::remove_file(&config).unwrap();
        File::create(&config).unwrap().set_len(FILES[0].1).unwrap();
        assert!(!rejected(&root));
    }
    #[test]
    fn download_progress_counts_bytes_across_files() {
        let (mut done, mut heard) = (0, vec![]);
        for part in [&b"abc"[..], b"de"] {
            let mut report = |n: u64, total: u64| heard.push((n, total));
            let mut counted = Counted {
                inner: part,
                done: &mut done,
                total: 5,
                progress: &mut report,
            };
            std::io::copy(&mut counted, &mut std::io::sink()).unwrap();
        }
        assert_eq!(heard.first(), Some(&(3, 5)));
        assert_eq!(heard.last(), Some(&(5, 5)));
    }
}
