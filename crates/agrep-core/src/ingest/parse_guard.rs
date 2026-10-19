//! Per-source panic isolation for adapter parsers: rayon re-raises a worker's panic in the
//! caller, so an uncaught parser bug on one hostile file aborts every agent's publication on each
//! run that meets it. A caught panic reads as an unreadable source whose reason quotes no store text.

use std::any::Any;
use std::cell::Cell;
use std::panic::{self, AssertUnwindSafe};
use std::path::Path;
use std::sync::Once;

const MESSAGE_CAP: usize = 160;
const CODE_REFERENCE_CAP: usize = 48;

thread_local! {
    static GUARD_DEPTH: Cell<usize> = const { Cell::new(0) };
    static PANIC_SITE: Cell<Option<String>> = const { Cell::new(None) };
}

/// A parse that panicked instead of returning.
#[derive(Debug)]
pub(crate) struct ParserPanic {
    /// Disclosable: the panic site and message, with any quoted operand withheld.
    pub(crate) reason: String,
}

/// Run one source's parse. A panic is caught, its half-counted intake tallies are withdrawn and
/// it comes back as [`ParserPanic`]; the caller then treats the source as unreadable this pass.
pub(crate) fn isolate<T>(
    agent: &str,
    source: &Path,
    parse: impl FnOnce() -> T,
) -> Result<T, ParserPanic> {
    route_guarded_panics();
    PANIC_SITE.with(Cell::take);
    // AssertUnwindSafe: the parse owns its working state. The one shared structure it writes,
    // the intake run book, is repaired below; its locks are never held across parser code.
    let (result, opened) = crate::intake::scoped(|| {
        GUARD_DEPTH.with(|depth| depth.set(depth.get() + 1));
        let result = panic::catch_unwind(AssertUnwindSafe(parse));
        GUARD_DEPTH.with(|depth| depth.set(depth.get() - 1));
        result
    });
    let payload = match result {
        Ok(value) => return Ok(value),
        Err(payload) => payload,
    };
    crate::intake::discard(&opened);
    let site = PANIC_SITE.with(Cell::take);
    let reason = panic_reason(payload.as_ref(), site.as_deref());
    crate::ingest::warn_source_skip(agent, source, &reason);
    Err(ParserPanic { reason })
}

/// The default hook prints a multi-line report to stderr. A guarded panic is reported once, as
/// a source issue; every other panic still reaches the previous hook unchanged.
fn route_guarded_panics() {
    static ROUTE: Once = Once::new();
    ROUTE.call_once(|| {
        let previous = panic::take_hook();
        panic::set_hook(Box::new(move |info| {
            if GUARD_DEPTH.try_with(Cell::get).unwrap_or(0) == 0 {
                previous(info);
                return;
            }
            let site = info
                .location()
                .map(|location| format!("{}:{}", source_tail(location.file()), location.line()));
            let _ = PANIC_SITE.try_with(|slot| slot.set(site));
        }));
    });
}

/// The last two path components: enough to find the line, without a builder's home directory.
fn source_tail(file: &str) -> &str {
    file.rmatch_indices(['/', '\\'])
        .nth(1)
        .map_or(file, |(index, _)| &file[index + 1..])
}

fn panic_reason(payload: &(dyn Any + Send), site: Option<&str>) -> String {
    let message = payload
        .downcast_ref::<&str>()
        .copied()
        .or_else(|| payload.downcast_ref::<String>().map(String::as_str))
        .map_or_else(|| "a non-string payload".to_owned(), withhold_operands);
    match site {
        Some(site) => format!("parser panicked at {site}: {message}"),
        None => format!("parser panicked: {message}"),
    }
}

/// std quotes what a panic is about (an unwrapped error's text, a sliced string) in Debug quotes
/// or backticks. Keep the message up to the first quote that is not std naming its own code,
/// such as `Option::unwrap()` or `None`, so no store text reaches a disclosed reason.
fn withhold_operands(message: &str) -> String {
    let line = message.lines().next().unwrap_or_default();
    let (mut rest, mut withheld) = match buffer_start(line) {
        Some(start) => (&line[..start], true),
        None => (line, false),
    };
    let mut kept = String::new();
    while let Some(open) = rest.find(['`', '"', '\'']) {
        let quote = &rest[open..open + 1];
        let quoted = &rest[open + 1..];
        kept.push_str(&rest[..open]);
        match quoted.find(quote) {
            Some(close) if quote == "`" && is_code_reference(&quoted[..close]) => {
                kept.push_str(&rest[open..open + close + 2]);
                rest = &quoted[close + 1..];
            }
            _ => {
                withheld = true;
                rest = "";
            }
        }
    }
    kept.push_str(rest);
    if withheld {
        kept.push('…');
    }
    crate::ingest::cap_str(&crate::ingest::terminal_safe(kept.trim_end()), MESSAGE_CAP)
}

/// Debug prints byte buffers unquoted (`FromUtf8Error { bytes: [109, 121, …] }`): a list bracket
/// or a run like `109, 121` starts store data too. Only bytes the cap could keep are scanned.
fn buffer_start(line: &str) -> Option<usize> {
    let bytes = &line.as_bytes()[..line.len().min(MESSAGE_CAP * 4)];
    let digits = |at: usize| {
        bytes[at..]
            .iter()
            .take_while(|b| b.is_ascii_digit())
            .count()
    };
    let run = (0..bytes.len()).find(|&at| {
        let first = digits(at);
        first > 0 && bytes[at + first..].starts_with(b", ") && digits(at + first + 2) > 0
    });
    bytes
        .iter()
        .position(|&byte| byte == b'[')
        .into_iter()
        .chain(run)
        .min()
}

