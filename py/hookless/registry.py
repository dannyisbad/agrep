"""Validated agent capabilities shared across the hookless boundary."""

from __future__ import annotations

import json
from pathlib import Path
import re
import unicodedata
from typing import Mapping, NamedTuple


MANIFEST_PATH = Path(__file__).with_name("agent_registry.json")
_NAME_RE = re.compile(r"[a-z][a-z0-9_-]*\Z")
_ENV_KEY_RE = re.compile(r"[A-Z][A-Z0-9_]*\Z")
_STATES = frozenset({"supported", "unsupported"})
_TEACH_KINDS = frozenset({"markdown", "skill"})
_TEACH_BASES = frozenset({"home", "opencode_data"})


class RegistryError(RuntimeError):
    """The checked-in capability manifest is malformed or inconsistent."""


class Capability(NamedTuple):
    state: str
    reason: str = ""

    @property
    def supported(self) -> bool:
        return self.state == "supported"


class AgentContextCapability(NamedTuple):
    state: str
    reason: str
    env_keys: tuple[str, ...]

    @property
    def supported(self) -> bool:
        return self.state == "supported"


class TeachCapability(NamedTuple):
    state: str
    reason: str
    target_ids: tuple[str, ...]

    @property
    def supported(self) -> bool:
        return self.state == "supported"


class TeachPath(NamedTuple):
    base: str
    parts: tuple[str, ...]


class TeachTarget(NamedTuple):
    target_id: str
    label: str
    kind: str
    proof: TeachPath
    target: TeachPath


class TeachClient(NamedTuple):
    name: str
    teach: TeachCapability


class Adapter(NamedTuple):
    name: str
    aliases: tuple[str, ...]
    live: Capability
    native_resume: Capability
    agent_context: AgentContextCapability
    teach: TeachCapability


class Registry(NamedTuple):
    version: int
    adapters: tuple[Adapter, ...]
    teach_clients: tuple[TeachClient, ...]
    teach_targets: tuple[TeachTarget, ...]

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(adapter.name for adapter in self.adapters)

    @property
    def input_names(self) -> tuple[str, ...]:
        return tuple(
            name
            for adapter in self.adapters
            for name in (adapter.name, *adapter.aliases)
        )

    def normalize(self, name: str) -> str:
        return next(
            (
                adapter.name
                for adapter in self.adapters
                if name == adapter.name or name in adapter.aliases
            ),
            name,
        )

    def supported(self, capability: str) -> tuple[str, ...]:
        return tuple(
            adapter.name for adapter in self.adapters
            if getattr(adapter, capability).supported
        )

    def unsupported(self, capability: str) -> dict[str, str]:
        return {
            adapter.name: getattr(adapter, capability).reason
            for adapter in self.adapters
            if not getattr(adapter, capability).supported
        }

    @property
    def active_teach_targets(self) -> tuple[TeachTarget, ...]:
        active = {
            target_id
            for owner in (*self.adapters, *self.teach_clients)
            if owner.teach.supported
            for target_id in owner.teach.target_ids
        }
        return tuple(
            target for target in self.teach_targets
            if target.target_id in active
        )


def _capability(raw: object, where: str) -> Capability:
    if not isinstance(raw, dict):
        raise RegistryError(f"{where} must be an object")
    unknown = set(raw) - {"state", "reason"}
    if unknown:
        raise RegistryError(f"{where} has unknown fields: {sorted(unknown)}")
    state = raw.get("state")
    reason = raw.get("reason", "")
    if state not in _STATES:
        raise RegistryError(f"{where}.state must be supported or unsupported")
    if not isinstance(reason, str):
        raise RegistryError(f"{where}.reason must be text")
    reason = reason.strip()
    if state == "unsupported" and not reason:
        raise RegistryError(f"{where} needs an unsupported reason")
    if state == "supported" and reason:
        raise RegistryError(f"{where} cannot explain a supported capability")
    return Capability(state, reason)


