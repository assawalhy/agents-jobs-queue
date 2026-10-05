"""User configuration: `~/.config/ajq/config.json`, `AJQ_*` env, flag overrides.

Precedence is fixed: **flags > env > user file > DEFAULTS**, deep-merged per key
path so a file that sets a single pool cap keeps every other default. A malformed
or unreadable file warns on stderr once and falls back to DEFAULTS — it never
raises, because a broken config must not stop the daemon from starting.
"""

from __future__ import annotations

import copy
import json
import os
import sys
from typing import Any, Mapping, Optional

from ajq import paths

DEFAULTS: dict = {
    "limits": {
        "max_concurrent": 8,
        "pools": {"heavy": 2, "normal": 4, "service": 3, "light": 6},
    },
    "defaults": {
        "timeout_s": 1800,
        "max_output_bytes": 8388608,
        "pool": "auto",
        "kind": "auto",
        "kill_grace_s": 10,
        "priority": 0,
        "serial_key": "auto",
        "shell": False,
    },
    "resources": {
        "memory_mb": 2048,
        "cpu_percent": 200,
        "nice": 10,
        "backend": "auto",
        "memory_headroom": 1.2,
        "extra_args": [],
    },
    "estimates": {
        "enabled": True,
        "light_threshold_s": 30,
        "sigma_weight": 0.5,
        "cold_defaults_s": {
            "build": 300,
            "test": 120,
            "typecheck": 90,
            "lint": 20,
            "format": 10,
            "install": 240,
            "docker": 600,
            "check": 30,
            "unknown": 60,
        },
    },
    "hooks": {"guard_mode": "warn"},
    "daemon": {"unit": "ajqd.service", "tick_s": 0.5, "socket": None},
}

# (env var, dotted key path, value kind)
ENV_KEYS: tuple[tuple[str, str, str], ...] = (
    ("AJQ_MAX_CONCURRENT", "limits.max_concurrent", "int"),
    ("AJQ_POOL", "defaults.pool", "str"),
    ("AJQ_TIMEOUT_S", "defaults.timeout_s", "int"),
    ("AJQ_MAX_OUTPUT_BYTES", "defaults.max_output_bytes", "int"),
    ("AJQ_GUARD_MODE", "hooks.guard_mode", "str"),
    ("AJQ_MEMORY_MB", "resources.memory_mb", "int"),
    ("AJQ_CPU_PERCENT", "resources.cpu_percent", "int"),
    ("AJQ_NICE", "resources.nice", "int"),
    ("AJQ_BACKEND", "resources.backend", "str"),
    ("AJQ_LIGHT_THRESHOLD_S", "estimates.light_threshold_s", "int"),
    ("AJQ_KILL_GRACE_S", "defaults.kill_grace_s", "int"),
)

_DOC: dict[str, str] = {
    "limits": "max_concurrent caps every pool at once; pools.* caps one tier each.",
    "defaults": "Per-job fallbacks used when the matching submit flag is absent.",
    "resources": "Enforcement handed to the exec backend; memory_headroom gates admission.",
    "estimates": "Welford duration cache; light_threshold_s promotes cheap jobs to the light pool.",
    "hooks": "guard_mode for PreToolUse guards: warn | block | off.",
    "daemon": "Unit name, serve tick interval, socket override (null = default path).",
}

_warned: set[str] = set()


def _warn(message: str) -> None:
    sys.stderr.write(f"ajq: {message}\n")


def _warn_once(key: str, message: str) -> None:
    if key in _warned:
        return
    _warned.add(key)
    _warn(message)


def _assign(node: dict, parts: list[str], value: Any) -> None:
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = value


def _merge(base: dict, patch: Mapping[str, Any]) -> dict:
    """Deep-merge `patch` into `base` in place, one key path at a time."""
    for key, value in patch.items():
        current = base.get(key)
        if isinstance(value, Mapping) and isinstance(current, dict):
            _merge(current, value)
        elif isinstance(value, Mapping):
            base[key] = _merge({}, value)
        else:
            base[key] = copy.deepcopy(value)
    return base


