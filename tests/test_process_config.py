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
from process_monitor import ProcessMonitorUI, main


def test_default_config_path_uses_absolute_xdg_config_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert default_config_path() == tmp_path / "xdg" / "kaiamon" / "config.json"


def test_default_config_path_falls_back_for_relative_xdg_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative/config")
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
        ('{"refresh_interval": NaN}', "finite"),
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


def test_cli_overrides_saved_values_and_persists_runtime_preferences(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "config.json"
    save_config(
        path,
        MonitorConfig(
            refresh_interval=1.0,
            columns=("PID", "USER"),
            sort_field="CPU",
            descending=True,
            tree_mode=False,
        ),
    )
    observed: dict[str, object] = {}

    def fake_run(ui: ProcessMonitorUI) -> None:
        observed.update({
            "refresh_interval": ui.refresh_interval,
            "columns": ui.columns,
            "sort_field": ui.sort_field,
            "descending": ui.descending,
            "tree_mode": ui.tree_mode,
        })
        # Simulate preferences changed interactively during the session.
        ui.sort_field = "TIME"
        ui.descending = True
        ui.tree_mode = False

    monkeypatch.setattr(ProcessMonitorUI, "run", fake_run)

    main([
        "--config", str(path),
        "--refresh", "0.25",
        "--columns", "PID,COMMAND",
        "--sort", "MEM",
        "--sort-order", "asc",
        "--tree",
    ])

    assert observed == {
        "refresh_interval": 0.25,
        "columns": ("PID", "COMMAND"),
        "sort_field": "MEM",
        "descending": False,
        "tree_mode": True,
    }
    assert load_config(path) == MonitorConfig(
        refresh_interval=0.25,
        columns=("PID", "COMMAND"),
        sort_field="TIME",
        descending=True,
        tree_mode=False,
    )


def test_no_save_config_preserves_existing_preferences(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "config.json"
    expected = MonitorConfig(
        refresh_interval=0.5,
        columns=("PID", "COMMAND"),
        sort_field="USER",
        descending=False,
        tree_mode=True,
    )
    save_config(path, expected)
    monkeypatch.setattr(ProcessMonitorUI, "run", lambda _ui: None)

    main(["--config", str(path), "--no-save-config", "--flat"])

    assert load_config(path) == expected


def test_cli_uses_default_config_path_and_saves_after_clean_exit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    config_home = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    captured: list[ProcessMonitorUI] = []
    monkeypatch.setattr(ProcessMonitorUI, "run", lambda ui: captured.append(ui))

    main(["--sort", "PID", "--sort-order", "asc"])

    assert len(captured) == 1
    saved = load_config(config_home / "kaiamon" / "config.json")
    assert saved.sort_field == "PID"
    assert saved.descending is False


def test_cli_loads_saved_preferences_without_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "saved.json"
    expected = MonitorConfig(
        refresh_interval=0.4,
        columns=("PID", "COMMAND", "CPU"),
        sort_field="COMMAND",
        descending=False,
        tree_mode=True,
    )
    save_config(path, expected)
    observed: list[ProcessMonitorUI] = []
    monkeypatch.setattr(ProcessMonitorUI, "run", lambda ui: observed.append(ui))

    main(["--config", str(path), "--no-save-config"])

    assert len(observed) == 1
    ui = observed[0]
    assert ui.refresh_interval == expected.refresh_interval
    assert ui.columns == expected.columns
    assert ui.sort_field == expected.sort_field
    assert ui.descending is expected.descending
    assert ui.tree_mode is expected.tree_mode


def test_cli_reports_invalid_config_before_starting_ui(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "bad.json"
    path.write_text('{"columns": ["PID", "UNKNOWN"]}', encoding="utf-8")
    monkeypatch.setattr(
        ProcessMonitorUI, "run",
        lambda _ui: pytest.fail("UI must not start with invalid config"),
    )

    with pytest.raises(SystemExit) as error:
        main(["--config", str(path)])

    assert error.value.code == 2


def test_failed_atomic_replace_keeps_existing_config_and_cleans_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "config.json"
    original = MonitorConfig(refresh_interval=0.5, sort_field="PID")
    save_config(path, original)

    def fail_replace(_source: object, _destination: object) -> None:
        raise OSError("simulated filesystem error")

    monkeypatch.setattr("process_config.os.replace", fail_replace)

    with pytest.raises(OSError, match="simulated filesystem error"):
        save_config(path, MonitorConfig(refresh_interval=0.25, sort_field="MEM"))

    assert load_config(path) == original
    assert list(tmp_path.glob(".config.json.*.tmp")) == []
