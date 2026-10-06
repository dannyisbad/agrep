"""Scale invariants over one agrep data dir; every check reports aggregate counts only."""

from __future__ import annotations

import hashlib
import json
import random
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import scrub
import shapes

GENERIC_CONTAINERS = frozenset((
    "projects", "project", "private", "tmp", "temp", "users", "home", "var", "folders",
    "desktop", "documents", "downloads", "src", "repos", "repositories", "code", "git",
    "github", "work", "dev", "workspace", "workspaces", "t", "local", "library", "onedrive",
    "appdata", "roaming", "locallow",
))
# pi publishes the raw cwd by contract (conformance goldens pin `<HOME>/projects/cedar`),
# so only name-form labels are held to the container rule.
PATH_LABEL_ADAPTERS = frozenset(("pi",))
DURABLE_ARTIFACTS = ("messages.jsonl", "sessions.jsonl", "replies.jsonl", "intake_stats.json",
                     "boundary_stats.json", "event_stats.json")
_FNV_OFFSET_16 = 0xCBF29CE484222325 & 0xFFFF
_FNV_PRIME_16 = 0x100000001B3 & 0xFFFF
IN_CHAT_HITS = 100_000


@dataclass
class Check:
    name: str
    ok: bool
    counts: dict = field(default_factory=dict)
    detail: str = ""

    def as_dict(self) -> dict:
        return {"name": self.name, "ok": self.ok, "counts": self.counts, "detail": self.detail}


def content_digest(text: str) -> str:
    digest = _FNV_OFFSET_16
    for byte in (text or "").encode("utf-8"):
        digest = ((digest ^ byte) * _FNV_PRIME_16) & 0xFFFF
    return f"{digest:04x}"


def read_jsonl(path: Path) -> list[dict]:
    rows = []
    with path.open("rb") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def intake_book(data: Path) -> dict[str, dict]:
    book = json.loads((data / "intake_stats.json").read_text(encoding="utf-8"))
    return book.get("files", {})


def artifact_hashes(data: Path) -> dict[str, str]:
    """Durable publication digests; event payloads are hashed logically out of the event store."""
    hashes = {}
    for name in DURABLE_ARTIFACTS:
        path = data / name
        if path.is_file():
            hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    store = data / "events" / ".store.sqlite3"
    if store.is_file():
        digest = hashlib.sha256()
        with sqlite3.connect(f"file:{store}?mode=ro", uri=True) as connection:
            for row in connection.execute(
                    "SELECT name, agent, session, hash, n_events, payload FROM event_sessions "
                    "ORDER BY name"):
                digest.update(repr(row[:5]).encode())
                digest.update(row[5] if isinstance(row[5], bytes) else bytes(row[5]))
        hashes["events/.store.sqlite3"] = digest.hexdigest()
    return hashes


def check_intake_identity(book: dict[str, dict]) -> Check:
    broken = 0
    rows_over_seen = 0
    negative = 0
    errors = 0
    per_agent: Counter = Counter()
    for entry in book.values():
        skips = sum(entry.get("skips", {}).values())
        seen = entry.get("seen", 0)
        rows = entry.get("rows", 0)
        agent_rows = entry.get("agent_rows", 0)
        file_errors = entry.get("errors", 0)
        per_agent[entry.get("agent", "?")] += 1
        errors += file_errors
        if min(seen, rows, agent_rows, file_errors, *entry.get("skips", {}).values(), 0) < 0:
            negative += 1
        if rows > seen:
            rows_over_seen += 1
        if seen != rows + agent_rows + skips + file_errors:
            broken += 1
    return Check(
        "intake_identity", broken == 0 and rows_over_seen == 0 and negative == 0,
        {"files": len(book), "identity_broken": broken, "rows_over_seen": rows_over_seen,
         "negative_counts": negative, "errors_total": errors, "files_per_agent": dict(per_agent)})


def tallied_extent(entry: dict, source: Path) -> tuple[str, int | None]:
    """How much of `source` the tally covered: ("whole", None) while its `s:<mtime>:<size>` key still
    matches, ("prefix", size) when the file only grew since (append-only JSONL), ("uncomparable",
    None) when it was rewritten, shrank, or is compressed, so no byte range maps to the tally."""
    key = str(entry.get("key", ""))
    parts = key.split(":")
    if len(parts) != 3 or parts[0] != "s" or not parts[2].isdigit():
        return "whole", None
    stat = source.stat()
    if key == f"s:{stat.st_mtime_ns // 1_000_000}:{stat.st_size}":
        return "whole", None
    size = int(parts[2])
    if stat.st_size > size and not source.name.endswith(".gz"):
        return "prefix", size
    return "uncomparable", None


