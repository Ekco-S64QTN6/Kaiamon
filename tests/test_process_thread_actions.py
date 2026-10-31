from __future__ import annotations

from pathlib import Path

import pytest

from process_actions import renice_thread


def _stat_line(pid: int, command: str, *, start: int) -> str:
    fields = [
        "S", "1", "0", "0", "0", "0", "0", "0", "0", "0", "0",
        "10", "5", "0", "0", "20", "0", "1", "0", str(start),
        "409600", "10",
    ]
    return f"{pid} ({command}) " + " ".join(fields)


def _thread_fixture(root: Path, *, process_start: int = 100, thread_start: int = 200) -> None:
    task = root / "321" / "task" / "322"
    task.mkdir(parents=True)
    (root / "321" / "stat").write_text(
        _stat_line(321, "python parent", start=process_start)
    )
    (task / "stat").write_text(
        _stat_line(322, "worker thread", start=thread_start)
    )


def test_renice_thread_checks_parent_and_thread_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _thread_fixture(tmp_path)
    calls: list[tuple[int, int, int]] = []
    monkeypatch.setattr("process_actions.os.getpid", lambda: 999)
    monkeypatch.setattr(
        "process_actions.os.setpriority",
        lambda which, who, nice: calls.append((which, who, nice)),
    )
    monkeypatch.setattr("process_actions.os.getpriority", lambda _which, _who: 4)

    actual = renice_thread(321, 322, 100, 200, 4, proc_root=tmp_path)

    assert actual == 4
    assert calls == [(0, 322, 4)]


@pytest.mark.parametrize(
    "process_start, thread_start, expected_process, expected_thread",
    [
        (999, 200, 100, 200),
        (100, 999, 100, 200),
    ],
)
def test_renice_thread_rejects_reused_pid_or_tid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    process_start: int, thread_start: int,
    expected_process: int, expected_thread: int,
) -> None:
    _thread_fixture(tmp_path, process_start=process_start, thread_start=thread_start)
    monkeypatch.setattr("process_actions.os.getpid", lambda: 999)
    monkeypatch.setattr(
        "process_actions.os.setpriority",
        lambda *_args: pytest.fail("stale thread reached setpriority"),
    )

    with pytest.raises(ValueError, match="exited or was reused"):
        renice_thread(
            321, 322, expected_process, expected_thread, 0, proc_root=tmp_path,
        )


def test_renice_thread_rejects_missing_or_malformed_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _thread_fixture(tmp_path)
    monkeypatch.setattr("process_actions.os.getpid", lambda: 999)
    (tmp_path / "321" / "task" / "322" / "stat").unlink()

    with pytest.raises(OSError, match="cannot verify TID"):
        renice_thread(321, 322, 100, 200, 0, proc_root=tmp_path)


@pytest.mark.parametrize(
    "tgid, tid, expected_process, expected_thread, nice",
    [
        (1, 2, 100, 200, 0),
        (999, 322, 100, 200, 0),
        (321, 1, 100, 200, 0),
        (321, 322, -1, 200, 0),
        (321, 322, 100, -1, 0),
        (321, 322, 100, 200, 20),
    ],
)
def test_renice_thread_validates_targets_and_values_before_io(
    monkeypatch: pytest.MonkeyPatch,
    tgid: int, tid: int, expected_process: int,
    expected_thread: int, nice: int,
) -> None:
    monkeypatch.setattr("process_actions.os.getpid", lambda: 999)
    monkeypatch.setattr(
        "process_actions.parse_proc_stat",
        lambda _contents: pytest.fail("invalid thread arguments reached procfs"),
    )

    with pytest.raises(ValueError):
        renice_thread(
            tgid, tid, expected_process, expected_thread, nice,
        )

@pytest.mark.parametrize("stat_path", ["321/stat", "321/task/322/stat"])
def test_renice_thread_rejects_malformed_identity_files_before_syscalls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stat_path: str,
) -> None:
    _thread_fixture(tmp_path)
    monkeypatch.setattr("process_actions.os.getpid", lambda: 999)
    (tmp_path / stat_path).write_text("truncated")
    monkeypatch.setattr(
        "process_actions.os.setpriority",
        lambda *_args: pytest.fail("malformed procfs identity reached setpriority"),
    )

    with pytest.raises(ValueError):
        renice_thread(321, 322, 100, 200, 0, proc_root=tmp_path)

def test_renice_thread_reads_owner_identity_after_thread_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _thread_fixture(tmp_path)
    original_read_text = Path.read_text
    reads: list[str] = []

    def record_reads(path: Path, *args: object, **kwargs: object) -> str:
        reads.append(str(path))
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr("process_actions.os.getpid", lambda: 999)
    monkeypatch.setattr(Path, "read_text", record_reads)
    monkeypatch.setattr("process_actions.os.setpriority", lambda *_args: None)
    monkeypatch.setattr("process_actions.os.getpriority", lambda *_args: 4)

    assert renice_thread(321, 322, 100, 200, 4, proc_root=tmp_path) == 4
    assert reads == [
        str(tmp_path / "321" / "task" / "322" / "stat"),
        str(tmp_path / "321" / "stat"),
    ]