def _agent_context(raw: object, where: str) -> AgentContextCapability:
    if not isinstance(raw, dict):
        raise RegistryError(f"{where} must be an object")
    unknown = set(raw) - {"state", "reason", "env_keys"}
    if unknown:
        raise RegistryError(f"{where} has unknown fields: {sorted(unknown)}")
    state = raw.get("state")
    reason = raw.get("reason", "")
    keys = raw.get("env_keys", [])
    if state not in _STATES:
        raise RegistryError(f"{where}.state must be supported or unsupported")
    if not isinstance(reason, str):
        raise RegistryError(f"{where}.reason must be text")
    if not isinstance(keys, list) or any(not isinstance(key, str) for key in keys):
        raise RegistryError(f"{where}.env_keys must be a list of names")
    reason = reason.strip()
    env_keys = tuple(keys)
    if any(_ENV_KEY_RE.fullmatch(key) is None for key in env_keys):
        raise RegistryError(f"{where}.env_keys contains an invalid name")
    if len(set(env_keys)) != len(env_keys):
        raise RegistryError(f"{where}.env_keys contains duplicates")
    if state == "supported" and not env_keys:
        raise RegistryError(f"{where} needs at least one environment key")
    if state == "supported" and reason:
        raise RegistryError(f"{where} cannot explain a supported capability")
    if state == "unsupported" and not reason:
        raise RegistryError(f"{where} needs an unsupported reason")
    if state == "unsupported" and env_keys:
        raise RegistryError(f"{where} cannot give keys for an unsupported capability")
    return AgentContextCapability(state, reason, env_keys)


def _teach_path(raw: object, where: str) -> TeachPath:
    if not isinstance(raw, dict) or set(raw) != {"base", "parts"}:
        raise RegistryError(f"{where} must contain base and parts")
    base = raw.get("base")
    parts = raw.get("parts")
    if base not in _TEACH_BASES:
        raise RegistryError(f"{where}.base is unsupported")
    if not isinstance(parts, list) or any(not isinstance(part, str) for part in parts):
        raise RegistryError(f"{where}.parts must be a list of path components")
    if any(
            not part or part in {".", ".."} or "/" in part or "\\" in part
            for part in parts):
        raise RegistryError(f"{where}.parts contains an unsafe path component")
    return TeachPath(str(base), tuple(parts))


def _teach_target(raw: object, where: str) -> TeachTarget:
    if not isinstance(raw, dict) or set(raw) != {
            "id", "label", "kind", "proof", "target"}:
        raise RegistryError(
            f"{where} must contain id, label, kind, proof, and target")
    target_id = raw.get("id")
    label = raw.get("label")
    kind = raw.get("kind")
    if not isinstance(target_id, str) or _NAME_RE.fullmatch(target_id) is None:
        raise RegistryError(f"{where}.id is invalid")
    if (not isinstance(label, str) or not label
            or label != label.strip()
            or any(unicodedata.category(char) == "Cc" for char in label)):
        raise RegistryError(f"{where}.label is invalid")
    if kind not in _TEACH_KINDS:
        raise RegistryError(f"{where}.kind must be markdown or skill")
    return TeachTarget(
        target_id,
        label,
        str(kind),
        _teach_path(raw.get("proof"), f"{where}.proof"),
        _teach_path(raw.get("target"), f"{where}.target"),
    )


