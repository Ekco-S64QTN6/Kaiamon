"""Validated Linux process-control operations, independent of curses.

The curses UI is responsible for prompting and confirmation. This module owns
PID/nice/signal/affinity validation and the corresponding operating-system calls.
"""
from __future__ import annotations

import os
import signal
from collections.abc import Iterable


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


def get_available_cpus() -> frozenset[int]:
    """Return the CPUs currently available to the monitor's own cpuset."""
    cpus = frozenset(os.sched_getaffinity(0))
    if not cpus:
        raise OSError("the current process has no available CPUs")
    return cpus


def get_process_affinity(pid: int) -> frozenset[int]:
    """Return the allowed CPU IDs for a validated process."""
    validate_process_target(pid)
    cpus = frozenset(os.sched_getaffinity(pid))
    if not cpus:
        raise OSError(f"PID {pid} has no available CPUs")
    return cpus


def set_process_affinity(pid: int, cpus: Iterable[int]) -> frozenset[int]:
    """Set a validated process CPU mask and return the effective kernel mask."""
    validate_process_target(pid)
    if isinstance(cpus, (str, bytes)):
        raise ValueError("CPU affinity must be an iterable of CPU IDs")
    try:
        requested = set(cpus)
    except TypeError as exc:
        raise ValueError("CPU affinity must be an iterable of CPU IDs") from exc
    if not requested:
        raise ValueError("at least one CPU must remain enabled")
    if any(
        isinstance(cpu, bool) or not isinstance(cpu, int) or cpu < 0
        for cpu in requested
    ):
        raise ValueError("CPU IDs must be non-negative integers")
    os.sched_setaffinity(pid, requested)
    effective = frozenset(os.sched_getaffinity(pid))
    if not effective:
        raise OSError(f"PID {pid} has no available CPUs after applying affinity")
    return effective