def check_source_bounds(book: dict[str, dict], home: Path, adapters=shapes.JSONL_ADAPTERS,
                        sample: int | None = None, seed: int = 7) -> Check:
    """Rows never exceed text-bearing non-synthetic candidates; synthetic mirrors are skips.
    Live stores grow between tally and oracle, so the oracle reads the tallied extent."""
    candidates = [(path, entry) for path, entry in book.items()
                  if entry.get("agent") in adapters and not path.startswith("\0")]
    if sample is not None and len(candidates) > sample:
        candidates = random.Random(seed).sample(candidates, sample)
    violations: Counter = Counter()
    checked: Counter = Counter()
    extents: Counter = Counter()
    synthetic_total = 0
    rows_total = 0
    bound_total = 0
    for path, entry in candidates:
        source = Path(path)
        if not source.is_file():
            violations["missing_source"] += 1
            continue
        adapter = entry["agent"]
        extent, limit = tallied_extent(entry, source)
        extents[extent] += 1
        if extent == "uncomparable":
            continue
        counts = shapes.oracle_counts(adapter, source, limit)
        checked[adapter] += 1
        bound = shapes.text_bearing_bound(adapter, counts)
        rows = entry.get("rows", 0)
        rows_total += rows
        bound_total += bound
        if rows > bound:
            violations[f"{adapter}:rows_over_candidates"] += 1
        skips = entry.get("skips", {})
        synthetic = counts.get("synthetic_user", 0)
        synthetic_total += synthetic
        if synthetic > skips.get("sidechain", 0) + skips.get("unreferenced", 0):
            violations[f"{adapter}:synthetic_not_skipped"] += 1
        if entry.get("seen", 0) != counts["seen"]:
            violations[f"{adapter}:seen_mismatch"] += 1
    return Check(
        "source_bounds", not violations,
        {"files_checked": dict(checked), "rows": rows_total, "candidate_bound": bound_total,
         "synthetic_records": synthetic_total, "tallied_extent": dict(extents),
         "violations": dict(violations)})


def check_coverage(discovered: list[tuple[str, Path]], book: dict[str, dict],
                   sessions: list[dict]) -> Check:
    """Every discovered content file has a tally, and a file with rows names a published session."""
    published = {row["session"] for row in sessions}
    aliases = {row["alias"] for row in sessions if row.get("alias")}
    missing = 0
    unmapped = 0
    empty = 0
    contributing = 0
    skipped_only = 0
    for adapter, path in discovered:
        entry = book.get(str(path))
        if entry is None:
            missing += 1
            continue
        if entry.get("seen", 0) == 0:
            empty += 1
            continue
        if entry.get("rows", 0) > 0:
            contributing += 1
            if adapter in shapes.JSONL_ADAPTERS:
                ids = shapes.source_session_ids(adapter, path)
                if not (ids & published or ids & aliases):
                    unmapped += 1
        else:
            skipped_only += 1
    return Check(
        "file_coverage", missing == 0 and unmapped == 0,
        {"discovered": len(discovered), "without_tally": missing, "rows_without_session": unmapped,
         "empty_files": empty, "contributing": contributing, "skips_only": skipped_only})


def check_duplicate_ids(messages: list[dict]) -> Check:
    ids = Counter(row["id"] for row in messages)
    turns = Counter((row["session"], row["turn"]) for row in messages)
    dup_ids = sum(1 for count in ids.values() if count > 1)
    dup_turns = sum(1 for count in turns.values() if count > 1)
    return Check("duplicate_ids", dup_ids == 0 and dup_turns == 0,
                 {"messages": len(messages), "duplicate_ids": dup_ids, "duplicate_turns": dup_turns})


