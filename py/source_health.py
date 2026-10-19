"""The last index pass's source health record and the remedy wording for its issues, one owner
for doctor, status, audit and the CLI."""

from __future__ import annotations

import json
import os
import sys

import common
import console
import derived_anchor
import dist
import ownerfile
import surface_policy as surface


def source_health_issues() -> list[dict]:
    path = common.DATA_DIR / ".source-health.json"

    def unavailable(reason: str) -> list[dict]:
        return [{
            "agent": "all",
            "path": str(path),
            "kind": "source-health-unavailable",
            "reason": reason,
        }]

    try:
        raw = ownerfile.snapshot(path, max_bytes=1024 * 1024).raw
        record = json.loads(raw.decode("utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, RecursionError, UnicodeError, ValueError) as exc:
        return unavailable(f"source health record is unreadable ({exc})")
    if not isinstance(record, dict) or record.get("code") != "source-unreadable":
        return unavailable("source health record is malformed")
    issues = record.get("issues")
    if not isinstance(issues, list) or not all(isinstance(issue, dict) for issue in issues):
        return unavailable("source health record is malformed")
    return issues


def cli_command(*args: object) -> str:
    """The agrep command line `args` is run as here, quoted for a shell."""
    return console.shell_command(*dist.cli_invocation(*args), fallback="<agrep command>")


def command_remedy(name: str, *args: object, **values: object) -> str:
    return surface.render_remedy(name, command=cli_command(*args), **values)


def store_sidecars_blocked() -> list[dict]:
    """The SQLite sidecars of corpus.db and the event store this user cannot read and write."""
    import index_lock
    return [
        *index_lock.inaccessible_sidecars(common.DATA_DIR / "corpus.db", "search index"),
        *index_lock.inaccessible_sidecars(
            common.DATA_DIR / "events" / common.EVENT_STORE_NAME, "event store"),
    ]


def inaccessible_sidecar_fix(sidecar: dict, command: str | None = None) -> str:
    """The commands that give this user back a blocked sidecar, then the index pass."""
    path = str(sidecar["path"])
    access = sidecar.get("access") or ("owner" if sidecar.get("owner") else "mode")
    quoted = console.shell_command(path, fallback="") if os.name == "posix" else ""
    if not quoted:
        fix = "fix its owner or permissions"
    elif access == "owner":
        fix = f"`sudo chown $USER {quoted}`"
    elif access == "flags":
        fix = (f"`chflags nouchg {quoted} && chmod -N {quoted}`" if sys.platform == "darwin"
               else f"`sudo chattr -i {quoted} && setfacl -b {quoted}`")
    else:
        fix = f"`chmod u+rw {quoted}`"
    if derived_anchor.newer_anchor() is not None:
        # this build serves a newer agrep's data dir read-only: its `index` would decline
        return f"{fix}, then {surface.render_remedy('newer-owner-index')}"
    return f"{fix}, then `{command or cli_command('index')}`"


def inaccessible_sidecar_line(sidecar: dict, command: str | None = None) -> str:
    """Why a store cannot be updated, and the two commands that end it."""
    path = str(sidecar["path"])
    access = sidecar.get("access") or ("owner" if sidecar.get("owner") else "mode")
    whose = {
        "owner": f"is owned by {console.terminal_safe(sidecar.get('owner') or 'another user')}",
        "flags": "is closed to you by a file flag or an access-control list",
    }.get(access, "is not readable and writable by you")
    return (f"the {sidecar['store']} cannot be updated - its {sidecar['kind']} at "
            f"{console.terminal_safe(path)} {whose}, so no index pass can use it: "
            f"{inaccessible_sidecar_fix(sidecar, command)}")


def sidecar_block_remedy(command: str | None = None) -> str | None:
    """While a store sidecar blocks every pass, the remedy a surface that would promise an index
    pass gives instead (the file's own row says why); None when nothing is blocked."""
    blocked = store_sidecars_blocked()
    if not blocked:
        return None
    return (f"index updates wait on {console.terminal_safe(blocked[0]['path'])}: "
            f"{inaccessible_sidecar_fix(blocked[0], command)}")


def rebuild_cost() -> str:
    """The consent rebuild's cost clause: `reindex --full` redoes meaning search only where
    embeddings are on and their runtime is installed, and says nothing more otherwise."""
    if common.setting("embeddings") == "off" or not dist.semantic_runtime_installed():
        return ""
    return "; " + surface.REBUILD_COST


def _hold_names(held: list[dict]) -> dict[str, str]:
    """The agents of `held` records, each once in order, and the pronouns for how many there
    are: the names the ingest's own refusal gives them."""
    agents = dict.fromkeys(console.terminal_safe(issue.get("agent") or "its agent")
                           for issue in held)
    one = len(held) == 1
    return {"agents": ", ".join(agents), "them": "it" if one else "them",
            "they": "it is" if one else "they are"}


def _hold_remedy(held: list[dict], *, newer_owner: bool, cost: bool = True) -> str:
    """One remedy naming every record of `held`, all of one kind (see `_hold_kind`); `cost`
    says what the rebuild costs, which a line beside another that says it leaves out."""
    names = _hold_names(held)
    fields = {"agent": names["agents"], "them": names["them"]}
    kind = _hold_kind(held[0], newer_owner=newer_owner)
    if kind != "unreadable-holds-publication":
        return surface.render_remedy(kind, **fields)
    return command_remedy(kind, "reindex", "--full", they=names["they"],
                          cost=rebuild_cost() if cost else "", **fields)


def _hold_kind(issue: dict, *, newer_owner: bool) -> str:
    # All that was published is unreadable: a rebuild would leave nothing, so only the agent's
    # own fix helps; a newer agrep's data dir is that agrep's to rebuild.
    if issue.get("rebuild") is False:
        return "unreadable-holds-everything"
    return "unreadable-holds-newer-owner" if newer_owner else "unreadable-holds-publication"


def source_issue_remedy(issue: dict, *, newer_owner: bool = False) -> str:
    """`newer_owner`: a newer agrep owns the data dir, so this build can neither index it nor
    run the rebuild; what it would have run is that agrep's."""
    # The last pass refused to publish over this record alone: retrying cannot help, while
    # fixing the source or an explicit rebuild without it does.
    if issue.get("blocks_publication") is True:
        return _hold_remedy([issue], newer_owner=newer_owner)
    kind = str(issue.get("kind") or "")
    if kind == "permission-denied":
        return surface.render_remedy("store-unreadable")
    if kind == "not-found":
        return surface.render_remedy("source-not-found")
    if newer_owner:
        return surface.render_remedy("newer-owner-index")
    if kind == surface.ATTRIBUTION_ISSUE_KIND:
        return command_remedy("attribution-provisional", "index")
    if kind == "source-read-incomplete":
        return command_remedy("source-read-incomplete", "index")
    if kind == "source-health-unavailable":
        return command_remedy("source-health-unavailable", "index")
    return command_remedy("source-inspect", "index")


def _held_records() -> list[dict]:
    return [issue for issue in source_health_issues() if issue.get("blocks_publication") is True]


def _held_remedies(held: list[dict], *, newer_owner: bool, cost: bool = True) -> list[str]:
    groups: dict[str, list[dict]] = {}
    for issue in held:
        groups.setdefault(_hold_kind(issue, newer_owner=newer_owner), []).append(issue)
    return [_hold_remedy(group, newer_owner=newer_owner, cost=cost) for group in groups.values()]


def held_publication_remedy(*, newer_owner: bool = False, cost: bool = True) -> str | None:
    """The remedy of the records holding publication back, naming each agent once as the
    ingest's refusal does, or None. While one holds, no plain `agrep index` publishes, so a
    surface that would promise one gives this remedy instead."""
    remedies = _held_remedies(_held_records(), newer_owner=newer_owner, cost=cost)
    return "; ".join(remedies) if remedies else None


def issue_remedies(issues: list[dict], *, newer_owner: bool = False) -> str:
    """The remedies of `issues` as one row gives them: the held ones as one clause naming each
    agent once, where the first of them stands, and each other remedy once."""
    held = [issue for issue in issues if issue.get("blocks_publication") is True]
    remedies: list[str] = []
    for issue in issues:
        if issue.get("blocks_publication") is not True:
            remedies.append(source_issue_remedy(issue, newer_owner=newer_owner))
        elif issue is held[0]:
            remedies.extend(_held_remedies(held, newer_owner=newer_owner))
    return "; ".join(dict.fromkeys(remedies))


def rollback_remedy(command: str | None = None, *, newer_owner: bool = False) -> str:
    """What rolls corpus.db's interrupted journal back: any `agrep index` a store sidecar does
    not block, even one a hold then refuses, so the hold is no remedy for it; in a newer
    agrep's data dir, that agrep's index."""
    blocked = sidecar_block_remedy(command)
    if blocked:
        return blocked
    if newer_owner:
        return surface.render_remedy("newer-owner-index")
    if _held_records():
        return command_remedy("index-rollback", "index")
    return command_remedy("index-publish", "index")


def publication_remedy(command: str | None = None, *, newer_owner: bool = False) -> str:
    """What the next publication needs: a blocked store sidecar's fix then a hold's remedy
    (see `blocked_publication_remedy`), else `agrep index` runs one, which in a data dir a
    newer agrep owns is that agrep's to run."""
    blocked = blocked_publication_remedy(command, newer_owner=newer_owner)
    if blocked:
        return blocked
    if newer_owner:
        return surface.render_remedy("newer-owner-index")
    return command_remedy("index-publish", "index")


def hold_fields() -> dict[str, str]:
    """The names and rebuild cost the status advice for a hold gives (see
    `surface.indexing_advice_line`)."""
    held = _held_records()
    return {**(_hold_names(held) if held else {}), "cost": rebuild_cost()}


def blocked_publication_remedy(command: str | None = None, *,
                               newer_owner: bool = False) -> str | None:
    """Everything standing between this box and its next publication, in the order it must be
    taken: a store sidecar no pass can use, then the records holding publication back."""
    parts = [sidecar_block_remedy(command), held_publication_remedy(newer_owner=newer_owner)]
    return "; ".join(part for part in parts if part) or None
