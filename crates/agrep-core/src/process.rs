//! Passive process evidence. Never launches, attaches to, or changes an agent.
use std::io;
#[cfg(target_os = "linux")]
use std::path::Path;

#[derive(Clone, Debug)]
pub struct Process {
    pub pid: u32,
    pub agent: String,
    pub session: Option<String>,
    pub cwd: String,
}

pub fn classify(name: &str, command: &str) -> Option<&'static str> {
    let name = name.to_ascii_lowercase();
    let name = name.strip_suffix(".exe").unwrap_or(&name);
    let command = command.to_ascii_lowercase();
    match name {
        "claude" => Some("claude"),
        "codex" => Some("codex"),
        "opencode" => Some("opencode"),
        "kimi" => Some("kimi"),
        "agy" | "antigravity" => Some("antigravity"),
        "pi" | "omp"
            if !command
                .split_whitespace()
                .any(|s| s.starts_with("__omp_") || s == "browser-relay") =>
        {
            Some("pi")
        }
        "node" | "bun" => {
            if command.contains("opencode") {
                Some("opencode")
            } else if command.contains("@anthropic-ai/claude")
                || (command.contains("claude") && command.contains(".js"))
            {
                Some("claude")
            } else if command.contains("codex") && command.contains("node_modules") {
                Some("codex")
            } else if ["pi-coding-agent", "@oh-my-pi/", "node_modules/oh-my-pi"]
                .iter()
                .any(|s| command.contains(s))
            {
                Some("pi")
            } else {
                None
            }
        }
        _ => None,
    }
}
pub fn session_from_command(agent: &str, command: &str) -> Option<String> {
    use std::sync::LazyLock;
    static UUID: LazyLock<regex::Regex> = LazyLock::new(|| {
        regex::Regex::new(
            r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b",
        )
        .unwrap()
    });
    static SES: LazyLock<regex::Regex> =
        LazyLock::new(|| regex::Regex::new(r"\bses_[A-Za-z0-9]{6,}\b").unwrap());
    (if agent == "opencode" { &SES } else { &UUID })
        .find(command)
        .map(|m| m.as_str().into())
}
fn record(pid: u32, name: &str, cmd: &str, cwd: String) -> Option<Process> {
    let agent = classify(name, cmd)?;
    Some(Process {
        pid,
        agent: agent.into(),
        session: session_from_command(agent, cmd),
        cwd,
    })
}

#[cfg(target_os = "linux")]
pub fn scan() -> io::Result<Vec<Process>> {
    let mut out = Vec::new();
    for entry in std::fs::read_dir("/proc")? {
        let entry = entry?;
        let Some(pid) = entry
            .file_name()
            .to_str()
            .and_then(|s| s.parse::<u32>().ok())
        else {
            continue;
        };
        let Ok(raw) = std::fs::read(entry.path().join("cmdline")) else {
            continue;
        };
        let args: Vec<_> = raw
            .split(|b| *b == 0)
            .filter(|a| !a.is_empty())
            .map(String::from_utf8_lossy)
            .collect();
        let Some(first) = args.first() else { continue };
        let name = Path::new(first.as_ref())
            .file_name()
            .unwrap_or_default()
            .to_string_lossy();
        let cmd = args.join(" ");
        let cwd = std::fs::read_link(entry.path().join("cwd"))
            .unwrap_or_default()
            .to_string_lossy()
            .into_owned();
        if let Some(p) = record(pid, &name, &cmd, cwd) {
            out.push(p)
        }
    }
    Ok(out)
}