def check_family_closure(messages: list[dict], sessions: list[dict], corpus: Path) -> Check:
    message_sessions = {row["session"] for row in messages}
    index_sessions = {row["session"] for row in sessions}
    counts = {"sessions": len(index_sessions)}
    problems: Counter = Counter()
    if message_sessions != index_sessions:
        problems["message_session_mismatch"] = len(message_sessions ^ index_sessions)
    per_session = Counter(row["session"] for row in messages)
    for row in sessions:
        if per_session.get(row["session"]) != row.get("n"):
            problems["n_mismatch"] += 1
    parents = {row["session"]: row["parent"] for row in sessions if row.get("parent")}
    aliases = {row["alias"]: row["session"] for row in sessions if row.get("alias")}
    counts["side_sessions"] = len(parents)
    counts["aliases"] = len(aliases)
    counts["orphan_parents"] = sum(1 for parent in parents.values() if parent not in index_sessions)
    alias_counter = Counter(row["alias"] for row in sessions if row.get("alias"))
    problems["alias_claimed_twice"] = sum(1 for n in alias_counter.values() if n > 1)
    problems["alias_names_indexed_session"] = sum(1 for alias in aliases if alias in index_sessions)
    with sqlite3.connect(f"file:{corpus}?mode=ro", uri=True) as connection:
        family = {session: (root, side) for session, root, side in connection.execute(
            "SELECT session, root, side FROM session_family")}
    counts["family_rows"] = len(family)
    for session in index_sessions:
        entry = family.get(session)
        if entry is None:
            problems["session_without_family_row"] += 1
            continue
        root, side = entry
        if bool(side) != (session in parents):
            problems["side_flag_mismatch"] += 1
        if root not in family or family[root][0] != root:
            problems["root_not_fixed_point"] += 1
    for alias, session in aliases.items():
        entry = family.get(alias)
        if entry is None or session not in family or entry[0] != family[session][0]:
            problems["alias_family_mismatch"] += 1
    problems = +problems
    return Check("family_closure", not problems, {**counts, "problems": dict(problems)})


def _source_cwd(adapter: str, path: Path) -> str | None:
    for ordinal, (_offset, _raw, record) in enumerate(shapes.iter_records(path)):
        if ordinal >= 400 or not isinstance(record, dict):
            if ordinal >= 400:
                break
            continue
        if adapter == "claude" and isinstance(record.get("cwd"), str):
            return record["cwd"]
        if adapter == "codex" and record.get("type") == "session_meta":
            payload = record.get("payload")
            return payload.get("cwd") if isinstance(payload, dict) else None
    return None


def _session_cwds(agent: str, session: str, book: dict[str, dict]) -> list[str]:
    """The starting cwd of every source file the session publishes from."""
    cwds = []
    for path, entry in book.items():
        if entry.get("agent") != agent or path.startswith("\0") or not Path(path).is_file():
            continue
        if session not in shapes.source_session_ids(agent, Path(path)):
            continue
        cwd = _source_cwd(agent, Path(path))
        if cwd is not None:
            cwds.append(cwd)
    return cwds


def _named_in_place(cwd: str, leaf: str) -> bool:
    """True when the cwd's own folder is named `leaf` and its path never uses that name as a container."""
    name = cwd.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].lower()
    return name == leaf and leaf not in scrub.container_segments(cwd)


def check_project_labels(sessions: list[dict], book: dict[str, dict] | None = None) -> Check:
    """Name-form labels are never a container unless the session's own cwd was that bare container."""
    generic: Counter = Counter()
    bare: Counter = Counter()
    named: Counter = Counter()
    empty = 0
    labels: Counter = Counter()
    for row in sessions:
        label = row.get("project", "")
        if not label:
            empty += 1
            continue
        labels[row["agent"]] += 1
        if row["agent"] in PATH_LABEL_ADAPTERS or label.startswith(("/", "~", "\\")) or (
                len(label) > 2 and label[1] == ":"):
            continue
        leaf = label.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].lower()
        if leaf in GENERIC_CONTAINERS or label.lower() in GENERIC_CONTAINERS:
            cwds = _session_cwds(row["agent"], row["session"], book) if book is not None else []
            if cwds and all(scrub.project_root(cwd) is None for cwd in cwds):
                bare[row["agent"]] += 1
            elif row["agent"] == "codex" and cwds and all(_named_in_place(cwd, leaf) for cwd in cwds):
                # Codex labels the cwd's own folder, so a folder named like a container is its label;
                # Claude names the first repo root, so its leaf labels stay mislabels.
                named[row["agent"]] += 1
            else:
                generic[row["agent"]] += 1
    return Check("project_labels", not generic and empty == 0,
                 {"sessions_per_agent": dict(labels), "generic_container_labels": dict(generic),
                  "bare_container_cwd_labels": dict(bare),
                  "container_named_folder_labels": dict(named), "empty_labels": empty})


