use agrep_core::{explorer::Explorer, terminal::Signals};
use anyhow::Result;
use std::io::Write;
pub fn run(port: u16, no_auto: bool) -> Result<i32> {
    start(port, no_auto, false)
}
pub(super) fn start(port: u16, no_auto: bool, open_browser: bool) -> Result<i32> {
    let root = crate::ready(no_auto)?;
    let server = Explorer::bind(root, port)?.with_refresh(move || {
        crate::ready(no_auto)?;
        Ok(())
    });
    let signals = Signals::new()?;
    let url = server.url();
    println!("agrep explorer: {url}");
    std::io::stdout().flush()?;
    if open_browser {
        let result = open(&url);
        if result.is_err() {
            eprintln!("browser did not open automatically; paste the explorer URL");
        }
    }
    server.serve(&signals)?;
    Ok(if signals.pending() == 0 {
        0
    } else {
        signals.exit_code()
    })
}
fn open(url: &str) -> std::io::Result<()> {
    #[cfg(target_os = "macos")]
    let mut command = std::process::Command::new("open");
    #[cfg(target_os = "linux")]
    let mut command = std::process::Command::new("xdg-open");
    #[cfg(windows)]
    let mut command = {
        let mut c = std::process::Command::new("rundll32");
        c.arg("url.dll,FileProtocolHandler");
        c
    };
    let mut child = command
        .arg(url)
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .spawn()?;
    std::thread::spawn(move || {
        let _ = child.wait();
    });
    Ok(())
}
