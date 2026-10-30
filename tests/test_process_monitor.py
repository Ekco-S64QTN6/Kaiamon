from __future__ import annotations

import os
import signal
from pathlib import Path
from unittest.mock import patch

import pytest

from process_monitor import (
    CpuTimes,
    ProcessCollector,
    ProcessInfo,
    ProcessSnapshot,
    ProcessMonitorUI,
    cpu_usage,
    filter_processes,
    parse_cpu_times,
    parse_proc_stat,
    renice_process,
    signal_process,
    sort_processes,
    tree_processes,
    validate_nice_value,
    validate_process_target,
)


def _stat_line(
    pid: int, command: str, *, utime: int, stime: int, ppid: int = 1,
    priority: int = 20, nice: int = 0, start: int = 100,
) -> str:
    fields = [
        "S", str(ppid), "0", "0", "0", "0", "0", "0", "0", "0", "0",
        str(utime), str(stime), "0", "0", str(priority), str(nice), "1",
        "0", str(start), str(4096 * 100), "10",
    ]
    return f"{pid} ({command}) " + " ".join(fields)


def _fixture(root: Path, *, user_proc: bool = True) -> None:
    root.mkdir(exist_ok=True)
    (root / "stat").write_text(
        "cpu 100 0 50 850 0 0 0 0\n"
        "cpu0 50 0 25 425 0 0 0 0\n"
        "cpu1 50 0 25 425 0 0 0 0\n"
    )
    (root / "meminfo").write_text(
        "MemTotal: 100000 kB\nMemAvailable: 40000 kB\n"
    )
    if user_proc:
        proc = root / "123"
        proc.mkdir()
        (proc / "stat").write_text(
            _stat_line(123, "python worker (test)", utime=10, stime=5)
        )
        (proc / "status").write_text(
            "Name:\tpython\nUid:\t1000\t1001\t1000\t1000\n"
        )
        (proc / "statm").write_text("1000 100 20 0 0 0 0\n")
        (proc / "cmdline").write_bytes(b"python\0worker.py\0")


def _proc(
    pid: int, ppid: int, user: str, command: str, cpu: float = 0.0,
    mem: float = 0.0,
) -> ProcessInfo:
    return ProcessInfo(
        pid, ppid, user, 20, 0, 1000, 500, 100, "S", cpu, mem, 12.0,
        command, 10,
    )


def test_parse_proc_stat_handles_spaces_and_parentheses() -> None:
    parsed = parse_proc_stat(
        _stat_line(42, "name with (parens)", utime=11, stime=7, nice=4)
    )
    assert parsed["pid"] == 42
    assert parsed["command"] == "name with (parens)"
    assert parsed["utime"] == 11
    assert parsed["stime"] == 7
    assert parsed["nice"] == 4


def test_parse_proc_stat_rejects_truncated_data() -> None:
    with pytest.raises(ValueError):
        parse_proc_stat("12 (oops) S 1 2")


def test_cpu_counter_parsing_and_delta_percentage() -> None:
    old = parse_cpu_times("cpu 10 0 10 80 0\ncpu0 10 0 10 80 0\n")
    new = parse_cpu_times("cpu 20 0 20 160 0\ncpu0 20 0 20 160 0\n")
    assert cpu_usage(old[0], new[0]) == pytest.approx(20.0)
    assert cpu_usage(CpuTimes(5, 4), CpuTimes(5, 4)) == 0.0
    guest = parse_cpu_times("cpu 10 0 10 80 0 0 0 0 50 5\n")
    assert guest[0].total == 100


def test_process_collector_reads_fixture_and_publishes_nonempty_snapshot(
    tmp_path: Path,
) -> None:
    _fixture(tmp_path)
    collector = ProcessCollector(
        tmp_path, clock_ticks=100, page_size=4096,
        uid_lookup=lambda uid: f"uid{uid}",
    )
    first = collector.collect_once()
    assert len(first.processes) == 1
    process = first.processes[0]
    assert process.pid == 123
    assert process.user == "uid1001"
    assert process.command == "python worker.py"
    assert process.virt_kb == 400
    assert process.res_kb == 400
    assert process.shr_kb == 80
    assert process.cpu_time_s == pytest.approx(0.15)
    assert first.memory_total_kb == 100000
    assert first.memory_used_kb == 60000
    assert collector.get_snapshot() == first


def test_process_cpu_is_delta_based_and_starttime_guards_pid_reuse(
    tmp_path: Path,
) -> None:
    _fixture(tmp_path)
    collector = ProcessCollector(
        tmp_path, clock_ticks=100, page_size=4096,
        uid_lookup=lambda _uid: "tester",
    )
    collector.collect_once()
    proc = tmp_path / "123"
    (tmp_path / "stat").write_text(
        "cpu 200 0 100 1700 0 0 0 0\n"
        "cpu0 100 0 50 850 0 0 0 0\n"
        "cpu1 100 0 50 850 0 0 0 0\n"
    )
    (proc / "stat").write_text(
        _stat_line(123, "python worker", utime=30, stime=15)
    )
    second = collector.collect_once()
    assert second.processes[0].cpu_percent == pytest.approx(6.0)
    (proc / "stat").write_text(
        _stat_line(123, "python worker", utime=60, stime=30, start=999)
    )
    third = collector.collect_once()
    assert third.processes[0].cpu_percent == 0.0


def test_collector_skips_process_that_disappears_during_sample(
    tmp_path: Path,
) -> None:
    _fixture(tmp_path, user_proc=False)
    proc = tmp_path / "124"
    proc.mkdir()
    (proc / "stat").write_text(_stat_line(124, "gone", utime=1, stime=1))
    collector = ProcessCollector(tmp_path, clock_ticks=100, page_size=4096)
    snapshot = collector.collect_once()
    assert snapshot.processes == ()