def _normalize(source: Mapping[str, Any] | None) -> dict:
    """Accept nested dicts, dotted keys, or a mix; return a nested dict."""
    out: dict = {}
    if not source:
        return out
    for key, value in source.items():
        parts = str(key).split(".") if isinstance(key, str) else [str(key)]
        if len(parts) > 1:
            _assign(out, parts, value)
        elif isinstance(value, Mapping):
            _assign(out, parts, _normalize(value))
        else:
            out[key] = value
    return out


class Config:
    """Read-only view over the merged config tree with dotted-key access."""

    __slots__ = ("_data", "path")

    def __init__(self, data: Optional[dict] = None, path: Optional[str] = None) -> None:
        self._data = _normalize(data) if data is not None else copy.deepcopy(DEFAULTS)
        self.path = path

    def get(self, dotted_key: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in str(dotted_key).split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return default
        return node

    def __getitem__(self, dotted_key: str) -> Any:
        marker = object()
        value = self.get(dotted_key, marker)
        if value is marker:
            raise KeyError(dotted_key)
        return value

    def __contains__(self, dotted_key: str) -> bool:
        marker = object()
        return self.get(dotted_key, marker) is not marker

    def set(self, dotted_key: str, value: Any) -> "Config":
        """Set one key path (used to apply submit flags)."""
        _assign(self._data, str(dotted_key).split("."), value)
        return self

    def as_dict(self) -> dict:
        return copy.deepcopy(self._data)

    def __repr__(self) -> str:
        return f"Config(path={self.path!r}, keys={sorted(self._data)!r})"


def env_overrides() -> dict:
    """Nested partial config built from `AJQ_*`; empty when none are set."""
    out: dict = {}
    for var, dotted, kind in ENV_KEYS:
        raw = os.environ.get(var)
        if raw is None:
            continue
        text = raw.strip()
        if not text:
            continue
        if kind == "int":
            try:
                value: Any = int(text)
            except ValueError:
                _warn_once(var, f"ignoring {var}={raw!r}: not an integer")
                continue
        elif kind == "float":
            try:
                value = float(text)
            except ValueError:
                _warn_once(var, f"ignoring {var}={raw!r}: not a number")
                continue
        else:
            value = text
        _assign(out, dotted.split("."), value)
    return out


def _read_file(path: str) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except FileNotFoundError:
        return None
    except OSError as exc:
        _warn_once(path, f"cannot read {path}: {exc.strerror or exc}; using defaults")
        return None
    try:
        data = json.loads(text.strip() or "{}")
    except json.JSONDecodeError as exc:
        _warn_once(path, f"{path} is not valid JSON ({exc}); using defaults")
        return None
    if not isinstance(data, dict):
        _warn_once(path, f"{path} must hold a JSON object; using defaults")
        return None
    return data


def load_config(path: Optional[str] = None, overrides: Optional[dict] = None) -> Config:
    """Merge DEFAULTS < user file < env < overrides into a `Config`.

    A missing file is normal (first run). A malformed one warns and falls back to
    DEFAULTS instead of raising.
    """
    target = path if path else paths.CONFIG_PATH
    data = copy.deepcopy(DEFAULTS)
    from_file = _read_file(target)
    if from_file:
        _merge(data, _normalize(from_file))
    from_env = env_overrides()
    if from_env:
        _merge(data, from_env)
    explicit = _normalize(overrides)
    if explicit:
        _merge(data, explicit)
    return Config(data, path=target)


def default_config_json() -> str:
    """Annotated template: DEFAULTS plus a `_doc` section, valid JSON."""
    payload: dict = {"_doc": copy.deepcopy(_DOC)}
    payload.update(copy.deepcopy(DEFAULTS))
    return json.dumps(payload, indent=2) + "\n"


def seed_config(path: str, force: bool = False) -> bool:
    """Write the pretty-printed DEFAULTS when `path` is absent; True when written."""
    if os.path.exists(path) and not force:
        return False
    try:
        directory = os.path.dirname(os.path.abspath(path))
        paths.ensure_dir(directory)
        body = json.dumps(DEFAULTS, indent=2, sort_keys=True) + "\n"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(body)
    except OSError as exc:
        _warn_once(path, f"cannot seed {path}: {exc.strerror or exc}")
        return False
    return True