def _teach_capability(
        raw: object, where: str, target_ids: frozenset[str],
) -> TeachCapability:
    if not isinstance(raw, dict):
        raise RegistryError(f"{where} must be an object")
    unknown = set(raw) - {"state", "reason", "target_ids"}
    if unknown:
        raise RegistryError(f"{where} has unknown fields: {sorted(unknown)}")
    state = raw.get("state")
    reason = raw.get("reason", "")
    raw_ids = raw.get("target_ids", [])
    if state not in _STATES:
        raise RegistryError(f"{where}.state must be supported or unsupported")
    if not isinstance(reason, str):
        raise RegistryError(f"{where}.reason must be text")
    if not isinstance(raw_ids, list) or any(
            not isinstance(target_id, str) for target_id in raw_ids):
        raise RegistryError(f"{where}.target_ids must be a list of target ids")
    reason = reason.strip()
    ids = tuple(raw_ids)
    if len(set(ids)) != len(ids):
        raise RegistryError(f"{where}.target_ids contains duplicates")
    missing = sorted(set(ids) - target_ids)
    if missing:
        raise RegistryError(f"{where} references missing teach targets: {missing}")
    if state == "supported" and not ids:
        raise RegistryError(f"{where} needs at least one teach target")
    if state == "supported" and reason:
        raise RegistryError(f"{where} cannot explain a supported capability")
    if state == "unsupported" and not reason:
        raise RegistryError(f"{where} needs an unsupported reason")
    if state == "unsupported" and ids:
        raise RegistryError(f"{where} cannot give targets for an unsupported capability")
    return TeachCapability(state, reason, ids)


def registry_from_payload(payload: object) -> Registry:
    if not isinstance(payload, dict) or set(payload) != {
            "version", "adapters", "teach_clients", "teach_targets"}:
        raise RegistryError(
            "registry must contain only version, adapters, teach_clients, "
            "and teach_targets")
    version = payload.get("version")
    if type(version) is not int or version != 2:
        raise RegistryError("unsupported agent registry version")
    raw_adapters = payload.get("adapters")
    if not isinstance(raw_adapters, list) or not raw_adapters:
        raise RegistryError("registry adapters must be a nonempty list")
    raw_targets = payload.get("teach_targets")
    if not isinstance(raw_targets, list) or not raw_targets:
        raise RegistryError("registry teach_targets must be a nonempty list")
    raw_clients = payload.get("teach_clients")
    if not isinstance(raw_clients, list):
        raise RegistryError("registry teach_clients must be a list")
    teach_targets = tuple(
        _teach_target(raw, f"teach_targets[{index}]")
        for index, raw in enumerate(raw_targets)
    )
    target_ids = tuple(target.target_id for target in teach_targets)
    if len(set(target_ids)) != len(target_ids):
        raise RegistryError("duplicate teach target id")
    target_id_set = frozenset(target_ids)
    adapters = []
    seen = set()
    seen_input_names = set()
    seen_env_keys: set[str] = set()
    for index, raw in enumerate(raw_adapters):
        where = f"adapters[{index}]"
        required = {
            "name", "live", "native_resume", "agent_context", "teach"}
        if not isinstance(raw, dict):
            raise RegistryError(f"{where} must be an object")
        fields = set(raw)
        if fields not in (required, required | {"aliases"}):
            raise RegistryError(
                f"{where} must contain name, live, native_resume, "
                "agent_context, and teach; aliases is optional")
        name = raw.get("name")
        if not isinstance(name, str) or _NAME_RE.fullmatch(name) is None:
            raise RegistryError(f"{where}.name is invalid")
        if name in seen:
            raise RegistryError(f"duplicate adapter {name!r}")
        raw_aliases = raw.get("aliases", [])
        if not isinstance(raw_aliases, list) or any(
                not isinstance(alias, str) or _NAME_RE.fullmatch(alias) is None
                for alias in raw_aliases):
            raise RegistryError(f"{where}.aliases must be valid agent names")
        aliases = tuple(raw_aliases)
        input_names = (name, *aliases)
        repeated_names = sorted(set(input_names) & seen_input_names)
        if len(set(input_names)) != len(input_names) or repeated_names:
            raise RegistryError(f"{where}.aliases collide with an agent name")
        seen.add(name)
        seen_input_names.update(input_names)
        agent_context = _agent_context(
            raw.get("agent_context"), f"{where}.agent_context")
        repeated = sorted(seen_env_keys & set(agent_context.env_keys))
        if repeated:
            raise RegistryError(
                f"{where}.agent_context repeats environment keys: {repeated}")
        seen_env_keys.update(agent_context.env_keys)
        adapters.append(Adapter(
            name,
            aliases,
            _capability(raw.get("live"), f"{where}.live"),
            _capability(raw.get("native_resume"), f"{where}.native_resume"),
            agent_context,
            _teach_capability(raw.get("teach"), f"{where}.teach", target_id_set),
        ))
    teach_clients = []
    for index, raw in enumerate(raw_clients):
        where = f"teach_clients[{index}]"
        if not isinstance(raw, dict) or set(raw) != {"name", "teach"}:
            raise RegistryError(f"{where} must contain name and teach")
        name = raw.get("name")
        if not isinstance(name, str) or _NAME_RE.fullmatch(name) is None:
            raise RegistryError(f"{where}.name is invalid")
        if name in seen_input_names:
            raise RegistryError(f"duplicate teach client {name!r}")
        seen_input_names.add(name)
        teach_clients.append(TeachClient(
            name,
            _teach_capability(
                raw.get("teach"), f"{where}.teach", target_id_set),
        ))
    owners = (*adapters, *teach_clients)
    referenced = {
        target_id
        for owner in owners
        for target_id in owner.teach.target_ids
    }
    unreferenced = sorted(target_id_set - referenced)
    if unreferenced:
        raise RegistryError(f"unreferenced teach targets: {unreferenced}")
    return Registry(2, tuple(adapters), tuple(teach_clients), teach_targets)


