//! Pinned, verified model provisioning and in-process CLS embedding.
use crate::{
    data::DataRoot,
    semantic_bert::{Config, ModernBert},
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
use tokenizers::{Tokenizer, TruncationParams};
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
    let mut file = File::open(path)?;
    if file.metadata()?.len() != size {
        bail!("incorrect model file size: {}", path.display());
    }
    let mut hash = Sha256::new();
    let mut buffer = [0u8; 65536];
    loop {
        let n = file.read(&mut buffer)?;
        if n == 0 {
            break;
        }
        hash.update(&buffer[..n]);
    }
    if format!("{:x}", hash.finalize()) != expected {
        bail!("model SHA-256 mismatch: {}", path.display());
    }
    Ok(())
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
    fs2::FileExt::lock_exclusive(&lock)?;
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
    Ok(())
}

pub struct Embedder {
    tok: Tokenizer,
    model: ModernBert,
    device: Device,
    token_budget: usize,
    attention_budget: usize,
}
impl Embedder {
    pub fn load(path: &Path, device: Device) -> Result<Self> {
        verify_installed_at(path)?;
        let mut tok =
            Tokenizer::from_file(path.join("tokenizer.json")).map_err(|e| anyhow!("{e}"))?;
        tok.with_truncation(Some(TruncationParams {
            max_length: 1024,
            ..Default::default()
        }))
        .map_err(|e| anyhow!("{e}"))?;
        tok.with_padding(None);
        let config: Config = serde_json::from_slice(&fs::read(path.join("config.json"))?)?;
        let dtype = if device.is_metal() {
            DType::F16
        } else {
            DType::F32
        };
        // All three immutable artifacts were SHA-256 verified before mapping.
        let vb = unsafe {
            VarBuilder::from_mmaped_safetensors(&[path.join("model.safetensors")], dtype, &device)?
        }
        .rename_f(|name| name.strip_prefix("model.").unwrap_or(name).to_owned());
        let model = ModernBert::load(vb, &config)?;
        // Wide short-row batches amortize dispatch and BLAS work; the quadratic
        // attention cap keeps full-length rows at eight Metal / four CPU rows.
        let token_budget = if device.is_metal() { 32768 } else { 16384 };
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
        })
    }
    pub fn query(root: &DataRoot) -> Result<Self> {
        Self::load(&directory(root), Device::Cpu)
    }
    pub fn background(root: &DataRoot) -> Result<Self> {
        #[cfg(target_os = "macos")]
        if let Ok(device) = Device::new_metal(0) {
            return Self::load(&directory(root), device);
        }
        Self::query(root)
    }
    /// Experimental window comparison for the standalone quality/performance harness.
    /// Product callers use the fixed 1024-token contract from `load`.
    pub fn with_token_limit(mut self, limit: usize) -> Result<Self> {
        if !(2..=1024).contains(&limit) {
            bail!("token limit must be 2..=1024");
        }
        self.tok
            .with_truncation(Some(TruncationParams {
                max_length: limit,
                ..Default::default()
            }))
            .map_err(|e| anyhow!("{e}"))?;
        Ok(self)
    }
    pub fn token_ids(&self, text: &str) -> Result<Vec<u32>> {
        Ok(self
            .tok
            .encode(text, true)
            .map_err(|e| anyhow!("{e}"))?
            .get_ids()
            .to_vec())
    }
    pub fn embed(&self, texts: &[String]) -> Result<Vec<Vec<f32>>> {
        let ids: Vec<_> = texts
            .iter()
            .map(|s| self.token_ids(s))
            .collect::<Result<_>>()?;
        let mut order: Vec<_> = (0..texts.len()).collect();
        order.sort_by_key(|&i| ids[i].len());
        let mut out = vec![vec![]; texts.len()];
        let mut start = 0;
        while start < order.len() {
            let mut end = start + 1;
            while end < order.len()
                && (end - start + 1) * ids[order[end]].len() <= self.token_budget
                && (end - start + 1) * ids[order[end]].len().pow(2) <= self.attention_budget
            {
                end += 1;
            }
            let batch = &order[start..end];
            let width = batch.iter().map(|&i| ids[i].len()).max().unwrap();
            let mut padded = vec![50283u32; batch.len() * width];
            let mut mask = vec![0u32; padded.len()];
            for (j, &i) in batch.iter().enumerate() {
                padded[j * width..j * width + ids[i].len()].copy_from_slice(&ids[i]);
                mask[j * width..j * width + ids[i].len()].fill(1);
            }
            let xs = Tensor::from_vec(padded, (batch.len(), width), &self.device)?;
            let mask = Tensor::from_vec(mask, (batch.len(), width), &self.device)?;
            let cls = self
                .model
                .forward(&xs, &mask)?
                .i((.., 0, ..))?
                .to_dtype(DType::F32)?
                .to_vec2::<f32>()?;
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
        let sha = format!("{:x}", Sha256::digest(b"abc"));
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
        verify(&p, 2, &format!("{:x}", Sha256::digest(b"ok"))).unwrap();
        assert!(verify(&p, 2, &format!("{:x}", Sha256::digest(b"no"))).is_err());
        assert!(verify(&p, 3, "").is_err());
    }
}
