from pathlib import Path
from types import SimpleNamespace

from app import capper_control


def _core():
    storage = {}

    def _load(path):
        return storage.get(str(path), {})

    def _save(path, value):
        storage[str(path)] = value

    return SimpleNamespace(
        DATA_DIR=Path("/tmp/capper-control-test"),
        _load=_load,
        _save=_save,
        storage=storage,
    )


def test_capper_defaults_online_and_persists_independent_state():
    core = _core()

    assert capper_control.is_enabled(core, "Slam - NFL") is True
    assert capper_control.is_enabled(core, "Syndicate - NFL") is True

    assert capper_control.set_enabled(core, "Slam - NFL", False) is False
    assert capper_control.is_enabled(core, "Slam - NFL") is False
    assert capper_control.is_enabled(core, "Syndicate - NFL") is True

    assert capper_control.set_enabled(core, "Slam - NFL", True) is True
    assert capper_control.is_enabled(core, "Slam - NFL") is True


def test_legacy_string_and_object_values_are_read_safely():
    core = _core()
    state_path = str(core.DATA_DIR / capper_control.STATE_FILE_NAME)
    core.storage[state_path] = {
        "Slam - CFB": "offline",
        "Syndicate - CFB": {"enabled": True},
    }

    assert capper_control.is_enabled(core, "Slam - CFB") is False
    assert capper_control.is_enabled(core, "Syndicate - CFB") is True
