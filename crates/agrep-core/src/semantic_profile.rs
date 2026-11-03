//! Opt-in synchronized diagnostics, separate from uninstrumented acceptance samples.
use candle_core::Device;
use std::{sync::OnceLock, time::Instant};

pub fn timing_enabled() -> bool {
    static ENABLED: OnceLock<bool> = OnceLock::new();
    *ENABLED.get_or_init(|| enabled() || std::env::var_os("AGREP_QUERY_TIMING").is_some())
}

fn origin() -> Instant {
    static START: OnceLock<Instant> = OnceLock::new();
    *START.get_or_init(Instant::now)
}

#[derive(Default, Clone, Copy)]
struct Usage {
    cpu_ms: f64,
    minor_faults: i64,
    major_faults: i64,
    input_blocks: i64,
    output_blocks: i64,
    involuntary_switches: i64,
}

impl Usage {
    // libc's counters are c_long, whose width differs between Unix targets.
    #[allow(clippy::unnecessary_cast)]
    fn now() -> Self {
        #[cfg(unix)]
        unsafe {
            let mut usage: libc::rusage = std::mem::zeroed();
            if libc::getrusage(libc::RUSAGE_SELF, &mut usage) == 0 {
                let milliseconds =
                    |t: libc::timeval| t.tv_sec as f64 * 1000. + t.tv_usec as f64 / 1000.;
                return Self {
                    cpu_ms: milliseconds(usage.ru_utime) + milliseconds(usage.ru_stime),
                    minor_faults: usage.ru_minflt as i64,
                    major_faults: usage.ru_majflt as i64,
                    input_blocks: usage.ru_inblock as i64,
                    output_blocks: usage.ru_oublock as i64,
                    involuntary_switches: usage.ru_nivcsw as i64,
                };
            }
        }
        Self::default()
    }
}

/// Aggregate-only diagnostics: no query, source text, paths, or SQL values.
pub struct Span {
    name: &'static str,
    start: Instant,
    usage: Option<Usage>,
}

pub fn span(name: &'static str) -> Span {
    let _ = origin();
    Span {
        name,
        start: Instant::now(),
        usage: timing_enabled().then(Usage::now),
    }
}

impl Drop for Span {
    fn drop(&mut self) {
        if let Some(before) = self.usage {
            let after = Usage::now();
            eprintln!(
                "{}",
                serde_json::json!({
                    "phase":self.name, "wall_ms":self.start.elapsed().as_secs_f64()*1000.,
                "start_ms":self.start.duration_since(origin()).as_secs_f64()*1000.,
                    "cpu_ms":after.cpu_ms-before.cpu_ms,
                    "minor_faults":after.minor_faults-before.minor_faults,
                    "major_faults":after.major_faults-before.major_faults,
                    "input_blocks":after.input_blocks-before.input_blocks,
                    "output_blocks":after.output_blocks-before.output_blocks,
                    "involuntary_switches":after.involuntary_switches-before.involuntary_switches,
                    "thread":format!("{:?}",std::thread::current().id())
                })
            );
        }
    }
}

pub fn event(value: serde_json::Value) {
    if timing_enabled() {
        eprintln!("{value}");
    }
}

/// Wall-clock endpoints let a parent measure exec and exit separately from
/// in-process phases. They carry no process, source or query identifiers.
pub fn process_endpoint(name: &'static str) {
    if timing_enabled() {
        let ns = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos();
        event(serde_json::json!({name: ns.min(u128::from(u64::MAX)) as u64}));
    }
}

pub(crate) fn enabled() -> bool {
    static ENABLED: OnceLock<bool> = OnceLock::new();
    *ENABLED.get_or_init(|| std::env::var_os("AGREP_SEM_PROFILE").is_some())
}

pub(crate) fn stage<T, E: From<candle_core::Error>>(
    device: &Device,
    name: &str,
    f: impl FnOnce() -> Result<T, E>,
) -> Result<T, E> {
    if !enabled() {
        return f();
    }
    device.synchronize()?;
    let start = Instant::now();
    let usage = Usage::now();
    let result = f()?;
    let encode_ms = start.elapsed().as_secs_f64() * 1000.;
    device.synchronize()?;
    let after = Usage::now();
    eprintln!(
        "{}",
        serde_json::json!({"semantic_stage":name,"encode_ms":encode_ms,"complete_ms":start.elapsed().as_secs_f64()*1000.,
            "cpu_ms":after.cpu_ms-usage.cpu_ms,"major_faults":after.major_faults-usage.major_faults,
            "involuntary_switches":after.involuntary_switches-usage.involuntary_switches})
    );
    Ok(result)
}