def load_registry(path: Path = MANIFEST_PATH) -> Registry:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RegistryError(f"cannot read agent registry: {exc}") from exc
    return registry_from_payload(payload)


def require_exact(label: str, implementation: Mapping[str, object],
                  expected: tuple[str, ...]) -> None:
    actual = tuple(implementation)
    if actual != expected:
        raise RegistryError(
            f"{label} implementation {actual!r} != registry {expected!r}")


REGISTRY = load_registry()
ADAPTER_NAMES = REGISTRY.names
ADAPTER_INPUT_NAMES = REGISTRY.input_names
LIVE_AGENTS = REGISTRY.supported("live")
NATIVE_RESUME_AGENTS = REGISTRY.supported("native_resume")
AGENT_CONTEXT_AGENTS = REGISTRY.supported("agent_context")
TEACH_AGENTS = REGISTRY.supported("teach")
LIVE_UNSUPPORTED = REGISTRY.unsupported("live")
NATIVE_RESUME_UNSUPPORTED = REGISTRY.unsupported("native_resume")
AGENT_CONTEXT_UNSUPPORTED = REGISTRY.unsupported("agent_context")
TEACH_UNSUPPORTED = REGISTRY.unsupported("teach")
AGENT_CONTEXT_ENV_KEYS = tuple(
    key
    for adapter in REGISTRY.adapters
    for key in adapter.agent_context.env_keys
)
TEACH_CLIENTS = REGISTRY.teach_clients
TEACH_TARGETS = REGISTRY.active_teach_targets


def normalize_agent_name(name: str) -> str:
    return REGISTRY.normalize(name)


def capability_error(capability: str, name: str) -> str:
    """Explain an unknown or explicitly unsupported adapter capability."""
    labels = {
        "live": "live observation",
        "native_resume": "native resume",
        "agent_context": "agent-context detection",
        "teach": "agent teaching",
    }
    if capability not in labels:
        raise ValueError(f"unknown capability {capability!r}")
    name = normalize_agent_name(name)
    adapter = next(
        (item for item in REGISTRY.adapters if item.name == name), None)
    supported = REGISTRY.supported(capability)
    if adapter is None:
        return (f"unknown agent {name!r}; supported for {labels[capability]}: "
                f"{', '.join(supported)}")
    contract = getattr(adapter, capability)
    if contract.supported:
        return ""
    return f"{name} has no {labels[capability]} support: {contract.reason}"
