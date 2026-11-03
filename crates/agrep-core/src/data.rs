//! The sole data format and kernel-owned writer locks.
use anyhow::{bail, Context, Result};
use fs2::FileExt;
use serde::{Deserialize, Serialize};
use std::{
    fs::{self, File, OpenOptions},
    path::{Path, PathBuf},
};
pub const FORMAT: &[u8] = b"agrep-data 1\n";
#[derive(Debug, Clone)]
pub struct DataRoot(pub PathBuf);
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct Config {
    pub tools: bool,
    pub embeddings: String,
    pub post_index: Option<String>,
    pub enrollment: Vec<crate::integrations::Installed>,
    pub archive: crate::archive::Policy,
    pub board_view: Option<crate::live::board::Preference>,
}
impl Default for Config {
    fn default() -> Self {
        Self {
            tools: true,
            embeddings: "auto".into(),
            post_index: None,
            enrollment: vec![],
            archive: Default::default(),
            board_view: None,
        }
    }
}
pub struct WriterLock(File);
impl Drop for WriterLock {
    fn drop(&mut self) {
        let _ = FileExt::unlock(&self.0);
    }
}
impl DataRoot {
    pub fn discover() -> Result<Self> {
        if let Some(p) = std::env::var_os("AGREP_DATA_DIR") {
            return Ok(Self(p.into()));
        }
        #[cfg(not(target_os = "windows"))]
        let home = std::env::var_os("HOME")
            .or_else(|| std::env::var_os("USERPROFILE"))
            .context("home directory unavailable")?;
        #[cfg(target_os = "macos")]
        let p = PathBuf::from(home).join("Library/Application Support/agrep/0.5");
        #[cfg(target_os = "windows")]
        let p =
            PathBuf::from(std::env::var_os("LOCALAPPDATA").context("LOCALAPPDATA unavailable")?)
                .join("agrep/0.5");
        #[cfg(not(any(target_os = "macos", target_os = "windows")))]
        let p = std::env::var_os("XDG_DATA_HOME")
            .map(PathBuf::from)
            .unwrap_or_else(|| PathBuf::from(home).join(".local/share"))
            .join("agrep/0.5");
        Ok(Self(p))
    }
    pub fn open(path: impl Into<PathBuf>, initialize: bool) -> Result<Self> {
        let root = Self(path.into());
        root.validate(initialize)?;
        Ok(root)
    }
    pub fn validate(&self, initialize: bool) -> Result<()> {
        match fs::read(self.0.join("FORMAT")) {
            Ok(v) if v == FORMAT => {
                private_dir(&self.0)?;
                for name in ["archive", "model", "log"] {
                    let path = self.0.join(name);
                    if path.is_dir() {
                        private_dir(&path)?;
                    }
                }
                return Ok(());
            }
            Ok(_) => bail!("unsupported data format: {}", self.0.display()),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
            Err(e) => return Err(e).with_context(|| format!("data root: {}", self.0.display())),
        }
        if self.0.exists() && fs::read_dir(&self.0)?.next().is_some() {
            bail!("nonempty data root without FORMAT: {}", self.0.display())
        }
        if !initialize || read_only() {
            bail!("index does not exist: {}", self.0.display())
        }
        private_dir(&self.0)?;
        // create_new makes racing initializers converge without replacing a marker.
        match OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(self.0.join("FORMAT"))
        {
            Ok(mut f) => {
                use std::io::Write;
                f.write_all(FORMAT)?;
                f.sync_all()?;
            }
            Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {
                if fs::read(self.0.join("FORMAT"))? != FORMAT {
                    bail!("unsupported data format: {}", self.0.display())
                }
            }
            Err(e) => return Err(e.into()),
        }
        Ok(())
    }
    pub fn writer_active(&self) -> Result<bool> {
        let path = self.0.join("writer.lock");
        if !path.exists() {
            return Ok(false);
        }
        let file = OpenOptions::new().read(true).write(true).open(path)?;
        match file.try_lock_exclusive() {
            Ok(()) => {
                FileExt::unlock(&file)?;
                Ok(false)
            }
            Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => Ok(true),
            Err(error) => Err(error.into()),
        }
    }
    pub fn lock(&self, blocking: bool) -> Result<Option<WriterLock>> {
        self.lock_named("writer.lock", blocking)
    }
    pub fn work_lock(&self, blocking: bool) -> Result<Option<WriterLock>> {
        self.lock_named("work.lock", blocking)
    }
    fn lock_named(&self, name: &str, blocking: bool) -> Result<Option<WriterLock>> {
        if read_only() {
            bail!("data directory is read-only")
        }
        let f = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(self.0.join(name))?;
        if blocking {
            f.lock_exclusive()?
        } else {
            match f.try_lock_exclusive() {
                Ok(()) => {}
                Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => return Ok(None),
                Err(e) => return Err(e.into()),
            }
        }
        Ok(Some(WriterLock(f)))
    }
    pub fn db(&self) -> PathBuf {
        self.0.join("index.db")
    }
    pub fn config(&self) -> Result<Config> {
        match fs::read(self.0.join("config.json")) {
            Ok(v) => Ok(serde_json::from_slice(&v).context("invalid config.json")?),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(Config::default()),
            Err(e) => Err(e.into()),
        }
    }
    pub fn save_config(&self, c: &Config) -> Result<()> {
        if read_only() {
            bail!("data directory is read-only")
        }
        crate::safe_fs::write_atomic(
            &self.0.join("config.json"),
            &serde_json::to_vec_pretty(c)?,
            true,
        )
    }
    pub fn path(&self) -> &Path {
        &self.0
    }
}

pub fn read_only() -> bool {
    std::env::var("AGREP_DATA_READONLY")
        .is_ok_and(|v| !v.is_empty() && !matches!(v.as_str(), "0" | "false" | "off"))
}

/// Create only the requested directory tree; tighten an existing owned directory.
pub fn private_dir(path: &Path) -> Result<()> {
    let mut builder = fs::DirBuilder::new();
    builder.recursive(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::DirBuilderExt;
        builder.mode(0o700);
    }
    builder.create(path)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let metadata = fs::metadata(path)?;
        if metadata.permissions().mode() & 0o777 != 0o700 {
            fs::set_permissions(path, fs::Permissions::from_mode(0o700))?;
        }
    }
    Ok(())
}
/// Inspect permissions without mutating a root that diagnostics may need to report.
pub fn exposed(path: &Path) -> Result<bool> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        Ok(fs::metadata(path)?.permissions().mode() & 0o077 != 0)
    }
    #[cfg(not(unix))]
    {
        let _ = path;
        Ok(false)
    }
}
