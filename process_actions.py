"""Validated Linux process-control operations, independent of curses.

The curses UI is responsible for prompting and confirmation. This module owns
PID/nice/signal/affinity validation, start-time-verified process signalling and
renicing, start-time-verified thread renicing, and the corresponding operating-system calls.
"""
from __future__ import annotations

import os
import signal
from collections.abc import Iterable
from pathlib import Path

from process_core import parse_proc_stat


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
        raise ValueError("refusing to change the monitor itself")


def validate_nice_value(nice: int) -> int:
    """Validate a Linux nice value."""
    if isinstance(nice, bool) or not isinstance(nice, int) or not -20 <= nice <= 19:
        raise ValueError("nice value must be an integer from -20 through 19")
    return nice


def _verify_process_start(
    pid: int, expected_start_ticks: int, proc_root: str | Path
) -> None:
    """Reject a PID that exited or was reused since the user selected it."""
    if (
        isinstance(expected_start_ticks, bool)
        or not isinstance(expected_start_ticks, int)
        or expected_start_ticks < 0
    ):
        raise ValueError("expected process start ticks must be a non-negative integer")
    try:
        stat = parse_proc_stat((Path(proc_root) / str(pid) / "stat").read_text())
    except OSError as exc:
        raise OSError(f"cannot verify PID {pid}: {exc}") from exc
    if stat["pid"] != pid or stat["start_ticks"] != expected_start_ticks:
        raise ValueError(f"PID {pid} exited or was reused; nothing was changed")


def signal_process(
    pid: int,
    signum: int,
    expected_start_ticks: int | None = None,
    *,
    proc_root: str | Path = "/proc",
) -> None:
    """Send one of the explicitly supported signals to a validated PID.

    With ``expected_start_ticks`` the target is pinned with a pidfd before its
    start time is checked, so a PID recycled while the user answered the
    prompts cannot receive the signal. The UI always passes it; it is optional
    only for callers that have no snapshot identity.
    """
    validate_process_target(pid)
    allowed = {value for value, _label in SIGNAL_CHOICES.values()}
    if isinstance(signum, bool) or not isinstance(signum, int) or signum not in allowed:
        raise ValueError("unsupported process signal")
    if expected_start_ticks is None:
        os.kill(pid, signum)
        return

    pidfd_open = getattr(os, "pidfd_open", None)
    pidfd_send_signal = getattr(signal, "pidfd_send_signal", None)
    if pidfd_open is None or pidfd_send_signal is None:
        # No pidfd support: verify, then signal. A reuse in between is still
        # possible, but the window is microseconds rather than two prompts.
        _verify_process_start(pid, expected_start_ticks, proc_root)
        os.kill(pid, signum)
        return

    pidfd = pidfd_open(pid)
    try:
        # The pidfd refers to whichever process held the PID when it was
        # opened; if that is the selected process, it cannot be swapped out
        # before the signal is delivered.
        _verify_process_start(pid, expected_start_ticks, proc_root)
        pidfd_send_signal(pidfd, signum)
    finally:
        os.close(pidfd)


def renice_process(
    pid: int,
    nice: int,
    expected_start_ticks: int | None = None,
    *,
    proc_root: str | Path = "/proc",
) -> None:
    """Set the nice value for a validated process, rejecting a reused PID."""
    validate_process_target(pid)
    nice_value = validate_nice_value(nice)
    if expected_start_ticks is not None:
        _verify_process_start(pid, expected_start_ticks, proc_root)
    os.setpriority(os.PRIO_PROCESS, pid, nice_value)


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



def renice_thread(
    tgid: int,
    tid: int,
    expected_tgid_start_ticks: int,
    expected_tid_start_ticks: int,
    nice: int,
    *,
    proc_root: str | Path = "/proc",
) -> int:
    """Renice one verified Linux thread, rejecting stale or reused identifiers."""
    validate_process_target(tgid)
    if isinstance(tid, bool) or not isinstance(tid, int) or tid <= 1:
        raise ValueError("TID must identify a non-system thread greater than 1")
    for name, value in (
        ("process", expected_tgid_start_ticks),
        ("thread", expected_tid_start_ticks),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"expected {name} start ticks must be a non-negative integer")
    nice_value = validate_nice_value(nice)

    root = Path(proc_root)
    try:
        # Read the thread first and the owning process last, minimizing the
        # interval between the final owner-identity check and setpriority.
        thread_stat = parse_proc_stat(
            (root / str(tgid) / "task" / str(tid) / "stat").read_text()
        )
        process_stat = parse_proc_stat((root / str(tgid) / "stat").read_text())
    except OSError as exc:
        raise OSError(f"cannot verify TID {tid} in process {tgid}: {exc}") from exc

    if (
        process_stat["pid"] != tgid
        or process_stat["start_ticks"] != expected_tgid_start_ticks
    ):
        raise ValueError(f"PID {tgid} exited or was reused; thread nice was not changed")
    if (
        thread_stat["pid"] != tid
        or thread_stat["start_ticks"] != expected_tid_start_ticks
    ):
        raise ValueError(f"TID {tid} exited or was reused; thread nice was not changed")

    os.setpriority(os.PRIO_PROCESS, tid, nice_value)
    return os.getpriority(os.PRIO_PROCESS, tid)