def check_per_adapter_bounds(messages: list[dict], book: dict[str, dict]) -> Check:
    published: Counter = Counter(row["agent"] for row in messages)
    user_rows: Counter = Counter(row["agent"] for row in messages if row["who"] == "user")
    tallied: Counter = Counter()
    for entry in book.values():
        tallied[entry.get("agent", "?")] += entry.get("rows", 0)
    over = {agent: (published[agent], tallied[agent]) for agent in published
            if published[agent] > tallied[agent]}
    return Check("per_adapter_row_bounds", not over,
                 {"published_rows": dict(published), "user_rows": dict(user_rows),
                  "tallied_rows": dict(tallied), "published_over_tallied": over})


def check_warm_identity(before: dict[str, str], after: dict[str, str]) -> Check:
    changed = sorted(name for name in before if after.get(name) != before[name])
    missing = sorted(name for name in before if name not in after)
    return Check("warm_reindex_identity", not changed and not missing,
                 {"artifacts": len(before), "changed": changed, "missing": missing})


def _parse_handle(handle: str) -> tuple[str, int, str] | None:
    body = handle.lstrip("@")
    if ":" not in body:
        return None
    session, _, rest = body.partition(":")
    rest = rest.split("~", 1)[0]
    turn, _, digest = rest.partition(".")
    if not turn.isdigit() or len(digest) != 4:
        return None
    return session, int(turn), digest


def check_handle_round_trip(runner, messages: list[dict], sessions: list[dict], *,
                            sample: int = 24, seed: int = 11, corpus: Path | None = None) -> Check:
    """search/chats handles reopen through `around --json` at the same session, turn and digest."""
    rng = random.Random(seed)
    by_key = {(row["session"], row["turn"]): row for row in messages}
    published: dict[tuple[str, int], set[str]] = defaultdict(set)
    if corpus is not None:
        with sqlite3.connect(f"file:{corpus}?mode=ro", uri=True) as connection:
            for session, turn, text in connection.execute("SELECT session, turn, text FROM msgs"):
                published[(session, turn)].add(content_digest(text or ""))
    chats = runner.cli(["chats", "--json", "-n", str(max(sample * 4, 50))])
    handles = []
    for row in runner.json_rows(chats):
        handle = row.get("latest_handle")
        if handle:
            handles.append((handle, row["session"], "chat"))
    for row in rng.sample(sessions, min(3, len(sessions))):
        words = [word for word in row.get("first_text", "").split() if word.isalnum() and len(word) >= 4]
        if not words:
            continue
        searched = runner.cli(["search", "--json", "-n", "100", words[0]])
        for hit in runner.json_rows(searched):
            if hit.get("handle") and hit.get("session"):
                handles.append((hit["handle"], hit["session"], hit.get("who") or "hit"))
    if len(handles) > sample:
        handles = rng.sample(handles, sample)
    aliases = [(row["alias"], row["session"]) for row in sessions if row.get("alias")]
    if len(aliases) > sample:
        aliases = rng.sample(aliases, sample)
    failures: Counter = Counter()
    for handle, session, who in handles:
        parsed = _parse_handle(handle)
        if parsed is None:
            failures[f"{who}:unparseable_handle"] += 1
            continue
        prefix, turn, digest = parsed
        if not session.startswith(prefix.split("~")[0]) and prefix != session:
            failures[f"{who}:handle_prefix_mismatch"] += 1
        if (session, turn) not in by_key:
            failures[f"{who}:turn_not_indexed"] += 1
        if corpus is not None and digest not in published.get((session, turn), set()):
            failures[f"{who}:digest_not_in_corpus"] += 1
        opened = runner.cli(["around", handle, "--json"])
        rows = runner.json_rows(opened) if opened.returncode == 0 else []
        meta = next((row for row in rows if row.get("kind") == "agrep-meta"), None)
        if opened.returncode != 0 or meta is None:
            failures[f"{who}:around_failed"] += 1
            payload = opened.stdout.strip().splitlines()[0][:200] if opened.stdout.strip() else ""
            code = re.search(r'"code":\s*"([a-z-]+)"', payload)
            failures[f"{who}:around_{code.group(1) if code else f'rc{opened.returncode}'}"] += 1
            continue
        if meta.get("scope", {}).get("session") != session:
            failures[f"{who}:around_session_mismatch"] += 1
        # A tool hit reopens as its event row (tool, subagent_start, ...) and the turn's prose
        # may be a role-hidden recap. Chat handles carry no role, so around's record role decides.
        role = who if who != "chat" else meta.get("scope", {}).get("selected_record_role")
        at_turn = [row for row in rows if row.get("turn") == turn and row.get("kind") != "agrep-meta"]
        if not any((row.get("kind") != "msg") == (role == "tool") for row in at_turn):
            failures[f"{who}:around_turn_missing"] += 1
    for alias, session in aliases:
        opened = runner.cli(["around", "@" + alias, "--json"])
        rows = runner.json_rows(opened) if opened.returncode == 0 else []
        meta = next((row for row in rows if row.get("kind") == "agrep-meta"), None)
        if meta is None or meta.get("scope", {}).get("session") != session:
            failures["alias_around_mismatch"] += 1
    return Check("handle_round_trip", not failures,
                 {"handles_checked": len(handles), "aliases_checked": len(aliases),
                  "failures": dict(failures)})


