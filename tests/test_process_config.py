from __future__ import annotations

import json
from pathlib import Path

import pytest

from process_config import (
    DEFAULT_COLUMNS,
    ConfigError,
    MonitorConfig,
    default_config_path,
    load_config,
    normalize_columns,
    save_config,
)


def test_default_config_path_uses_absolute_xdg_config_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert default_config_path() == tmp_path / "xdg" / "kaiamon" / "config.json"


def test_default_config_path_falls_back_for_relative_xdg_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert default_config_path() == tmp_path / ".config" / "kaiamon" / "config.json"


def test_missing_config_returns_documented_defaults(tmp_path: Path) -> None:
    config = load_config(tmp_path / "missing.json")

    assert config == MonitorConfig()
    assert config.columns == DEFAULT_COLUMNS


def test_config_round_trips_and_creates_parent_directories(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "kaiamon" / "config.json"
    expected = MonitorConfig(
        refresh_interval=0.25,
        columns=("PID", "COMMAND", "CPU"),
        sort_field="MEM",
        descending=False,
        tree_mode=True,
    )

    save_config(path, expected)

    assert load_config(path) == expected
    assert json.loads(path.read_text(encoding="utf-8")) == expected.to_dict()


def test_config_file_is_created_with_private_permissions(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    save_config(path, MonitorConfig())

    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "payload, message",
    [
        ("not json", "invalid JSON"),
        ("[]", "top-level JSON value must be an object"),
        ('{"version": 2}', "unsupported configuration version"),
        ('{"refresh_interval": 0.01}', "at least 0.05"),
        ('{"refresh_interval": NaN}', "invalid JSON"),
        ('{"columns": ["PID", "pid"]}', "duplicates"),
        ('{"columns": ["PID", "UNKNOWN"]}', "unsupported column"),
        ('{"sort_field": "NOPE"}', "sort_field"),
        ('{"descending": "yes"}', "descending"),
        ('{"tree_mode": 1}', "tree_mode"),
    ],
)
def test_invalid_config_is_rejected(
    tmp_path: Path, payload: str, message: str,
) -> None:
    path = tmp_path / "config.json"
    path.write_text(payload, encoding="utf-8")

    with pytest.raises(ConfigError, match=message):
        load_config(path)


def test_column_normalization_supports_aliases_and_rejects_bad_shapes() -> None:
    assert normalize_columns([" command ", "pid", "CPU%"]) == (
        "COMMAND", "PID", "CPU",
    )
    with pytest.raises(ValueError, match="non-string"):
        normalize_columns(["PID", 3])
