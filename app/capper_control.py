from __future__ import annotations

from typing import Any


STATE_FILE_NAME = "capper_enabled_state.json"


def _state_path(core: Any):
    return core.DATA_DIR / STATE_FILE_NAME


def _load_state(core: Any) -> dict[str, Any]:
    data = core._load(_state_path(core))
    return data if isinstance(data, dict) else {}


def is_enabled(core: Any, label: str, default: bool = True) -> bool:
    """Return whether one capper/sport is allowed to create new automatic trades."""
    raw = _load_state(core).get(str(label))
    if raw is None:
        return bool(default)
    if isinstance(raw, dict):
        raw = raw.get("enabled", default)
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "yes", "on", "online", "enabled"}
    return bool(raw)


def set_enabled(core: Any, label: str, enabled: bool) -> bool:
    state = _load_state(core)
    value = bool(enabled)
    state[str(label)] = value
    core._save(_state_path(core), state)
    return value