def _query_words(text: str) -> list[str]:
    """Alphanumeric words of four or more characters, casefolded, first occurrence order."""
    seen: dict[str, None] = {}
    for word in text.split():
        if word.isalnum() and len(word) >= 4:
            seen.setdefault(word.casefold())
    return list(seen)


def _first_line_turn(first_text: str, rows: list[dict]) -> int | None:
    """Turn of the published row that carries the session's first line (whitespace-normalised)."""
    head = re.sub(r"\s+", " ", first_text).strip()[:60]
    for row in sorted(rows, key=lambda row: row["turn"]):
        if re.sub(r"\s+", " ", row.get("text") or "").strip().startswith(head):
            return row["turn"]
    return None


def check_search_first_lines(runner, sessions: list[dict], messages: list[dict], *,
                             sample: int = 12, seed: int = 5, words_per_query: int = 3) -> Check:
    """Every published first line is searchable: its rarest words, scoped to the session with
    --chat, return the row carrying that line. Rank across the corpus is deliberately not
    asserted; thousands of sessions share boilerplate openers, so top-k is a ranking property."""
    rng = random.Random(seed)
    frequency: Counter = Counter()
    for row in sessions:
        frequency.update(set(_query_words(row.get("first_text", ""))))
    candidates = [row for row in sessions
                  if len(row.get("first_text", "")) >= 12 and _query_words(row["first_text"])]
    if len(candidates) > sample:
        candidates = rng.sample(candidates, sample)
    by_session: dict[str, list[dict]] = defaultdict(list)
    for row in messages:
        by_session[row["session"]].append(row)
    outcomes: Counter = Counter()
    for row in candidates:
        words = _query_words(row["first_text"])
        rarest = sorted(words, key=lambda word: (frequency[word], words.index(word)))[:words_per_query]
        turn = _first_line_turn(row["first_text"], by_session.get(row["session"], []))
        if turn is None:
            outcomes["first_line_row_not_published"] += 1
            continue
        # Orchestrated subagents repeat their opening prompt in every turn, so a hit cap inside
        # one chat would measure rank again; the chat itself bounds the hits.
        found = runner.cli(["search", "--json", "--chat", row["session"], "-n", str(IN_CHAT_HITS),
                            " ".join(rarest)])
        if found.returncode not in (0, 1):
            outcomes[f"search_rc{found.returncode}"] += 1
            continue
        hits = [hit for hit in runner.json_rows(found) if hit.get("session") == row["session"]]
        if any(hit.get("turn") == turn for hit in hits):
            outcomes["found"] += 1
        elif hits:
            outcomes["other_turns_only"] += 1
        else:
            outcomes["no_hit_in_session"] += 1
    misses = sum(count for key, count in outcomes.items() if key != "found")
    return Check("search_first_lines", misses == 0,
                 {"queried": len(candidates), "misses": misses, "outcomes": dict(outcomes)})