fn is_code_reference(text: &str) -> bool {
    let path_shaped = text.len() <= CODE_REFERENCE_CAP
        && text.contains("::")
        && text
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"_:<>()".contains(&byte));
    path_shaped || matches!(text, "None" | "Some" | "Ok" | "Err")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fault(message: &'static str) -> Result<(), ParserPanic> {
        isolate("test", Path::new("/fixture/source.jsonl"), || {
            panic!("{message}")
        })
    }

    #[test]
    fn a_panicking_parse_returns_its_site_and_message() {
        let reason = fault("attempt to subtract with overflow")
            .unwrap_err()
            .reason;
        assert!(reason.starts_with("parser panicked at "), "{reason}");
        assert!(reason.contains("parse_guard.rs:"), "{reason}");
        assert!(
            reason.ends_with(": attempt to subtract with overflow"),
            "{reason}"
        );
        assert_eq!(isolate("test", Path::new("/fixture"), || 7).unwrap(), 7);
    }

    #[test]
    fn quoted_store_text_is_withheld_from_the_reason() {
        let read: Result<(), String> = Err("private".to_owned());
        let unwrapped = isolate("test", Path::new("/fixture"), || {
            std::hint::black_box(read).unwrap()
        });
        let reason = unwrapped.unwrap_err().reason;
        assert!(reason.ends_with("on an `Err` value: …"), "{reason}");
        assert!(!reason.contains("private"), "{reason}");

        assert_eq!(
            withhold_operands("called `Option::unwrap()` on a `None` value"),
            "called `Option::unwrap()` on a `None` value"
        );
        assert_eq!(
            withhold_operands("called `Result::unwrap()` on an `Err` value: Custom(\"secret\")"),
            "called `Result::unwrap()` on an `Err` value: Custom(…"
        );
        assert_eq!(
            withhold_operands("byte index 3 is not a char boundary; it is inside 'é' of `née`"),
            "byte index 3 is not a char boundary; it is inside …"
        );
        assert_eq!(
            withhold_operands("begin <= end when slicing `word`"),
            "begin <= end when slicing …"
        );
        assert_eq!(
            withhold_operands("unterminated `Option::unwrap"),
            "unterminated …"
        );
        assert_eq!(withhold_operands("first line\nsecond line"), "first line");
        assert_eq!(withhold_operands("x\u{7}y"), "x\\u0007y");
        assert!(withhold_operands(&"long ".repeat(100)).chars().count() <= MESSAGE_CAP + 1);
    }

    #[test]
    fn debug_printed_buffers_are_withheld_from_the_reason() {
        let utf8 = isolate("test", Path::new("/fixture"), || {
            String::from_utf8(std::hint::black_box(b"my api key\xff".to_vec())).unwrap()
        });
        let reason = utf8.unwrap_err().reason;
        assert!(
            reason.ends_with("on an `Err` value: FromUtf8Error { bytes: …"),
            "{reason}"
        );
        let nul = isolate("test", Path::new("/fixture"), || {
            std::ffi::CString::new(std::hint::black_box(b"secret\0tail".to_vec())).unwrap()
        });
        let reason = nul.unwrap_err().reason;
        assert!(
            reason.ends_with("on an `Err` value: NulError(6, …"),
            "{reason}"
        );

        assert_eq!(withhold_operands("read 109, 121, 32 then"), "read …");
        assert_eq!(withhold_operands("`a[0]` is out of range"), "…");
        for kept in [
            "index out of bounds: the len is 3 but the index is 5",
            "Utf8Error { valid_up_to: 3, error_len: Some(1) }",
        ] {
            assert_eq!(withhold_operands(kept), kept);
        }
    }

    #[test]
    fn a_withdrawn_parse_leaves_no_half_counted_tally() {
        let _run = crate::intake::RUN_TEST_LOCK
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let finished = isolate("test", Path::new("/fixture/whole.jsonl"), || {
            crate::intake::keyed("test", "/fixture/whole.jsonl", "s:1:1".into())
        })
        .unwrap();
        assert!(crate::intake::is_open(&finished));

        let mut opened = Vec::new();
        let failed = isolate("test", Path::new("/fixture/torn"), || {
            let nested = isolate("test", Path::new("/fixture/torn/part.jsonl"), || {
                crate::intake::keyed("test", "/fixture/torn/part.jsonl", "s:2:2".into())
            });
            opened.extend(nested.ok());
            let torn = crate::intake::keyed("test", "/fixture/torn/main.jsonl", "s:3:3".into());
            torn.seen();
            opened.push(torn);
            panic!("torn mid-record");
        });
        assert!(failed.is_err());
        assert_eq!(opened.len(), 2);
        assert!(opened.iter().all(|tally| !crate::intake::is_open(tally)));
        assert!(crate::intake::is_open(&finished));
    }

    #[test]
    fn source_tail_keeps_two_components() {
        assert_eq!(
            source_tail("crates/agrep-core/src/ingest/gemini.rs"),
            "ingest/gemini.rs"
        );
        assert_eq!(source_tail(r"C:\build\src\lib.rs"), r"src\lib.rs");
        assert_eq!(source_tail("lib.rs"), "lib.rs");
    }
}
