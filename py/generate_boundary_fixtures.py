from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import unicodedata

import boundary_rank


UNICODE_VERSION = "16.0.0"
ROOT = Path(__file__).resolve().parents[1]
ORACLE_PATH = Path(__file__).resolve().parent / "fixtures" / "boundary_conformance.json"
RUST_PATH = ROOT / "crates" / "agrep-core" / "src" / "unicode_v16.rs"

SCRIPTS = (
    "LATIN", "GREEK", "CYRILLIC", "ARMENIAN", "HEBREW", "ARABIC", "SYRIAC",
    "THAANA", "DEVANAGARI", "BENGALI", "GURMUKHI", "GUJARATI", "ORIYA",
    "TAMIL", "TELUGU", "KANNADA", "MALAYALAM", "SINHALA", "THAI", "LAO",
    "TIBETAN", "MYANMAR", "GEORGIAN", "HANGUL", "ETHIOPIC", "CHEROKEE",
    "KHMER", "MONGOLIAN", "HIRAGANA", "KATAKANA", "HAN",
)

SEGMENTATION_CASES = [
    {"text": "caf\u00e9's", "aligned": []},
    {"text": "cafe\u0301's", "aligned": []},
    {"text": "caf\u00e9Bar", "aligned": ["bar", "caf\u00e9"]},
    {"text": "cafe\u0301Bar", "aligned": ["bar", "caf\u00e9"]},
    {"text": "don't", "aligned": []},
    {"text": "peakDetect", "aligned": ["peak"]},
    {"text": "fooBar", "aligned": ["bar", "foo"]},
    {"text": "HTTPServer", "aligned": ["http"]},
    {"text": "sha256", "aligned": ["256", "sha"]},
    {"text": "JSON\u89e3\u6790", "aligned": ["json", "\u89e3\u6790"]},
    {"text": "pho\u031b\u030942", "aligned": ["42", "ph\u1edf"]},
    {"text": "a\u20dd", "aligned": ["a\u20dd"]},
    {"text": "\U00010940a", "aligned": []},
    {"text": "a\u00bc", "aligned": ["a1\u20444"]},
    {"text": "pic \U0001f468\u200d\U0001f469\u200d\U0001f467 set", "aligned": ["pic", "set"]},
    {"text": "get\u0130D", "aligned": ["get", "i\u0307d"]},
    {"text": "Ma\u00df", "aligned": ["mass"]},
]

EVALUATION_INPUTS = [
    {"name": "camel", "query": "bar", "text": "fooBarBaz"},
    {"name": "acronym", "query": "http", "text": "myHTTPServer"},
    {"name": "alpha_digit", "query": "256", "text": "sha256"},
    {"name": "partial_digit", "query": "2", "text": "sha256"},
    {"name": "script_change", "query": "\u03b4", "text": "var\u0394x"},
    {"name": "interior", "query": "akd", "text": "peakDetect"},
    {"name": "punctuation", "query": "filter", "text": "cyber_filter"},
    {"name": "partial_suffix", "query": "filter", "text": "cyber_filterilter"},
    {"name": "apostrophe", "query": "t", "text": "don't"},
    {"name": "sharp_s", "query": "STRASSE", "text": "Stra\u00dfe"},
    {"name": "combining", "query": "\u00e9", "text": "e\u0301"},
    {"name": "hangul_jamo", "query": "\uac00", "text": "\u1100\u1161"},
    {"name": "casefold_expansion_span", "query": "xxi", "text": "xx\u0130foo", "spans": [[0, 3]]},
    {"name": "validate_disabled", "query": "i", "text": "x\u0131x",
     "spans": [[1, 2]], "validate_spans": False},
    {"name": "observed_floor", "query": "akd", "stats": {"akd": [372, 32]}, "text": "peakDetect"},
    {"name": "geometric_mean", "query": "akd xyz",
     "stats": {"akd": [372, 32], "xyz": [100, 100]}, "text": "peakDetect xyz"},
    {"name": "duplicate_term", "query": "akd akd", "stats": {"akd": [10, 2]}, "text": "akd and akd"},
    {"name": "exact_spans", "query": "cyber filter",
     "text": "cyber_filterilter and standalone filter", "spans": [[0, 5], [6, 12]]},
    {"name": "automatic_phrase", "query": "cyber filter",
     "text": "cyber_filterilter and standalone filter"},
    {"name": "unsegmented_interior", "query": "\u6771\u4eac", "text": "\u99d0\u6771\u4eac\u90fd"},
    {"name": "unsegmented_aligned", "query": "\u6771\u4eac", "text": "\u6771\u4eac tower"},
    {"name": "unsegmented_stats", "query": "\u6771\u4eac",
     "stats": {"\u6771\u4eac": [100, 0]}, "text": "\u6771\u4eac\u90fd"},
    {"name": "mapping_stats", "query": "akd", "stats": {"akd": {"n": 10, "s": 2}}, "text": "peakDetect"},
    {"name": "half_quality_stats", "query": "akd", "stats": {"akd": [10, 2]}, "text": "xakd"},
    {"name": "stats_over", "query": "akd", "stats": {"akd": [10, 50]}, "text": "peakDetect"},
    {"name": "stats_negative", "query": "akd", "stats": {"akd": [10, -5]}, "text": "peakDetect"},
    {"name": "stats_unseen", "query": "akd", "stats": {"akd": [0, 7]}, "text": "peakDetect"},
    {"name": "missing_term", "query": "one two", "text": "one only"},
    {"name": "empty_query", "query": " -_ ", "text": "anything"},
    {"name": "span_count_error", "query": "one two", "text": "one two", "spans": [[0, 3]]},
    {"name": "span_validation_error", "query": "one two", "text": "one two", "spans": [[4, 7], [0, 3]]},
    {"name": "negative_span_error", "query": "akd", "text": "peakDetect", "spans": [[-3, 3]]},
    {"name": "reversed_span_error", "query": "akd", "text": "peakDetect\u6771",
     "spans": [[3, 1]], "validate_spans": False},
    {"name": "long_span_error", "query": "\u6771\u4eac", "text": "\u6771\u4eac\u90fd", "spans": [[0, 999]]},
    {"name": "unicode16_casefold", "query": "\ua7cc", "text": "x\ua7cdy"},
    {"name": "unicode16_separator", "query": "a", "text": "\U0001cc00a"},
    {"name": "ccc_zero_mark", "query": "a", "text": "a\u20ddy"},
    {"name": "emoji_zwj", "query": "y", "text": "x\U0001f469\u200d\U0001f4bby"},
    {"name": "nfc_cafe", "query": "caf\u00e9", "text": "caf\u00e9Bar"},
    {"name": "nfd_cafe", "query": "caf\u00e9", "text": "cafe\u0301Bar"},
    {"name": "nfc_bar", "query": "bar", "text": "caf\u00e9Bar"},
    {"name": "nfd_bar", "query": "bar", "text": "cafe\u0301Bar"},
    {"name": "compatibility", "query": "k", "text": "\u212a"},
]