def test_filter_matches_pid_user_and_command() -> None:
    rows = (
        _proc(10, 1, "alice", "python worker"),
        _proc(20, 10, "bob", "nginx"),
    )
    assert [p.pid for p in filter_processes(rows, "ALICE")] == [10]
    assert [p.pid for p in filter_processes(rows, "20")] == [20]
    assert [p.pid for p in filter_processes(rows, "PYTHON")] == [10]


def test_sort_processes_orders_by_selected_metric() -> None:
    rows = (
        _proc(10, 1, "a", "a", 12, 3),
        _proc(20, 1, "b", "b", 80, 1),
    )
    assert [p.pid for p in sort_processes(rows, "CPU")] == [20, 10]
    assert [p.pid for p in sort_processes(rows, "MEM", descending=False)] == [
        20, 10,
    ]
    with pytest.raises(ValueError):
        sort_processes(rows, "NOPE")


def test_tree_view_preserves_parent_child_order_and_orphans() -> None:
    rows = (
        _proc(1, 0, "root", "init"),
        _proc(2, 1, "root", "parent"),
        _proc(3, 2, "root", "child"),
        _proc(4, 99, "root", "orphan"),
    )
    flattened = tree_processes(rows, "PID", descending=False)
    assert [(p.pid, depth) for p, depth in flattened] == [
        (1, 0), (2, 1), (3, 2), (4, 0),
    ]


def test_tree_handles_parent_cycles_without_losing_rows() -> None:
    rows = (_proc(10, 11, "a", "a"), _proc(11, 10, "b", "b"))
    assert {p.pid for p, _depth in tree_processes(rows, "PID")} == {10, 11}


def test_process_action_validation_blocks_pid_one_self_and_bad_nice() -> None:
    with pytest.raises(ValueError):
        validate_process_target(1, current_pid=999)
    with pytest.raises(ValueError):
        validate_process_target(999, current_pid=999)
    with pytest.raises(ValueError):
        validate_nice_value(-21)
    with pytest.raises(ValueError):
        validate_nice_value(20)
    assert validate_nice_value(-20) == -20


def test_process_signal_and_renice_call_os_only_after_validation() -> None:
    with (
        patch("process_monitor.os.getpid", return_value=999),
        patch("process_monitor.os.kill") as kill,
    ):
        signal_process(123, int(signal.SIGTERM))
        kill.assert_called_once_with(123, signal.SIGTERM)
    with (
        patch("process_monitor.os.getpid", return_value=999),
        patch("process_monitor.os.setpriority") as setpriority,
    ):
        renice_process(123, 5)
        setpriority.assert_called_once_with(os.PRIO_PROCESS, 123, 5)


def test_ui_stops_collector_when_curses_wrapper_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ui = ProcessMonitorUI()
    calls: list[str] = []

    class FakeCollector:
        def start(self) -> None:
            calls.append("start")

        def stop(self) -> None:
            calls.append("stop")

    ui.collector = FakeCollector()  # type: ignore[assignment]

    def fail_wrapper(_callback: object) -> None:
        raise RuntimeError("curses startup failed")

    monkeypatch.setattr("process_monitor.curses.wrapper", fail_wrapper)
    with pytest.raises(RuntimeError, match="curses startup failed"):
        ui.run()
    assert calls == ["start", "stop"]

def test_signal_handler_requests_clean_ui_shutdown() -> None:
    ui = ProcessMonitorUI()
    ui._running = True
    ui._signal_handler(signal.SIGTERM, None)
    assert ui._running is False


def test_cpu_meter_renders_bounded_per_core_bar() -> None:
    assert ProcessMonitorUI._meter(50.0, 4) == "[██  ]"
    assert ProcessMonitorUI._meter(150.0, 3) == "[███]"
    assert ProcessMonitorUI._meter(-1.0, 3) == "[   ]"


def test_signal_action_requires_explicit_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    process = _proc(321, 1, "tester", "worker")
    snapshot = ProcessSnapshot(processes=(process,))

    class FakeCollector:
        def get_snapshot(self) -> ProcessSnapshot:
            return snapshot

    ui = ProcessMonitorUI()
    ui.collector = FakeCollector()  # type: ignore[assignment]
    prompts = iter(("1", "n"))
    monkeypatch.setattr(ui, "_prompt", lambda _prompt: next(prompts))
    signal_calls: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "process_monitor.signal_process",
        lambda pid, signum: signal_calls.append((pid, signum)),
    )

    ui._handle_key(ord("K"))

    assert signal_calls == []
    assert ui.status == "TERM cancelled for PID 321"


def test_signal_action_runs_only_after_yes_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    process = _proc(321, 1, "tester", "worker")
    snapshot = ProcessSnapshot(processes=(process,))

    class FakeCollector:
        def get_snapshot(self) -> ProcessSnapshot:
            return snapshot

    ui = ProcessMonitorUI()
    ui.collector = FakeCollector()  # type: ignore[assignment]
    prompts = iter(("2", "Y"))
    monkeypatch.setattr(ui, "_prompt", lambda _prompt: next(prompts))
    signal_calls: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "process_monitor.signal_process",
        lambda pid, signum: signal_calls.append((pid, signum)),
    )

    ui._handle_key(ord("K"))

    assert signal_calls == [(321, int(signal.SIGKILL))]
    assert ui.status == "Sent KILL to PID 321"