#[cfg(target_os = "macos")]
#[link(name = "proc")]
extern "C" {
    fn proc_listpids(kind: u32, info: u32, buffer: *mut std::ffi::c_void, size: i32) -> i32;
    fn proc_name(pid: i32, buffer: *mut std::ffi::c_void, size: u32) -> i32;
    fn proc_pidinfo(
        pid: i32,
        flavor: i32,
        arg: u64,
        buffer: *mut std::ffi::c_void,
        size: i32,
    ) -> i32;
}
/// KERN_PROCARGS2's executable prefix is followed by argc NUL-separated arguments.
#[cfg(any(target_os = "macos", test))]
fn mac_args(raw: &[u8]) -> Vec<String> {
    if raw.len() < 5 {
        return vec![];
    }
    let argc = i32::from_ne_bytes(raw[..4].try_into().unwrap());
    if argc <= 0 {
        return vec![];
    }
    let Some(end) = raw[4..].iter().position(|b| *b == 0) else {
        return vec![];
    };
    let mut start = 5 + end;
    while raw.get(start) == Some(&0) {
        start += 1;
    }
    raw[start..]
        .split(|b| *b == 0)
        .take(argc as usize)
        .map(|s| String::from_utf8_lossy(s).into_owned())
        .collect()
}
#[cfg(target_os = "macos")]
pub fn scan() -> io::Result<Vec<Process>> {
    // SAFETY: native APIs receive correctly sized writable buffers; no pointers escape.
    unsafe {
        let needed = proc_listpids(1, 0, std::ptr::null_mut(), 0);
        if needed <= 0 {
            return Err(io::Error::last_os_error());
        }
        let mut pids = vec![0i32; (needed as usize + 16384) / 4];
        let used = proc_listpids(1, 0, pids.as_mut_ptr().cast(), (pids.len() * 4) as i32);
        if used < 0 || used as usize >= pids.len() * 4 {
            return Err(io::Error::other("process census overflow"));
        }
        let mut out = vec![];
        for &pid in &pids[..used as usize / 4] {
            if pid <= 0 {
                continue;
            }
            let mut name = [0u8; 1024];
            let n = proc_name(pid, name.as_mut_ptr().cast(), name.len() as u32);
            if n <= 0 {
                continue;
            }
            let name = String::from_utf8_lossy(&name[..n as usize]);
            if classify(&name, &name).is_none() && !matches!(name.as_ref(), "node" | "bun") {
                continue;
            }
            let mut mib = [1, 49, pid];
            let mut args = vec![0u8; 262144];
            let mut size = args.len();
            if libc::sysctl(
                mib.as_mut_ptr(),
                3,
                args.as_mut_ptr().cast(),
                &mut size,
                std::ptr::null_mut(),
                0,
            ) != 0
            {
                size = 0;
            }
            let cmd = mac_args(&args[..size]).join(" ");
            let mut vnode = [0u8; 2352];
            let n = proc_pidinfo(pid, 9, 0, vnode.as_mut_ptr().cast(), vnode.len() as i32);
            let cwd = if n == vnode.len() as i32 {
                String::from_utf8_lossy(
                    vnode[152..1176]
                        .split(|b| *b == 0)
                        .next()
                        .unwrap_or_default(),
                )
                .into_owned()
            } else {
                String::new()
            };
            if let Some(p) = record(pid as u32, &name, &cmd, cwd) {
                out.push(p)
            }
        }
        Ok(out)
    }
}

#[cfg(windows)]
pub fn scan() -> io::Result<Vec<Process>> {
    use windows_sys::Win32::{
        Foundation::{CloseHandle, INVALID_HANDLE_VALUE},
        System::{
            Diagnostics::{Debug::ReadProcessMemory, ToolHelp::*},
            Threading::*,
        },
    };
    // Both supported Windows targets use the 64-bit PEB/UNICODE_STRING layout.
    unsafe fn bytes(
        h: windows_sys::Win32::Foundation::HANDLE,
        address: usize,
        size: usize,
    ) -> Option<Vec<u8>> {
        let mut b = vec![0; size];
        let mut n = 0;
        (ReadProcessMemory(h, address as _, b.as_mut_ptr().cast(), size, &mut n) != 0 && n == size)
            .then_some(b)
    }
    unsafe fn string(h: windows_sys::Win32::Foundation::HANDLE, address: usize) -> Option<String> {
        let head = bytes(h, address, 16)?;
        let n = u16::from_le_bytes(head[..2].try_into().ok()?) as usize;
        let ptr = u64::from_le_bytes(head[8..16].try_into().ok()?) as usize;
        let b = bytes(h, ptr, n.min(16384))?;
        Some(String::from_utf16_lossy(
            &b.chunks_exact(2)
                .map(|s| u16::from_le_bytes([s[0], s[1]]))
                .collect::<Vec<_>>(),
        ))
    }
    unsafe {
        let snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
        if snapshot == INVALID_HANDLE_VALUE {
            return Err(io::Error::last_os_error());
        }
        let mut row: PROCESSENTRY32W = std::mem::zeroed();
        row.dwSize = std::mem::size_of_val(&row) as u32;
        let mut more = Process32FirstW(snapshot, &mut row);
        let mut out = vec![];
        while more != 0 {
            let name = String::from_utf16_lossy(
                row.szExeFile.split(|c| *c == 0).next().unwrap_or_default(),
            );
            if classify(&name, &name).is_some() || matches!(name.as_str(), "node.exe" | "bun.exe") {
                let h = OpenProcess(
                    PROCESS_QUERY_INFORMATION | PROCESS_VM_READ,
                    0,
                    row.th32ProcessID,
                );
                if !h.is_null() {
                    use windows_sys::Wdk::System::Threading::NtQueryInformationProcess;
                    use windows_sys::Win32::System::Threading::PROCESS_BASIC_INFORMATION;
                    let mut pbi: PROCESS_BASIC_INFORMATION = std::mem::zeroed();
                    let mut returned = 0;
                    let mut cmd = String::new();
                    let mut cwd = String::new();
                    if NtQueryInformationProcess(
                        h,
                        0,
                        (&mut pbi as *mut PROCESS_BASIC_INFORMATION).cast(),
                        std::mem::size_of_val(&pbi) as u32,
                        &mut returned,
                    ) == 0
                    {
                        if let Some(raw) = bytes(h, pbi.PebBaseAddress as usize + 0x20, 8) {
                            let params = u64::from_le_bytes(raw.try_into().unwrap()) as usize;
                            cmd = string(h, params + 0x70).unwrap_or_default();
                            cwd = string(h, params + 0x38).unwrap_or_default();
                        }
                    }
                    if let Some(p) = record(row.th32ProcessID, &name, &cmd, cwd) {
                        out.push(p)
                    }
                    CloseHandle(h);
                }
            }
            more = Process32NextW(snapshot, &mut row);
        }
        CloseHandle(snapshot);
        Ok(out)
    }
}