def _score(case: dict[str, object]) -> dict[str, object]:
    prepared = boundary_rank.prepare_query(case["query"], case.get("stats"))
    try:
        score = prepared.evaluate(
            case["text"],
            spans=case.get("spans"),
            validate_spans=case.get("validate_spans", True),
        )
    except ValueError as error:
        return {"error": str(error)}
    return {
        "factor": score.factor,
        "qualities": list(score.qualities),
        "match_class": score.match_class,
        "matched": score.matched,
        "spans": [list(span) if span is not None else None for span in score.spans],
    }


def oracle_text() -> str:
    evaluations = []
    for source in EVALUATION_INPUTS:
        case = dict(source)
        case["expected"] = _score(case)
        evaluations.append(case)
    payload = {
        "schema": 2,
        "python_unicode_version": UNICODE_VERSION,
        "segmentation": SEGMENTATION_CASES,
        "evaluations": evaluations,
    }
    return json.dumps(payload, ensure_ascii=True, indent=2) + "\n"


def _property(character: str) -> int:
    category = unicodedata.category(character)
    return (
        (1 if character.isalpha() else 0)
        | (2 if character.isalnum() else 0)
        | (4 if character.islower() else 0)
        | (8 if character.isupper() else 0)
        | (16 if character.lower() != character.upper() else 0)
        | (32 if character.isspace() else 0)
        | (64 if category[0] in ("P", "S") else 0)
        | (128 if category[0] == "M" else 0)
        | (256 if character.isdigit() else 0)
    )


def _script(character: str) -> int:
    if not character.isalpha():
        return 0
    name = unicodedata.name(character, "")
    for index, script in enumerate(SCRIPTS, 1):
        needles = ("CJK", "IDEOGRAPH") if script == "HAN" else (script,)
        if any(needle in name for needle in needles):
            return index
    return 0


def _ranges(function) -> list[tuple[int, int, int]]:
    out = []
    start = 0
    previous = function(chr(0))
    for codepoint in range(1, 0x110000):
        value = 0 if 0xD800 <= codepoint <= 0xDFFF else function(chr(codepoint))
        if value == previous:
            continue
        if previous:
            out.append((start, codepoint - 1, previous))
        start = codepoint
        previous = value
    if previous:
        out.append((start, 0x10FFFF, previous))
    return out


def _escape(value: str) -> str:
    return "".join(f"\\u{{{ord(character):x}}}" for character in value)


