"""Process signal/renice must not act on a PID reused since it was selected."""
from __future__ import annotations

import os
import signal
from pathlib import Path

import pytest

from process_actions import renice_process, signal_process


def _stat_line(pid: int, command: str, *, start: int) -> str:
    fields = [
        "S", "1", "0", "0", "0", "0", "0", "0", "0", "0", "0",
        "10", "5", "0", "0", "20", "0", "1", "0", str(start),
        "409600", "10",
    ]
    return f"{pid} ({command}) " + " ".join(fields)


def _process(root: Path, pid: int, *, start: int) -> None:
    (root / str(pid)).mkdir(parents=True)
    (root / str(pid) / "stat").write_text(_stat_line(pid, "worker", start=start))


@pytest.fixture
def pidfd(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int, int]]:
    calls: list[tuple[str, int, int]] = []
    monkeypatch.setattr("process_actions.os.getpid", lambda: 999)
    monkeypatch.setattr(
        "process_actions.os.pidfd_open",
        lambda pid: calls.append(("open", pid, 0)) or 77,
        raising=False,
    )
    monkeypatch.setattr(
        "process_actions.signal.pidfd_send_signal",
        lambda fd, signum: calls.append(("send", fd, signum)),
        raising=False,
    )
    monkeypatch.setattr("process_actions.os.close", lambda fd: calls.append(("close", fd, 0)))
    monkeypatch.setattr(
        "process_actions.os.kill", lambda *_a: pytest.fail("pidfd path must not use os.kill")
    )
    return calls


def test_signal_goes_through_pidfd_when_identity_matches(
    tmp_path: Path, pidfd: list[tuple[str, int, int]],
) -> None:
    _process(tmp_path, 321, start=100)

    signal_process(321, int(signal.SIGTERM), 100, proc_root=tmp_path)

    assert pidfd == [("open", 321, 0), ("send", 77, int(signal.SIGTERM)), ("close", 77, 0)]


def test_signal_refuses_reused_pid_and_closes_pidfd(
    tmp_path: Path, pidfd: list[tuple[str, int, int]],
) -> None:
    _process(tmp_path, 321, start=555)  # same PID, different process

    with pytest.raises(ValueError, match="exited or was reused"):
        signal_process(321, int(signal.SIGKILL), 100, proc_root=tmp_path)

    assert ("send", 77, int(signal.SIGKILL)) not in pidfd
    assert pidfd[-1] == ("close", 77, 0)


def test_signal_without_pidfd_support_still_verifies_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _process(tmp_path, 321, start=555)
    monkeypatch.setattr("process_actions.os.getpid", lambda: 999)
    monkeypatch.delattr("process_actions.os.pidfd_open", raising=False)
    monkeypatch.setattr("process_actions.os.kill", lambda *_a: pytest.fail("reused PID signalled"))

    with pytest.raises(ValueError, match="exited or was reused"):
        signal_process(321, int(signal.SIGTERM), 100, proc_root=tmp_path)


def test_renice_refuses_reused_pid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _process(tmp_path, 321, start=555)
    monkeypatch.setattr("process_actions.os.getpid", lambda: 999)
    monkeypatch.setattr(
        "process_actions.os.setpriority", lambda *_a: pytest.fail("reused PID reniced")
    )

    with pytest.raises(ValueError, match="exited or was reused"):
        renice_process(321, 5, 100, proc_root=tmp_path)


def test_renice_applies_when_identity_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _process(tmp_path, 321, start=100)
    calls: list[tuple[int, int, int]] = []
    monkeypatch.setattr("process_actions.os.getpid", lambda: 999)
    monkeypatch.setattr(
        "process_actions.os.setpriority", lambda which, who, nice: calls.append((which, who, nice))
    )

    renice_process(321, 5, 100, proc_root=tmp_path)

    assert calls == [(os.PRIO_PROCESS, 321, 5)]


def test_exited_process_is_reported_not_signalled(
    tmp_path: Path, pidfd: list[tuple[str, int, int]],
) -> None:
    with pytest.raises(OSError, match="cannot verify PID 321"):
        signal_process(321, int(signal.SIGTERM), 100, proc_root=tmp_path)
    assert all(call[0] != "send" for call in pidfd)
