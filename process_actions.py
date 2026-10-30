"""Validated Linux process-control operations, independent of curses.

The curses UI is responsible for prompting and confirmation. This module owns
PID/nice/signal validation and the corresponding operating-system calls.
"""
from __future__ import annotations

import os
import signal


SIGNAL_CHOICES: dict[str, tuple[int, str]] = {
    "1": (int(signal.SIGTERM), "TERM"),
    "2": (int(signal.SIGKILL), "KILL"),
    "3": (int(signal.SIGINT), "INT"),
    "4": (int(signal.SIGHUP), "HUP"),
}


def validate_process_target(pid: int, *, current_pid: int | None = None) -> None:
    """Reject invalid, system, and self-targeted process operations."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        raise ValueError("PID must identify a non-system process greater than 1")
    if current_pid is None:
        current_pid = os.getpid()
    if pid == current_pid:
        raise ValueError("refusing to signal or renice the monitor itself")


def validate_nice_value(nice: int) -> int:
    """Validate a Linux nice value."""
    if isinstance(nice, bool) or not isinstance(nice, int) or not -20 <= nice <= 19:
        raise ValueError("nice value must be an integer from -20 through 19")
    return nice


def signal_process(pid: int, signum: int) -> None:
    """Send one of the explicitly supported signals to a validated PID."""
    validate_process_target(pid)
    allowed = {value for value, _label in SIGNAL_CHOICES.values()}
    if isinstance(signum, bool) or not isinstance(signum, int) or signum not in allowed:
        raise ValueError("unsupported process signal")
    os.kill(pid, signum)


def renice_process(pid: int, nice: int) -> None:
    """Set the nice value for a validated process."""
    validate_process_target(pid)
    os.setpriority(os.PRIO_PROCESS, pid, validate_nice_value(nice))
