"""Validated, atomic JSON configuration for Kaiamon's process monitor."""
from __future__ import annotations

import json
import math
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from process_core import POLL_INTERVAL

DEFAULT_REFRESH_INTERVAL = POLL_INTERVAL
DEFAULT_COLUMNS = (
    "PID", "USER", "PRI", "NI", "VIRT", "RES", "SHR", "STATE",
    "CPU", "MEM", "TIME", "COMMAND",
)
SORT_FIELDS = ("CPU", "MEM", "PID", "USER", "TIME", "COMMAND")
COLUMN_ALIASES = {
    "CPU%": "CPU", "MEM%": "MEM", "TIME+": "TIME", "S": "STATE",
}
SUPPORTED_COLUMNS = frozenset({
    "PID", "USER", "PRI", "NI", "VIRT", "RES", "SHR",
    "STATE", "CPU", "MEM", "TIME", "COMMAND",
})
CONFIG_VERSION = 1


class ConfigError(ValueError):
    """Raised when a configuration file cannot be read or validated."""


@dataclass(frozen=True)
class MonitorConfig:
    """Persisted user preferences; transient selection/filter state is excluded."""

    refresh_interval: float = DEFAULT_REFRESH_INTERVAL
    columns: tuple[str, ...] = DEFAULT_COLUMNS
    sort_field: str = "CPU"
    descending: bool = True
    tree_mode: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": CONFIG_VERSION,
            "refresh_interval": self.refresh_interval,
            "columns": list(self.columns),
            "sort_field": self.sort_field,
            "descending": self.descending,
            "tree_mode": self.tree_mode,
        }


def normalize_columns(value: object) -> tuple[str, ...]:
    """Validate and normalize a sequence or comma-separated list of columns."""
    if isinstance(value, str):
        requested: list[object] = value.split(",")
    elif isinstance(value, (list, tuple)):
        requested = list(value)
    else:
        raise ValueError("columns must be a list or comma-separated string")
    if not requested or any(not isinstance(item, str) or not item.strip() for item in requested):
        raise ValueError("columns cannot contain empty or non-string entries")
    columns = tuple(
        COLUMN_ALIASES.get(item.strip().upper(), item.strip().upper())
        for item in requested
    )
    unsupported = [column for column in columns if column not in SUPPORTED_COLUMNS]
    if unsupported:
        raise ValueError("unsupported column(s): " + ", ".join(unsupported))
    if len(set(columns)) != len(columns):
        raise ValueError("columns cannot contain duplicates")
    return columns


def default_config_path() -> Path:
    """Return XDG_CONFIG_HOME/kaiamon/config.json or ~/.config/kaiamon/config.json."""
    xdg_home = os.environ.get("XDG_CONFIG_HOME")
    if xdg_home:
        candidate = Path(xdg_home).expanduser()
        if candidate.is_absolute():
            return candidate / "kaiamon" / "config.json"
    return Path.home() / ".config" / "kaiamon" / "config.json"


def _validated_config(data: object) -> MonitorConfig:
    if not isinstance(data, dict):
        raise ConfigError("the top-level JSON value must be an object")
    version = data.get("version", CONFIG_VERSION)
    if isinstance(version, bool) or not isinstance(version, int):
        raise ConfigError("'version' must be an integer")
    if version != CONFIG_VERSION:
        raise ConfigError(f"unsupported configuration version: {version}")

    refresh = data.get("refresh_interval", DEFAULT_REFRESH_INTERVAL)
    if isinstance(refresh, bool) or not isinstance(refresh, (int, float)):
        raise ConfigError("'refresh_interval' must be a number")
    refresh = float(refresh)
    if not math.isfinite(refresh) or refresh < 0.05:
        raise ConfigError("'refresh_interval' must be finite and at least 0.05 seconds")

    try:
        columns = normalize_columns(data.get("columns", list(DEFAULT_COLUMNS)))
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc

    sort_field = data.get("sort_field", "CPU")
    if not isinstance(sort_field, str) or sort_field.upper() not in SORT_FIELDS:
        raise ConfigError("'sort_field' must be one of: " + ", ".join(SORT_FIELDS))
    sort_field = sort_field.upper()

    descending = data.get("descending", True)
    tree_mode = data.get("tree_mode", False)
    if not isinstance(descending, bool):
        raise ConfigError("'descending' must be true or false")
    if not isinstance(tree_mode, bool):
        raise ConfigError("'tree_mode' must be true or false")

    return MonitorConfig(
        refresh_interval=refresh,
        columns=columns,
        sort_field=sort_field,
        descending=descending,
        tree_mode=tree_mode,
    )


def load_config(path: str | os.PathLike[str]) -> MonitorConfig:
    """Load validated preferences, returning defaults when the file is absent."""
    config_path = Path(path).expanduser()
    try:
        raw = config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return MonitorConfig()
    except OSError as exc:
        raise ConfigError(f"could not read {config_path}: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON in {config_path}: {exc.msg}") from exc
    try:
        return _validated_config(data)
    except ConfigError as exc:
        raise ConfigError(f"invalid configuration in {config_path}: {exc}") from exc


def save_config(path: str | os.PathLike[str], config: MonitorConfig) -> None:
    """Atomically replace the config file; the temporary file is private (0600)."""
    config_path = Path(path).expanduser()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{config_path.name}.", suffix=".tmp",
        dir=str(config_path.parent),
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(config.to_dict(), stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, config_path)
    except BaseException:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
        raise
