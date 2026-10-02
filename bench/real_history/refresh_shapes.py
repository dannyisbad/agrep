#!/usr/bin/env python3
"""Sample one scrubbed record per distinct shape from the real stores into bench/fixtures/real_shapes."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import sandbox  # noqa: E402
import scrub  # noqa: E402
import shapes  # noqa: E402
import validate_repo_privacy  # noqa: E402

DEFAULT_OUT = sandbox.ROOT / "bench" / "fixtures" / "real_shapes"
STRUCTURAL_TOKENS = frozenset((
    "projects", "project", "home", "users", "desktop", "documents", "downloads", "library",
    "application", "support", "private", "local", "share", "tmp", "var", "folders", "work",
    "code", "src", "dev", "main", "master", "test", "tests", "docs", "agent", "agents", "data",
    "app", "web", "api", "cli", "lib", "bin", "sessions", "session", "advisor", "rollout",
    "subagents", "jsonl", "json", "archive", "opencode", "claude", "codex", "omp",
))


@dataclass
class FileScan:
    adapter: str
    path: Path
    relative: Path
    fclass: str
    size: int
    seen: int = 0
    malformed: int = 0
    first_by_shape: dict[str, int] = field(default_factory=dict)
    context: list[int] = field(default_factory=list)
    anchor: int | None = None
    cwd: str | None = None


@dataclass
class Planned:
    scan: FileScan
    ordinals: list[int]
    records: dict[int, object]
    malformed: dict[int, str] = field(default_factory=dict)
    companions: list[tuple[int, int, str, int]] = field(default_factory=list)


def discover(binary: Path, home: Path, scratch: Path) -> dict[str, list[Path]]:
    env = sandbox.sealed_env(home=home, data=scratch / "data", scratch=scratch, binary=binary)
    listing = subprocess.run([str(binary), "stores", "--paths"], env=env, capture_output=True,
                             text=True, check=True, timeout=900)
    found: dict[str, list[Path]] = defaultdict(list)
    for row in json.loads(listing.stdout):
        if row.get("state") == "available":
            found[row["name"]].append(Path(row["path"]))
    return found


def store_relative(adapter: str, home: Path, path: Path) -> Path:
    relative = path.relative_to(home)
    anchor = {"claude": 2, "codex": 2, "pi": 3}[adapter]
    if adapter == "pi" and relative.parts[2] == "archive":
        anchor = 4
    return Path(*relative.parts[anchor:])


def scan_file(adapter: str, home: Path, path: Path) -> FileScan:
    relative = store_relative(adapter, home, path)
    scan = FileScan(adapter, path, relative, shapes.file_class(adapter, relative), path.stat().st_size)
    seen_context = {"header": False, "context": False, "cwd": False}
    for ordinal, _raw, record in shapes.iter_records(path):
        scan.seen += 1
        if record is None:
            scan.malformed += 1
            key = f"{scan.fclass}|<malformed>"
        else:
            key = f"{scan.fclass}|{shapes.shape(record)}"
        scan.first_by_shape.setdefault(key, ordinal)
        if record is None or not isinstance(record, dict):
            continue
        kind = shapes.classify(adapter, record)
        if kind in ("header", "context") and not seen_context[kind]:
            seen_context[kind] = True
            scan.context.append(ordinal)
            if adapter == "pi" and isinstance(record.get("cwd"), str):
                scan.cwd = record["cwd"]
            if adapter == "codex":
                payload = record.get("payload")
                if kind == "header" and isinstance(payload, dict) and isinstance(payload.get("cwd"), str):
                    scan.cwd = payload["cwd"]
        if adapter == "claude" and not seen_context["cwd"] and isinstance(record.get("cwd"), str):
            seen_context["cwd"] = True
            scan.cwd = record["cwd"]
            scan.context.append(ordinal)
        if scan.anchor is None and kind == "user":
            scan.anchor = ordinal
    if adapter == "claude" and scan.fclass == "side" and scan.seen and 0 not in scan.context:
        scan.context.insert(0, 0)
    return scan


def set_cover(scans: list[FileScan], max_files: int) -> tuple[list[FileScan], set[str]]:
    covered: set[str] = set()
    chosen: list[FileScan] = []
    remaining = list(scans)
    while remaining and len(chosen) < max_files:
        best = max(remaining, key=lambda scan: (len(set(scan.first_by_shape) - covered), -scan.size))
        gain = set(best.first_by_shape) - covered
        if not gain:
            break
        covered |= gain
        chosen.append(best)
        remaining.remove(best)
    return chosen, covered


def plan_file(scan: FileScan, claimed: Counter, per_shape: int) -> Planned:
    wanted = set(scan.context)
    if scan.anchor is not None:
        wanted.add(scan.anchor)
    for key, ordinal in sorted(scan.first_by_shape.items(), key=lambda item: item[1]):
        if claimed[key] < per_shape:
            claimed[key] += 1
            wanted.add(ordinal)
    records: dict[int, object] = {}
    malformed: dict[int, str] = {}
    for ordinal, raw, record in shapes.iter_records(scan.path):
        if scan.adapter != "codex" and ordinal not in wanted:
            continue
        records[ordinal] = record
        if record is None:
            malformed[ordinal] = raw.decode("utf-8", "replace")
    companions: list[tuple[int, int, str, int]] = []
    if scan.adapter == "codex":
        companions = codex_companions(records, wanted)
        wanted |= {companion for _user, companion, _kind, _length in companions}
    return Planned(scan, sorted(wanted), records, malformed, companions)


def codex_companions(records: dict[int, object], wanted: set[int]) -> list[tuple[int, int, str, int]]:
    """For every kept user message, keep one attesting submission event (legacy or Desktop)."""
    submissions: list[tuple[int, str, str, str | None, str | None]] = []
    session = None
    for ordinal, record in sorted(records.items()):
        if not isinstance(record, dict):
            continue
        payload = record.get("payload")
        if record.get("type") == "session_meta" and isinstance(payload, dict):
            session = payload.get("id") if isinstance(payload.get("id"), str) else None
        if shapes.classify("codex", record) != "submission":
            continue
        if payload.get("type") == "user_message":
            if not isinstance(payload.get("message"), str):
                continue
            submissions.append((ordinal, "legacy", payload["message"].strip(), None, None))
        else:
            item = payload["item"]
            text = "\n".join(block.get("text") for block in item.get("content") or []
                             if isinstance(block, dict) and block.get("type") == "text"
                             and isinstance(block.get("text"), str)).strip()
            submissions.append((ordinal, "desktop", text, payload.get("thread_id"), payload.get("turn_id")))
    pairs = []
    for ordinal in sorted(wanted):
        record = records.get(ordinal)
        if not isinstance(record, dict) or shapes.classify("codex", record) != "user":
            continue
        payload = record["payload"]
        text = shapes.codex_text(payload.get("content")).strip()
        turn = payload.get("turn_id")
        if not isinstance(turn, str):
            meta = payload.get("internal_chat_message_metadata_passthrough")
            turn = meta.get("turn_id") if isinstance(meta, dict) else None
        for sub_ordinal, kind, submitted, thread, sub_turn in submissions:
            if kind == "legacy" and text.startswith(submitted):
                pairs.append((ordinal, sub_ordinal, kind, len(submitted)))
                break
            if kind == "desktop" and thread == session and sub_turn == turn and submitted == text:
                pairs.append((ordinal, sub_ordinal, kind, len(submitted)))
                break
    return pairs


def slug_for(mapped_cwd: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", mapped_cwd)


def output_relative(planned: Planned, scrubber: scrub.Scrubber, home: Path) -> Path:
    adapter = planned.scan.adapter
    home_relative = planned.scan.path.relative_to(home)
    top = home_relative.parts[0].lstrip(".")
    if adapter == "codex":
        name = scrubber.filename_token(home_relative.parts[-1])
        match = re.search(r"\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}", name)
        if match:
            stamp = match.group(0)
        else:
            stamp = scrubber.shift_stamp("-".join(home_relative.parts[-4:-1]) + "T00-00-00")
        return Path(top, "sessions", stamp[:4], stamp[5:7], stamp[8:10], name)
    store_parts = list(home_relative.parts[1:])
    if adapter == "claude":
        slug_index = 1
    else:
        slug_index = 3 if store_parts[1] == "archive" else 2
    mapped = []
    for index, part in enumerate(store_parts):
        if index < slug_index:
            mapped.append(part)
        elif index == slug_index:
            if planned.scan.cwd:
                mapped.append(slug_for(scrubber.path(planned.scan.cwd)))
            else:
                mapped.append(scrubber.filename_token(part))
        else:
            mapped.append(scrubber.filename_token(part))
    return Path(top, *mapped)


def scrub_planned(planned: Planned, scrubber: scrub.Scrubber, *, max_record_bytes: int,
                  max_file_bytes: int) -> tuple[list[bytes], dict[str, int]]:
    notes: Counter = Counter()
    scrubbed: dict[int, object] = {}
    protected = set(planned.scan.context)
    if planned.scan.anchor is not None:
        protected.add(planned.scan.anchor)
    for ordinal in planned.ordinals:
        record = planned.records[ordinal]
        if record is None:
            scrubbed[ordinal] = None
            continue
        scrubbed[ordinal] = scrubber.value(record)
    if planned.scan.adapter == "codex":
        repair_codex_companions(planned, scrubbed)
    if planned.scan.adapter == "pi":
        relink_pi(planned.ordinals, scrubbed)
    lines: dict[int, bytes] = {}
    for ordinal in planned.ordinals:
        record = scrubbed[ordinal]
        if record is None:
            filler = scrubber.text(planned.malformed[ordinal].strip() or "x")
            line = b"{" + filler.encode("utf-8")[:200] + b"\n"
        else:
            line = json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(line) > max_record_bytes and ordinal not in protected:
            notes["oversize_dropped"] += 1
            continue
        lines[ordinal] = line
    total = sum(len(line) for line in lines.values())
    while total > max_file_bytes:
        droppable = [ordinal for ordinal in lines if ordinal not in protected]
        if not droppable:
            break
        victim = max(droppable, key=lambda ordinal: len(lines[ordinal]))
        total -= len(lines.pop(victim))
        notes["file_cap_dropped"] += 1
    notes["records"] = len(lines)
    notes["bytes"] = total
    return [lines[ordinal] for ordinal in sorted(lines)], dict(notes)


def repair_codex_companions(planned: Planned, scrubbed: dict[int, object]) -> None:
    """Re-establish prefix/equality between a scrubbed user message and its submission event."""
    for user_ordinal, companion_ordinal, kind, length in planned.companions:
        user = scrubbed.get(user_ordinal)
        companion = scrubbed.get(companion_ordinal)
        if not isinstance(user, dict) or not isinstance(companion, dict):
            continue
        text = shapes.codex_text(user["payload"].get("content")).strip()
        payload = companion["payload"]
        if kind == "legacy":
            payload["message"] = text[:length]
        else:
            first = True
            for block in payload["item"].get("content") or []:
                if isinstance(block, dict) and block.get("type") == "text":
                    block["text"] = text if first else ""
                    first = False


def relink_pi(ordinals: list[int], scrubbed: dict[int, object]) -> None:
    """Kept records form one active branch: each parentId points at the previous kept record."""
    previous = None
    for ordinal in ordinals:
        record = scrubbed[ordinal]
        if not isinstance(record, dict) or record.get("type") == "session":
            continue
        if "parentId" in record:
            record["parentId"] = previous
        if isinstance(record.get("id"), str):
            previous = record["id"]


def observe_planned(planned: list[Planned]) -> list[datetime]:
    found: list[datetime] = []
    for item in planned:
        for ordinal in item.ordinals:
            record = item.records[ordinal]
            if record is not None:
                scrub.observe_timestamps(record, None, found)
    return found


# -- opencode ---------------------------------------------------------------------------

@dataclass
class OpencodePlan:
    schema: str
    tables: dict[str, list[tuple[str, str, int, int]]]
    rows: dict[str, list[dict]]
    shapes_total: int
    shapes_covered: int
    sessions_total: int


def plan_opencode(db_path: Path, per_shape: int, max_sessions: int) -> OpencodePlan:
    source = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    memory = sqlite3.connect(":memory:")
    source.backup(memory)
    source.close()
    memory.row_factory = sqlite3.Row
    names = {row["name"] for row in memory.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "session_message" in names:
        schema = "v2"
        session_table, message_table = "session_v2", "session_message"
        order = "session_id, seq, time_created, id"
    elif "message" in names:
        schema = "v1"
        session_table, message_table = "session", "message"
        order = "session_id, time_created, id"
    else:
        raise SystemExit("opencode database has neither session_message nor message tables")
    tables = {}
    for table in (session_table, message_table) + (("part",) if schema == "v1" else ()):
        tables[table] = [(row["name"], row["type"], row["notnull"], row["pk"])
                         for row in memory.execute(f"PRAGMA table_info({table})")]
    sessions = {row["id"]: dict(row) for row in memory.execute(f"SELECT * FROM {session_table}")}
    messages: dict[str, list[dict]] = defaultdict(list)
    for row in memory.execute(f"SELECT * FROM {message_table} ORDER BY {order}"):
        messages[row["session_id"]].append(dict(row))
    parts: dict[str, list[dict]] = defaultdict(list)
    if schema == "v1":
        for row in memory.execute("SELECT * FROM part ORDER BY message_id, time_created, id"):
            parts[row["message_id"]].append(dict(row))
    memory.close()

    def message_shape(message: dict) -> str:
        try:
            data = json.loads(message.get("data") or "null")
        except ValueError:
            data = "<malformed>"
        own = f"{message.get('type') or ''}|{shapes.shape(data)}"
        if schema == "v1":
            own += "|" + "|".join(sorted({shapes.shape(_json_or_marker(part.get("data")))
                                          for part in parts.get(message["id"], [])}))
        return own

    per_session: dict[str, dict[str, int]] = {}
    for session_id, rows in messages.items():
        first: dict[str, int] = {}
        for index, message in enumerate(rows):
            first.setdefault(message_shape(message), index)
        per_session[session_id] = first
    all_shapes = set().union(*per_session.values()) if per_session else set()
    covered: set[str] = set()
    chosen: list[str] = []
    while len(chosen) < max_sessions:
        best = max((sid for sid in per_session if sid not in chosen),
                   key=lambda sid: len(set(per_session[sid]) - covered), default=None)
        if best is None or not set(per_session[best]) - covered:
            break
        covered |= set(per_session[best])
        chosen.append(best)
    claimed: Counter = Counter()
    out_rows: dict[str, list[dict]] = {session_table: [], message_table: []}
    if schema == "v1":
        out_rows["part"] = []
    for sid in chosen:
        if sid in sessions:
            out_rows[session_table].append(sessions[sid])
        rows = messages[sid]
        keep = set()
        anchor = next((index for index, message in enumerate(rows)
                       if (message.get("type") == "user" if schema == "v2"
                           else '"role":"user"' in (message.get("data") or ""))), None)
        if anchor is not None:
            keep.add(anchor)
        for key, index in per_session[sid].items():
            if claimed[key] < per_shape:
                claimed[key] += 1
                keep.add(index)
        for index in sorted(keep):
            out_rows[message_table].append(rows[index])
            if schema == "v1":
                out_rows["part"].extend(parts.get(rows[index]["id"], []))
    for sid in chosen:
        parent = sessions.get(sid, {}).get("parent_id")
        if parent and parent in sessions and parent not in chosen:
            out_rows[session_table].append(sessions[parent])
    return OpencodePlan(schema, tables, out_rows, len(all_shapes), len(covered), len(sessions))


def _json_or_marker(text):
    try:
        return json.loads(text or "null")
    except ValueError:
        return "<malformed>"


def observe_opencode(plan: OpencodePlan) -> list[datetime]:
    found: list[datetime] = []
    for rows in plan.rows.values():
        for row in rows:
            scrub.observe_timestamps(row, None, found)
    return found


def write_opencode(plan: OpencodePlan, scrubber: scrub.Scrubber, out: Path) -> dict[str, int]:
    def literal(value) -> str:
        if value is None:
            return "NULL"
        if isinstance(value, bool):
            return "1" if value else "0"
        if isinstance(value, (int, float)):
            return repr(value)
        if isinstance(value, bytes):
            return "X'" + value.hex() + "'"
        return "'" + str(value).replace("'", "''") + "'"

    lines = ["-- Scrubbed opencode %s store shapes; the harness builds opencode.db from this." % plan.schema]
    rows_written = 0
    for table, columns in plan.tables.items():
        spec = ", ".join(
            f"{name} {ctype}{' PRIMARY KEY' if pk else ''}{' NOT NULL' if notnull and not pk else ''}"
            for name, ctype, notnull, pk in columns)
        lines.append(f"CREATE TABLE {table}({spec});")
        for row in plan.rows.get(table, []):
            scrubbed = {}
            for name, _ctype, _notnull, _pk in columns:
                value = row.get(name)
                if isinstance(value, bytes):
                    scrubbed[name] = bytes(len(value))
                elif isinstance(value, str) and name == "data":
                    parsed = _json_or_marker(value)
                    if isinstance(parsed, (dict, list)):
                        scrubbed[name] = json.dumps(scrubber.value(parsed, name, 0, table),
                                                    ensure_ascii=False, separators=(",", ":"))
                    else:
                        scrubbed[name] = scrubber.text(value)
                else:
                    scrubbed[name] = scrubber.value(value, name, 1, table)
            names = ", ".join(name for name, *_ in columns)
            values = ", ".join(literal(scrubbed[name]) for name, *_ in columns)
            lines.append(f"INSERT INTO {table}({names}) VALUES ({values});")
            rows_written += 1
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"rows": rows_written, "bytes": out.stat().st_size}


# -- scans ------------------------------------------------------------------------------

def fixture_files(out: Path) -> list[Path]:
    return sorted(path for path in out.rglob("*") if path.is_file())


def project_tokens(extra: list[str]) -> set[str]:
    tokens = {token.lower() for token in extra if len(token) >= 3}
    tokens.add(Path.home().name.lower())
    agrep = shutil.which("agrep")
    if agrep:
        listing = subprocess.run([agrep, "chats", "--json", "-n", "5000"], capture_output=True,
                                 text=True, timeout=300, check=False)
        for line in listing.stdout.splitlines():
            if not line.startswith("{"):
                continue
            row = json.loads(line)
            for token in re.split(r"[^A-Za-z0-9]+", row.get("project", "")):
                if len(token) >= 4:
                    tokens.add(token.lower())
    else:
        print("warning: installed `agrep` not on PATH; project tokens limited to --name-token",
              file=sys.stderr)
    return {token for token in tokens if token not in STRUCTURAL_TOKENS}


def _walk_tokens(value, keys: set[str], values: set[str]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            keys |= scrub._alnum_tokens(str(key))
            _walk_tokens(child, keys, values)
    elif isinstance(value, list):
        for child in value:
            _walk_tokens(child, keys, values)
    elif isinstance(value, str):
        nested = None
        if value.lstrip()[:1] in "{[":
            try:
                nested = json.loads(value)
            except ValueError:
                nested = None
        if isinstance(nested, (dict, list)):
            _walk_tokens(nested, keys, values)
        else:
            values |= scrub._alnum_tokens(value)


def name_scan(out: Path, forbidden: set[str]) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Forbidden tokens in paths or string values are leaks; in schema keys they are review items."""
    vocabulary = scrub.vocabulary_tokens()
    hits: list[tuple[str, str]] = []
    collisions: list[tuple[str, str]] = []
    for path in fixture_files(out):
        relative = path.relative_to(out).as_posix()
        if path.name == "manifest.json":
            continue
        keys: set[str] = set()
        values: set[str] = scrub._alnum_tokens(relative)
        if path.suffix == ".sql":
            with sqlite3.connect(":memory:") as memory:
                memory.executescript(path.read_text(encoding="utf-8"))
                for (table,) in memory.execute("SELECT name FROM sqlite_master WHERE type='table'"):
                    cursor = memory.execute(f"SELECT * FROM {table}")
                    columns = [column[0] for column in cursor.description]
                    for row in cursor:
                        _walk_tokens(dict(zip(columns, row)), keys, values)
        else:
            with path.open("rb") as handle:
                for raw in handle:
                    try:
                        _walk_tokens(json.loads(raw), keys, values)
                    except ValueError:
                        values |= scrub._alnum_tokens(raw.decode("utf-8", "replace"))
        for token in sorted((forbidden & values) - vocabulary):
            hits.append((relative, token))
        for token in sorted((forbidden & keys) - vocabulary - values):
            collisions.append((relative, token))
    return hits, collisions


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--home", type=Path, default=Path.home(), help="store home to sample (read-only)")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--binary", type=Path, default=sandbox.resolve_binary())
    parser.add_argument("--adapters", default="claude,codex,pi,opencode")
    parser.add_argument("--per-shape", type=int, default=1, help="representatives kept per shape")
    parser.add_argument("--max-files", type=int, default=48, help="source files chosen per adapter")
    parser.add_argument("--max-string-chars", type=int, default=2000)
    parser.add_argument("--max-record-bytes", type=int, default=200_000)
    parser.add_argument("--max-file-bytes", type=int, default=900_000)
    parser.add_argument("--name-token", action="append", default=[],
                        help="extra forbidden token for the final scan (never stored)")
    parser.add_argument("--keep-on-findings", action="store_true",
                        help="leave the output in place when the privacy scan fails (review only)")
    args = parser.parse_args(argv)
    adapters = [name.strip() for name in args.adapters.split(",") if name.strip()]
    home = args.home.resolve()
    out = args.out.resolve()
    if not args.binary.is_file():
        print(f"ingest binary missing: {args.binary}", file=sys.stderr)
        return 2
    if sandbox.ROOT not in out.parents:
        print(f"refusing to write outside the checkout: {out}", file=sys.stderr)
        return 2
    scratch = Path(tempfile.mkdtemp(prefix="agrep-real-shapes-"))
    scratch.chmod(0o700)
    manifest = {
        "version": 1, "tool": "bench/real_history/refresh_shapes.py",
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "era_origin": scrub.ERA_ORIGIN.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "per_shape": args.per_shape, "max_string_chars": args.max_string_chars,
        "max_record_bytes": args.max_record_bytes, "max_file_bytes": args.max_file_bytes,
        "adapters": {},
    }
    try:
        forbidden = project_tokens(args.name_token)
        found = discover(args.binary, home, scratch)
        planned: dict[str, list[Planned]] = {}
        uncovered: dict[str, list[str]] = {}
        for adapter in adapters:
            if adapter == "opencode":
                continue
            paths = [path for path in found.get(adapter, []) if not path.name.endswith(".gz")]
            skipped_gzip = len(found.get(adapter, [])) - len(paths)
            scans = [scan_file(adapter, home, path) for path in paths]
            chosen, covered = set_cover(scans, args.max_files)
            all_shapes = set().union(*(set(scan.first_by_shape) for scan in scans)) if scans else set()
            claimed: Counter = Counter()
            planned[adapter] = [plan_file(scan, claimed, args.per_shape) for scan in chosen]
            uncovered[adapter] = sorted(all_shapes - covered)
            manifest["adapters"][adapter] = {
                "source_files": len(paths), "source_bytes": sum(scan.size for scan in scans),
                "source_records": sum(scan.seen for scan in scans),
                "malformed_records": sum(scan.malformed for scan in scans),
                "skipped_gzip": skipped_gzip, "shapes_total": len(all_shapes),
                "shapes_covered": len(covered), "files_chosen": len(chosen),
                "file_classes": dict(Counter(scan.fclass for scan in chosen)),
            }
            print(f"{adapter}: {len(paths)} files, {len(all_shapes)} shapes, "
                  f"{len(covered)} covered by {len(chosen)} files", flush=True)
        opencode_plans = []
        if "opencode" in adapters:
            for db_path in found.get("opencode", []):
                plan = plan_opencode(db_path, args.per_shape, args.max_files)
                opencode_plans.append(plan)
                print(f"opencode: {plan.schema} {plan.sessions_total} sessions, "
                      f"{plan.shapes_total} shapes, {plan.shapes_covered} covered", flush=True)
        observed = []
        for items in planned.values():
            observed.extend(observe_planned(items))
        for plan in opencode_plans:
            observed.extend(observe_opencode(plan))
        earliest = min(observed) if observed else None
        scrubber = scrub.Scrubber(era_delta=scrub.era_delta_for(earliest),
                                  max_string_chars=args.max_string_chars, forbidden=forbidden)
        if out.exists():
            shutil.rmtree(out)
        out.mkdir(parents=True)
        for adapter, items in planned.items():
            written = Counter()
            for item in items:
                lines, notes = scrub_planned(item, scrubber, max_record_bytes=args.max_record_bytes,
                                             max_file_bytes=args.max_file_bytes)
                target = out / output_relative(item, scrubber, home)
                if target.exists():
                    raise SystemExit(f"mapped fixture path collision: {target.relative_to(out)}")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"".join(lines))
                written.update(notes)
                written["files"] += 1
            manifest["adapters"][adapter].update(dict(written))
            manifest["adapters"][adapter]["uncovered_shapes"] = uncovered[adapter]
        for index, plan in enumerate(opencode_plans):
            suffix = "" if index == 0 else f"-{index}"
            target = out / "opencode" / f"seed{suffix}.sql"
            notes = write_opencode(plan, scrubber, target)
            manifest["adapters"][f"opencode{suffix}"] = {
                "schema": plan.schema, "sessions_total": plan.sessions_total,
                "shapes_total": plan.shapes_total, "shapes_covered": plan.shapes_covered, **notes}
        manifest["scrub_stats"] = scrubber.stats
        hits, key_collisions = name_scan(out, forbidden)
        manifest["name_scan"] = {"value_hits": len(hits), "schema_key_collisions": len(key_collisions),
                                 "forbidden_tokens": len(forbidden)}
        (out / "manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n",
                                           encoding="utf-8")
        findings = validate_repo_privacy.scan_worktree(sandbox.ROOT, fixture_files(out))
        for path, reason in findings:
            print(f"privacy: {path}: {reason}", file=sys.stderr)
        for path, token in hits:
            print(f"name scan: {path}: value token of length {len(token)} matched", file=sys.stderr)
        for path, token in key_collisions:
            print(f"name scan (schema key, review): {path}: key token of length {len(token)}",
                  file=sys.stderr)
        if findings or hits:
            if not args.keep_on_findings:
                shutil.rmtree(out)
                print("scan failed; output deleted", file=sys.stderr)
            return 1
        total = sum(path.stat().st_size for path in fixture_files(out))
        print(f"wrote {len(fixture_files(out))} fixture files, {total} bytes, "
              f"privacy scan clean, name scan clean -> {out}")
        return 0
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