#[cfg(not(any(target_os = "macos", target_os = "linux", windows)))]
pub fn scan() -> io::Result<Vec<Process>> {
    Err(io::Error::other("process census unsupported"))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn identities_and_hosts() {
        assert_eq!(
            classify("node", "/x/node_modules/@anthropic-ai/claude/cli.js"),
            Some("claude")
        );
        assert_eq!(classify("omp", "omp __omp_browser"), None);
        assert_eq!(classify("agrep", "agrep codex"), None);
        assert_eq!(classify("codex.exe", "codex resume abc"), Some("codex"));
        assert_eq!(
            session_from_command("codex", "codex resume 12345678-1234-1234-1234-123456789abc")
                .as_deref(),
            Some("12345678-1234-1234-1234-123456789abc")
        );
        assert_eq!(
            session_from_command("opencode", "opencode -s ses_abcdef"),
            Some("ses_abcdef".into())
        );
    }
    #[test]
    fn kernel_argument_layout() {
        let mut b = 3i32.to_ne_bytes().to_vec();
        b.extend_from_slice(b"/bin/node\0\0node\0cli.js\0resume\0SECRET=x\0");
        assert_eq!(mac_args(&b), ["node", "cli.js", "resume"]);
    }
    #[test]
    fn native_scan_finds_owned_child_and_session() {
        use std::{
            io::{BufRead, BufReader},
            process::{Command, Stdio},
        };
        let t = tempfile::tempdir().unwrap();
        let session = "12345678-1234-1234-1234-123456789abc";
        #[cfg(unix)]
        let executable = t.path().join("codex");
        #[cfg(windows)]
        let executable = t.path().join("codex.exe");
        let source = t.path().join("probe.rs");
        std::fs::write(&source, r#"fn main(){println!("ready");let mut line=String::new();std::io::stdin().read_line(&mut line).unwrap();}"#).unwrap();
        assert!(
            Command::new(std::env::var_os("RUSTC").unwrap_or_else(|| "rustc".into()))
                .arg(&source)
                .arg("-o")
                .arg(&executable)
                .status()
                .unwrap()
                .success()
        );
        let mut cmd = Command::new(&executable);
        cmd.arg(session);
        let mut child = cmd
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .spawn()
            .unwrap();
        let mut ready = String::new();
        BufReader::new(child.stdout.take().unwrap())
            .read_line(&mut ready)
            .unwrap();
        let result = scan();
        let pid = child.id();
        child.kill().unwrap();
        child.wait().unwrap();
        assert_eq!(ready.trim(), "ready");
        let processes = result.unwrap();
        let found = processes
            .iter()
            .find(|p| p.pid == pid)
            .expect("owned codex child missing from census");
        assert_eq!(found.agent, "codex");
        assert_eq!(found.session.as_deref(), Some(session));
    }
}