def rust_text() -> str:
    properties = _ranges(_property)
    scripts = _ranges(_script)
    casefolds = [
        (codepoint, chr(codepoint).casefold())
        for codepoint in range(0x110000)
        if not 0xD800 <= codepoint <= 0xDFFF and chr(codepoint).casefold() != chr(codepoint)
    ]
    lines = [
        "// Generated from Python's Unicode 16.0 data; its license is bundled in",
        "// THIRD_PARTY_LICENSES.txt. Regenerate with py/generate_boundary_fixtures.py.",
        "",
        "use unicode_normalization::UNICODE_VERSION as NORMALIZATION_VERSION;",
        "",
        "const _: () = {",
        "    assert!(NORMALIZATION_VERSION.0 == 16);",
        "    assert!(NORMALIZATION_VERSION.1 == 0);",
        "    assert!(NORMALIZATION_VERSION.2 == 0);",
        "};",
        "",
        "const ALPHA: u16 = 1;",
        "const ALNUM: u16 = 2;",
        "const LOWER: u16 = 4;",
        "const UPPER: u16 = 8;",
        "const CASED: u16 = 16;",
        "const SPACE: u16 = 32;",
        "const SEPARATOR: u16 = 64;",
        "const MARK: u16 = 128;",
        "const DIGIT: u16 = 256;",
        "",
        "const PROPERTIES: &[(u32, u32, u16)] = &[",
    ]
    lines.extend(f"    (0x{start:x}, 0x{end:x}, 0x{value:x})," for start, end, value in properties)
    lines.extend(["];;".replace(";;", ";"), "", "const SCRIPTS: &[(u32, u32, u8)] = &["])
    lines.extend(f"    (0x{start:x}, 0x{end:x}, {value})," for start, end, value in scripts)
    lines.extend(["];;".replace(";;", ";"), "", "const CASEFOLD: &[(u32, &str)] = &["])
    lines.extend(f'    (0x{codepoint:x}, "{_escape(value)}"),' for codepoint, value in casefolds)
    lines.extend([
        "];",
        "",
        "fn ranged_value<T: Copy + Default>(codepoint: u32, ranges: &[(u32, u32, T)]) -> T {",
        "    let index = ranges.partition_point(|(start, _, _)| *start <= codepoint);",
        "    if index == 0 {",
        "        return T::default();",
        "    }",
        "    let (start, end, value) = ranges[index - 1];",
        "    if codepoint >= start && codepoint <= end { value } else { T::default() }",
        "}",
        "",
        "fn has(character: char, flag: u16) -> bool {",
        "    ranged_value(character as u32, PROPERTIES) & flag != 0",
        "}",
        "",
        "pub fn is_alpha(character: char) -> bool { has(character, ALPHA) }",
        "pub fn is_alphanumeric(character: char) -> bool { has(character, ALNUM) }",
        "pub fn is_lower(character: char) -> bool { has(character, LOWER) }",
        "pub fn is_upper(character: char) -> bool { has(character, UPPER) }",
        "pub fn is_cased(character: char) -> bool { has(character, CASED) }",
        "pub fn is_space(character: char) -> bool { has(character, SPACE) }",
        "pub fn is_digit(character: char) -> bool { has(character, DIGIT) }",
        "",
        "pub fn is_attachment(character: char) -> bool {",
        "    let codepoint = character as u32;",
        "    has(character, MARK)",
        "        || (0xfe00..=0xfe0f).contains(&codepoint)",
        "        || (0xe0100..=0xe01ef).contains(&codepoint)",
        "        || (0x1f3fb..=0x1f3ff).contains(&codepoint)",
        "}",
        "",
        "pub fn is_separator(character: char) -> bool {",
        "    has(character, SPACE | SEPARATOR)",
        "}",
        "",
        "pub fn script(character: char) -> u8 {",
        "    ranged_value(character as u32, SCRIPTS)",
        "}",
        "",
        "pub fn is_unsegmented(script: u8) -> bool {",
        "    matches!(script, 19 | 20 | 22 | 24 | 27 | 29 | 30 | 31)",
        "}",
        "",
        "pub fn case_fold(text: &str) -> String {",
        "    let mut out = String::with_capacity(text.len());",
        "    for character in text.chars() {",
        "        match CASEFOLD.binary_search_by_key(&(character as u32), |(key, _)| *key) {",
        "            Ok(index) => out.push_str(CASEFOLD[index].1),",
        "            Err(_) => out.push(character),",
        "        }",
        "    }",
        "    out",
        "}",
        "",
    ])
    source = "\n".join(lines)
    return subprocess.run(
        ["rustfmt", "--edition", "2021"],
        input=source,
        text=True,
        encoding="utf-8",
        errors="strict",
        capture_output=True,
        check=True,
    ).stdout


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if unicodedata.unidata_version != UNICODE_VERSION:
        parser.error(f"Python Unicode {UNICODE_VERSION} required, got {unicodedata.unidata_version}")
    generated = ((ORACLE_PATH, oracle_text()), (RUST_PATH, rust_text()))
    if args.check:
        stale = [str(path) for path, content in generated if path.read_text(encoding="utf-8") != content]
        if stale:
            parser.error("stale generated files: " + ", ".join(stale))
        return 0
    for path, content in generated:
        path.write_text(content, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
