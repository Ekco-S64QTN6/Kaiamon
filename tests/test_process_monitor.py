from __future__ import annotations

import argparse
import curses
from dataclasses import replace
import os
import signal
from pathlib import Path
from unittest.mock import patch

import pytest

from process_core import (
    CpuTimes,
    ProcessCollector,
    ProcessInfo,
    ProcessSnapshot,
    ThreadInfo,
    cpu_usage,
    parse_cpu_times,
    parse_diskstats,
    parse_network_dev,
    parse_boot_time,
    parse_proc_cgroup,
    parse_proc_io,
    parse_proc_stat,
)
from process_actions import (
    renice_process, signal_process, validate_nice_value, validate_process_target,
)
from process_monitor import (
    DEFAULT_COLUMNS,
    ProcessMonitorUI,
    filter_processes,
    filter_tree_context,
    parse_columns,
    parse_refresh_interval,
    sort_processes,
    tree_processes,
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
        "btime 1700000000\n"
    )
    (root / "meminfo").write_text(
        "MemTotal: 100000 kB\nMemAvailable: 40000 kB\n"
    )
    (root / "diskstats").write_text(
        "8 0 sda 10 0 10 1 20 0 20 2 0 0 0 0\n"
        "8 1 sda1 5 0 5 1 10 0 10 1 0 0 0 0\n"
    )
    (root / "net").mkdir(exist_ok=True)
    (root / "net" / "dev").write_text(
        "Inter-| Receive | Transmit\n"
        " face |bytes packets errs drop fifo frame compressed multicast|bytes packets errs drop fifo colls carrier compressed\n"
        "eth0: 100 1 0 0 0 0 0 0 200 1 0 0 0 0 0 0\n"
        "lo: 50 1 0 0 0 0 0 0 60 1 0 0 0 0 0 0\n"
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
        (proc / "cgroup").write_text(
            "12:memory:/docker/legacy-container\n"
            "0::/system.slice/kaiamon.service\n"
        )
        (proc / "io").write_text("rchar: 5000\nwchar: 3000\nsyscr: 4\nsyscw: 2\nread_bytes: 4096\nwrite_bytes: 2048\n")
        main_task = proc / "task" / "123"
        main_task.mkdir(parents=True)
        (main_task / "stat").write_text(
            _stat_line(123, "python", utime=10, stime=5, start=100)
        )
        worker_task = proc / "task" / "456"
        worker_task.mkdir()
        (worker_task / "stat").write_text(
            _stat_line(456, "worker-thread", utime=2, stime=1, start=200)
        )


def _proc(
    pid: int, ppid: int, user: str, command: str, cpu: float = 0.0,
    mem: float = 0.0,
) -> ProcessInfo:
    return ProcessInfo(
        pid, ppid, user, 20, 0, 1000, 500, 100, "S", cpu, mem, 12.0,
        command, 10,
    )


def test_parse_diskstats_converts_sectors_to_bytes() -> None:
    devices = parse_diskstats(
        "8 0 sda 10 0 100 1 20 0 200 2 0 0 0 0\n"
        "8 1 sda1 5 0 50 1 10 0 100 1 0 0 0 0\n"
    )

    assert [(device.device, device.read_bytes, device.write_bytes) for device in devices] == [
        ("sda", 51200, 102400),
        ("sda1", 25600, 51200),
    ]


def test_parse_diskstats_rejects_truncated_counters() -> None:
    with pytest.raises(ValueError, match="truncated"):
        parse_diskstats("8 0 sda 1 0 1")


def test_parse_network_dev_skips_headers_and_reads_interface_counters() -> None:
    interfaces = parse_network_dev(
        "Inter-| Receive | Transmit\n"
        " face |bytes packets errs drop fifo frame compressed multicast|bytes packets errs drop fifo colls carrier compressed\n"
        "eth0: 100 1 0 0 0 0 0 0 200 1 0 0 0 0 0 0\n"
        "lo: 50 1 0 0 0 0 0 0 60 1 0 0 0 0 0 0\n"
    )

    assert [(item.interface, item.receive_bytes, item.transmit_bytes) for item in interfaces] == [
        ("eth0", 100, 200),
        ("lo", 50, 60),
    ]


def test_parse_network_dev_rejects_truncated_interface_counters() -> None:
    with pytest.raises(ValueError, match="truncated"):
        parse_network_dev("eth0: 1 2 3\n")



def test_parse_proc_cgroup_prefers_unified_v2_hierarchy() -> None:
    contents = (
        "12:memory:/docker/legacy-container\n"
        "0::/system.slice/kaiamon.service\n"
        "7:cpu,cpuacct:/legacy/cpu\n"
    )

    assert parse_proc_cgroup(contents) == "/system.slice/kaiamon.service"


def test_parse_proc_cgroup_falls_back_to_deepest_v1_path() -> None:
    contents = (
        "2:cpu:/system.slice/workload\n"
        "3:memory:/system.slice/workload/container\n"
        "4:pids:/system.slice/workload\n"
    )

    assert parse_proc_cgroup(contents) == "/system.slice/workload/container"


@pytest.mark.parametrize("contents", ["", "malformed", "0:cpu:/invalid", "2:memory:relative"])
def test_parse_proc_cgroup_returns_none_for_unusable_data(contents: str) -> None:
    assert parse_proc_cgroup(contents) is None


def test_parse_proc_io_reads_storage_byte_counters() -> None:
    assert parse_proc_io(
        "rchar: 5000\nwchar: 3000\nread_bytes: 4096\nwrite_bytes: 2048\n"
    ) == (4096, 2048)


@pytest.mark.parametrize(
    "contents",
    [
        "read_bytes: 1\n",
        "read_bytes: nope\nwrite_bytes: 2\n",
        "read_bytes: -1\nwrite_bytes: 2\n",
    ],
)
def test_parse_proc_io_rejects_missing_or_invalid_counters(contents: str) -> None:
    with pytest.raises(ValueError):
        parse_proc_io(contents)


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
    assert process.start_time_s == pytest.approx(1700000001.0)
    assert process.cgroup == "/system.slice/kaiamon.service"
    assert process.io_read_rate is None
    assert process.io_write_rate is None
    assert first.memory_total_kb == 100000
    assert first.memory_used_kb == 60000
    assert collector.get_snapshot() == first



def test_parse_boot_time_reads_proc_stat_epoch() -> None:
    assert parse_boot_time("cpu 1 2 3 4\nbtime 1700000000\n") == 1700000000


def test_parse_boot_time_handles_missing_or_invalid_field() -> None:
    assert parse_boot_time("cpu 1 2 3 4\n") is None
    with pytest.raises(ValueError, match="invalid"):
        parse_boot_time("btime yesterday\n")
    with pytest.raises(ValueError, match="negative"):
        parse_boot_time("btime -1\n")


def test_start_and_elapsed_sorting_use_process_start_epoch() -> None:
    rows = (
        replace(_proc(10, 1, "a", "older"), start_time_s=100.0),
        replace(_proc(20, 1, "b", "newer"), start_time_s=200.0),
    )

    assert [process.pid for process in sort_processes(rows, "START")] == [20, 10]
    assert [process.pid for process in sort_processes(rows, "ELAPSED")] == [10, 20]
    unknown = replace(rows[0], pid=30, start_time_s=None)
    with_unknown = rows + (unknown,)
    assert [process.pid for process in sort_processes(with_unknown, "START")] == [20, 10, 30]
    assert [process.pid for process in sort_processes(with_unknown, "START", False)] == [10, 20, 30]
    assert [process.pid for process in sort_processes(with_unknown, "ELAPSED")] == [10, 20, 30]
    assert [process.pid for process in sort_processes(iter(with_unknown), "START")] == [20, 10, 30]



def test_f6_cycles_through_start_and_elapsed_sort_modes() -> None:
    ui = ProcessMonitorUI(sort_field="IO_WRITE", descending=False)

    ui._handle_key(curses.KEY_F6)
    assert ui.sort_field == "START"
    assert ui.descending is True

    ui._handle_key(curses.KEY_F6)
    assert ui.sort_field == "START"
    assert ui.descending is False

    ui._handle_key(curses.KEY_F6)
    assert ui.sort_field == "ELAPSED"
    assert ui.descending is True


def test_start_and_elapsed_time_formatters_handle_today_old_and_missing() -> None:
    from datetime import datetime

    now = 1700003600.0
    start_today = now - 300.0
    start_old = now - 3 * 86400.0

    assert ProcessMonitorUI._fmt_start_time(start_today, now) == datetime.fromtimestamp(
        start_today
    ).strftime("%H:%M")
    assert ProcessMonitorUI._fmt_start_time(start_old, now) == datetime.fromtimestamp(
        start_old
    ).strftime("%b%d")
    assert ProcessMonitorUI._fmt_start_time(None, now) == "—"
    assert ProcessMonitorUI._fmt_elapsed(now - (2 * 86400 + 3 * 3600 + 4 * 60), now) == (
        "2d03:04"
    )
    assert ProcessMonitorUI._fmt_elapsed(None, now) == "—"



@pytest.mark.parametrize("boot_line", [None, "btime invalid\n"])

def test_missing_process_cgroup_file_does_not_hide_process(tmp_path: Path) -> None:
    _fixture(tmp_path)
    (tmp_path / "123" / "cgroup").unlink()
    collector = ProcessCollector(
        tmp_path, clock_ticks=100, page_size=4096,
        uid_lookup=lambda _uid: "tester",
    )

    snapshot = collector.collect_once()

    assert [process.pid for process in snapshot.processes] == [123]
    assert snapshot.processes[0].cgroup is None


def test_collector_keeps_process_visible_when_boot_time_unavailable(
    tmp_path: Path, boot_line: str | None,
) -> None:
    _fixture(tmp_path)
    stat_path = tmp_path / "stat"
    existing = [
        line for line in stat_path.read_text().splitlines()
        if not line.startswith("btime ")
    ]
    if boot_line is not None:
        existing.append(boot_line.strip())
    stat_path.write_text("\n".join(existing) + "\n")
    collector = ProcessCollector(
        tmp_path, clock_ticks=100, page_size=4096,
        uid_lookup=lambda _uid: "tester",
    )

    snapshot = collector.collect_once()

    assert [process.pid for process in snapshot.processes] == [123]
    assert snapshot.processes[0].start_time_s is None


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


def test_process_io_rates_use_deltas_and_guard_pid_reuse(
    tmp_path: Path,
) -> None:
    _fixture(tmp_path)
    sample_times = iter((10.0, 12.0, 14.0, 16.0))
    collector = ProcessCollector(
        tmp_path, clock_ticks=100, page_size=4096,
        uid_lookup=lambda _uid: "tester",
        monotonic_clock=lambda: next(sample_times),
    )

    first = collector.collect_once()
    assert first.processes[0].io_read_rate is None
    assert first.processes[0].io_write_rate is None

    proc = tmp_path / "123"
    (proc / "io").write_text(
        "rchar: 9000\nwchar: 5000\nread_bytes: 8192\nwrite_bytes: 4096\n"
    )
    second = collector.collect_once()
    assert second.processes[0].io_read_rate == pytest.approx(2048.0)
    assert second.processes[0].io_write_rate == pytest.approx(1024.0)

    (proc / "stat").write_text(
        _stat_line(123, "reused worker", utime=1, stime=1, start=999)
    )
    (proc / "io").write_text(
        "read_bytes: 16384\nwrite_bytes: 8192\n"
    )
    third = collector.collect_once()
    assert third.processes[0].io_read_rate is None
    assert third.processes[0].io_write_rate is None

    # Counter rollback without PID reuse is clamped instead of producing negatives.
    (proc / "io").write_text("read_bytes: 8192\nwrite_bytes: 4096\n")
    fourth = collector.collect_once()
    assert fourth.processes[0].io_read_rate == 0.0
    assert fourth.processes[0].io_write_rate == 0.0


def test_missing_process_io_file_does_not_hide_process(
    tmp_path: Path,
) -> None:
    _fixture(tmp_path)
    (tmp_path / "123" / "io").unlink()
    collector = ProcessCollector(
        tmp_path, clock_ticks=100, page_size=4096,
        uid_lookup=lambda _uid: "tester",
    )

    snapshot = collector.collect_once()

    assert [process.pid for process in snapshot.processes] == [123]
    assert snapshot.processes[0].io_read_rate is None
    assert snapshot.processes[0].io_write_rate is None



def test_system_disk_and_network_rates_use_per_device_deltas(
    tmp_path: Path,
) -> None:
    _fixture(tmp_path)
    sample_times = iter((10.0, 12.0, 14.0))
    collector = ProcessCollector(
        tmp_path,
        clock_ticks=100,
        page_size=4096,
        uid_lookup=lambda _uid: "tester",
        monotonic_clock=lambda: next(sample_times),
    )

    first = collector.collect_once()
    assert {item.device for item in first.disk_io} == {"sda", "sda1"}
    assert {item.interface for item in first.network_io} == {"eth0", "lo"}
    assert all(item.read_bytes_s is None for item in first.disk_io)
    assert all(item.receive_bytes_s is None for item in first.network_io)

    (tmp_path / "diskstats").write_text(
        "8 0 sda 10 0 110 1 20 0 220 2 0 0 0 0\n"
        "8 1 sda1 5 0 60 1 10 0 120 1 0 0 0 0\n"
    )
    (tmp_path / "net" / "dev").write_text(
        "Inter-| Receive | Transmit\n"
        " face |bytes packets errs drop fifo frame compressed multicast|bytes packets errs drop fifo colls carrier compressed\n"
        "eth0: 500 1 0 0 0 0 0 0 1000 1 0 0 0 0 0 0\n"
        "lo: 70 1 0 0 0 0 0 0 100 1 0 0 0 0 0 0\n"
    )
    second = collector.collect_once()
    disks = {item.device: item for item in second.disk_io}
    networks = {item.interface: item for item in second.network_io}
    assert disks["sda"].read_bytes_s == pytest.approx(25_600.0)
    assert disks["sda"].write_bytes_s == pytest.approx(51_200.0)
    assert networks["eth0"].receive_bytes_s == pytest.approx(200.0)
    assert networks["eth0"].transmit_bytes_s == pytest.approx(400.0)

    # Counter resets or device replacement must not create negative throughput.
    (tmp_path / "diskstats").write_text("8 0 sda 10 0 1 1 20 0 1 2 0 0 0 0\n")
    (tmp_path / "net" / "dev").write_text(
        "Inter-| Receive | Transmit\n"
        " face |bytes packets errs drop fifo frame compressed multicast|bytes packets errs drop fifo colls carrier compressed\n"
        "eth0: 1 1 0 0 0 0 0 0 1 1 0 0 0 0 0 0\n"
    )
    third = collector.collect_once()
    assert {item.device: item for item in third.disk_io}["sda"].read_bytes_s == 0.0
    assert {item.interface: item for item in third.network_io}["eth0"].receive_bytes_s == 0.0


def test_missing_system_io_files_do_not_break_process_collection(
    tmp_path: Path,
) -> None:
    _fixture(tmp_path)
    (tmp_path / "diskstats").unlink()
    (tmp_path / "net" / "dev").unlink()
    collector = ProcessCollector(
        tmp_path, clock_ticks=100, page_size=4096,
        uid_lookup=lambda _uid: "tester",
    )

    snapshot = collector.collect_once()

    assert [process.pid for process in snapshot.processes] == [123]
    assert snapshot.disk_io == ()
    assert snapshot.network_io == ()



def test_selected_process_thread_sampling_tracks_cpu_and_pid_reuse(
    tmp_path: Path,
) -> None:
    _fixture(tmp_path)
    collector = ProcessCollector(
        tmp_path, clock_ticks=100, page_size=4096,
        uid_lookup=lambda _uid: "tester",
    )

    assert collector.collect_once().threads == ()
    collector.set_thread_target(123, 100)
    first = collector.collect_once()
    assert {thread.tid for thread in first.threads} == {123, 456}
    assert all(thread.cpu_percent == 0.0 for thread in first.threads)

    (tmp_path / "stat").write_text(
        "cpu 200 0 50 1750 0 0 0 0\n"
        "cpu0 100 0 25 875 0 0 0 0\n"
        "cpu1 100 0 25 875 0 0 0 0\n"
    )
    (tmp_path / "123" / "task" / "123" / "stat").write_text(
        _stat_line(123, "python", utime=110, stime=5, start=100)
    )
    (tmp_path / "123" / "task" / "456" / "stat").write_text(
        _stat_line(456, "worker-thread", utime=12, stime=1, start=200)
    )
    second = collector.collect_once()
    threads = {thread.tid: thread for thread in second.threads}
    assert threads[123].cpu_percent == pytest.approx(20.0)
    assert threads[456].cpu_percent == pytest.approx(2.0)
    assert threads[456].tgid == 123
    assert threads[456].cpu_time_s == pytest.approx(0.13)

    # Reusing a TID with a new start time must discard its previous CPU baseline.
    (tmp_path / "stat").write_text(
        "cpu 300 0 50 2650 0 0 0 0\n"
        "cpu0 150 0 25 1325 0 0 0 0\n"
        "cpu1 150 0 25 1325 0 0 0 0\n"
    )
    (tmp_path / "123" / "task" / "456" / "stat").write_text(
        _stat_line(456, "replacement-thread", utime=1000, stime=100, start=300)
    )
    third = collector.collect_once()
    threads = {thread.tid: thread for thread in third.threads}
    assert threads[456].cpu_percent == 0.0
    assert threads[456].command == "replacement-thread"



def test_missing_task_directory_does_not_hide_process(
    tmp_path: Path,
) -> None:
    _fixture(tmp_path)
    (tmp_path / "123" / "task").rename(tmp_path / "123" / "task-disabled")
    collector = ProcessCollector(
        tmp_path, clock_ticks=100, page_size=4096,
        uid_lookup=lambda _uid: "tester",
    )
    collector.set_thread_target(123, 100)

    snapshot = collector.collect_once()

    assert [process.pid for process in snapshot.processes] == [123]
    assert snapshot.threads == ()


def test_thread_sampling_is_targeted_and_can_be_disabled(
    tmp_path: Path,
) -> None:
    _fixture(tmp_path)
    collector = ProcessCollector(
        tmp_path, clock_ticks=100, page_size=4096,
        uid_lookup=lambda _uid: "tester",
    )
    collector.set_thread_target(123, 100)
    assert {thread.tid for thread in collector.collect_once().threads} == {123, 456}

    collector.set_thread_target(None)
    assert collector.collect_once().threads == ()


@pytest.mark.parametrize("invalid_pid", [0, -1, True])
def test_thread_target_rejects_invalid_pid(invalid_pid: int) -> None:
    collector = ProcessCollector()
    with pytest.raises(ValueError, match="positive integer"):
        collector.set_thread_target(invalid_pid)



def test_thread_sampling_stops_when_target_pid_is_reused(
    tmp_path: Path,
) -> None:
    _fixture(tmp_path)
    collector = ProcessCollector(
        tmp_path, clock_ticks=100, page_size=4096,
        uid_lookup=lambda _uid: "tester",
    )
    collector.set_thread_target(123, 100)
    assert {thread.tid for thread in collector.collect_once().threads} == {123, 456}

    (tmp_path / "123" / "stat").write_text(
        _stat_line(123, "replacement process", utime=1, stime=1, start=999)
    )
    snapshot = collector.collect_once()

    assert snapshot.processes[0].start_ticks == 999
    assert snapshot.threads == ()


def test_thread_sampler_discards_snapshot_if_parent_pid_changes_mid_sample(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fixture(tmp_path)
    collector = ProcessCollector(
        tmp_path, clock_ticks=100, page_size=4096,
        uid_lookup=lambda _uid: "tester",
    )
    parent_stat = tmp_path / "123" / "stat"
    original_read_text = Path.read_text

    def replace_parent_after_task_read(path: Path, *args: object, **kwargs: object) -> str:
        if path == tmp_path / "123" / "task" / "456" / "stat":
            contents = original_read_text(path, *args, **kwargs)
            parent_stat.write_text(
                _stat_line(
                    123, "replacement process", utime=1, stime=1, start=999,
                )
            )
            return contents
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", replace_parent_after_task_read)
    threads = collector._sample_threads(
        123, 100, parse_cpu_times((tmp_path / "stat").read_text()), None,
    )

    assert threads == ()


def test_thread_target_validates_process_start_ticks() -> None:
    collector = ProcessCollector()
    with pytest.raises(ValueError, match="non-negative integer"):
        collector.set_thread_target(123, -1)
    with pytest.raises(ValueError, match="require a target PID"):
        collector.set_thread_target(None, 123)


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


def test_filter_matches_pid_user_command_and_cgroup() -> None:
    rows = (
        replace(_proc(10, 1, "alice", "python worker"), cgroup="/system.slice/docker-app.scope"),
        _proc(20, 10, "bob", "nginx"),
    )
    assert [p.pid for p in filter_processes(rows, "ALICE")] == [10]
    assert [p.pid for p in filter_processes(rows, "20")] == [20]
    assert [p.pid for p in filter_processes(rows, "PYTHON")] == [10]
    assert [p.pid for p in filter_processes(rows, "DOCKER-APP")] == [10]



def test_cgroup_sorting_keeps_missing_paths_last_and_supports_generators() -> None:
    rows = (
        replace(_proc(10, 1, "alice", "one"), cgroup="/system.slice/z.service"),
        replace(_proc(20, 1, "bob", "two"), cgroup="/system.slice/a.service"),
        replace(_proc(30, 1, "carol", "three"), cgroup=None),
    )

    assert [p.pid for p in sort_processes(rows, "CGROUP")] == [10, 20, 30]
    assert [p.pid for p in sort_processes(rows, "CGROUP", descending=False)] == [20, 10, 30]
    assert [p.pid for p in sort_processes(iter(rows), "CGROUP")] == [10, 20, 30]


def test_sort_processes_orders_by_selected_metric() -> None:
    rows = (
        _proc(10, 1, "a", "a", 12, 3),
        _proc(20, 1, "b", "b", 80, 1),
    )
    assert [p.pid for p in sort_processes(rows, "CPU")] == [20, 10]
    assert [p.pid for p in sort_processes(rows, "MEM", descending=False)] == [
        20, 10,
    ]
    io_rows = (
        replace(rows[0], io_read_rate=100.0),
        replace(rows[1], io_read_rate=500.0),
    )
    assert [p.pid for p in sort_processes(io_rows, "IO_READ")] == [20, 10]
    write_rows = tuple(
        replace(process, io_write_rate=process.pid * 10.0) for process in rows
    )
    assert [p.pid for p in sort_processes(write_rows, "IO_WRITE")] == [20, 10]
    assert [p.pid for p in sort_processes(tuple(replace(p, start_time_s=float(p.pid)) for p in rows), "START")] == [20, 10]
    with pytest.raises(ValueError):
        sort_processes(rows, "NOPE")



def test_f3_opens_scrollable_system_io_panel_and_returns_to_process_table() -> None:
    ui = ProcessMonitorUI()

    ui._handle_key(curses.KEY_F3)
    assert ui.system_io_mode is True
    assert ui.io_panel_scroll == 0
    assert "System I/O" in ui.status

    ui._handle_key(curses.KEY_DOWN)
    assert ui.io_panel_scroll == 1
    ui._handle_key(curses.KEY_NPAGE)
    assert ui.io_panel_scroll == 11
    ui._handle_key(curses.KEY_PPAGE)
    assert ui.io_panel_scroll == 1

    ui._handle_key(27)
    assert ui.system_io_mode is False
    assert ui.status == "Process table"




def test_f8_opens_affinity_editor_for_selected_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ui = ProcessMonitorUI()
    process = _proc(321, 1, "tester", "python worker")
    observed: list[tuple[int, int]] = []

    monkeypatch.setattr(ui, "_selected_process", lambda: process)
    monkeypatch.setattr(
        "process_input.ProcessAffinityEditor.edit",
        lambda _editor, _ui, selected: observed.append((selected.pid, selected.start_ticks)),
    )

    ui._handle_key(curses.KEY_F8)

    assert observed == [(321, process.start_ticks)]


def test_f8_affinity_requires_a_selected_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ui = ProcessMonitorUI()
    monkeypatch.setattr(ui, "_selected_process", lambda: None)
    monkeypatch.setattr(
        "process_input.ProcessAffinityEditor.edit",
        lambda *_args: pytest.fail("affinity editor opened without a process"),
    )

    ui._handle_key(curses.KEY_F8)

    assert ui.status == "Select a process before editing CPU affinity"


def test_f4_opens_selected_process_threads_and_disables_sampling_on_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ui = ProcessMonitorUI()
    process = _proc(321, 1, "tester", "python worker")
    threads = tuple(
        ThreadInfo(
            400 + index, 321, "R", 20, 0, float(index), float(index),
            f"worker-{index}", 1000 + index,
        )
        for index in range(12)
    )
    snapshot = ProcessSnapshot(processes=(process,), threads=threads)
    monkeypatch.setattr(ui.collector, "get_snapshot", lambda: snapshot)

    ui._handle_key(curses.KEY_F4)
    assert ui.thread_panel_mode is True
    assert ui.thread_target_pid == 321
    assert ui.thread_target_start_ticks == process.start_ticks
    assert ui.collector._thread_target_pid == 321
    assert ui.collector._thread_target_start_ticks == process.start_ticks
    assert "PID 321" in ui.status

    ui._handle_key(curses.KEY_DOWN)
    assert ui.thread_selected == 1
    ui._handle_key(curses.KEY_NPAGE)
    assert ui.thread_selected == 11

    ui._handle_key(27)
    assert ui.thread_panel_mode is False
    assert ui.thread_target_pid is None
    assert ui.thread_target_start_ticks is None
    assert ui.collector._thread_target_pid is None
    assert ui.status == "Process table"



def test_thread_panel_r_renices_selected_thread_after_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ui = ProcessMonitorUI()
    process = _proc(321, 1, "tester", "python worker")
    threads = (
        ThreadInfo(322, 321, "R", 20, 0, 80.0, 3.0, "busy-worker", 200),
        ThreadInfo(323, 321, "S", 20, 0, 10.0, 2.0, "idle-worker", 300),
    )
    monkeypatch.setattr(
        ui.collector, "get_snapshot",
        lambda: ProcessSnapshot(processes=(process,), threads=threads),
    )
    ui.thread_panel_mode = True
    ui.thread_target_pid = 321
    ui.thread_target_start_ticks = process.start_ticks
    answers = iter(("7", "y"))
    monkeypatch.setattr(ui, "_prompt", lambda _prompt: next(answers))
    calls: list[tuple[int, int, int, int, int]] = []
    monkeypatch.setattr(
        "process_input.renice_thread",
        lambda tgid, tid, parent_start, thread_start, nice: (
            calls.append((tgid, tid, parent_start, thread_start, nice)) or nice
        ),
    )

    ui._handle_key(ord("r"))

    assert calls == [(321, 322, process.start_ticks, 200, 7)]
    assert ui.status == "Reniced TID 322 to 7"


def test_thread_panel_thread_renice_declines_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ui = ProcessMonitorUI()
    process = _proc(321, 1, "tester", "python worker")
    thread = ThreadInfo(322, 321, "R", 20, 0, 80.0, 3.0, "busy-worker", 200)
    monkeypatch.setattr(
        ui.collector, "get_snapshot",
        lambda: ProcessSnapshot(processes=(process,), threads=(thread,)),
    )
    ui.thread_panel_mode = True
    ui.thread_target_pid = 321
    ui.thread_target_start_ticks = process.start_ticks
    answers = iter(("7", "n"))
    monkeypatch.setattr(ui, "_prompt", lambda _prompt: next(answers))
    monkeypatch.setattr(
        "process_input.renice_thread",
        lambda *_args: pytest.fail("declined thread renice reached OS action"),
    )

    ui._handle_key(ord("r"))

    assert ui.status == "Renice cancelled for TID 322"


def test_thread_selection_loss_persists_until_navigation_acknowledges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ui = ProcessMonitorUI()
    ui.thread_panel_mode = True
    ui.thread_target_pid = 321
    ui.thread_target_start_ticks = 100
    ui.thread_selected_tid = 322
    ui.thread_selected_start_ticks = 200
    replacement = ThreadInfo(
        322, 321, "R", 20, 0, 80.0, 3.0, "replacement-thread", 201,
    )
    snapshot = ProcessSnapshot(threads=(replacement,))
    monkeypatch.setattr(ui.collector, "get_snapshot", lambda: snapshot)

    ui._thread_rows(snapshot)  # Renderer observes the reused TID.
    assert ui.thread_selection_lost is True
    ui._handle_key(ord("r"))  # Input refreshes again in the same UI frame.
    assert ui.thread_selection_lost is True
    assert "review the new selection before renicing" in ui.status

    ui._handle_key(curses.KEY_DOWN)
    assert ui.thread_selection_lost is False
    assert ui.thread_selected_start_ticks == 201


def test_thread_renice_blocks_reused_tid_until_user_reviews_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ui = ProcessMonitorUI()
    ui.thread_panel_mode = True
    ui.thread_target_pid = 321
    ui.thread_target_start_ticks = 100
    ui.thread_selected_tid = 322
    ui.thread_selected_start_ticks = 200
    replacement = ThreadInfo(
        322, 321, "R", 20, 0, 80.0, 3.0, "replacement-thread", 201,
    )
    monkeypatch.setattr(
        ui.collector, "get_snapshot",
        lambda: ProcessSnapshot(threads=(replacement,)),
    )
    monkeypatch.setattr(
        ui, "_prompt",
        lambda _prompt: pytest.fail("renice prompt shown for a reused TID"),
    )
    monkeypatch.setattr(
        "process_input.renice_thread",
        lambda *_args: pytest.fail("reused TID reached renice action"),
    )

    ui._handle_key(ord("r"))

    assert ui.thread_selected_start_ticks == 201
    assert ui.thread_selection_lost is True
    assert "review the new selection before renicing" in ui.status


def test_f4_requires_a_selected_process() -> None:
    ui = ProcessMonitorUI()

    ui._handle_key(curses.KEY_F4)

    assert ui.thread_panel_mode is False
    assert ui.status == "Select a process before opening its threads"


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
        patch("process_actions.os.getpid", return_value=999),
        patch("process_actions.os.kill") as kill,
    ):
        signal_process(123, int(signal.SIGTERM))
        kill.assert_called_once_with(123, signal.SIGTERM)
    with (
        patch("process_actions.os.getpid", return_value=999),
        patch("process_actions.os.setpriority") as setpriority,
    ):
        renice_process(123, 5)
        setpriority.assert_called_once_with(os.PRIO_PROCESS, 123, 5)


def test_invalid_actions_never_reach_operating_system() -> None:
    with (
        patch("process_actions.os.getpid", return_value=999),
        patch("process_actions.os.kill") as kill,
        patch("process_actions.os.setpriority") as setpriority,
    ):
        with pytest.raises(ValueError):
            signal_process(1, int(signal.SIGTERM))
        with pytest.raises(ValueError, match="unsupported process signal"):
            signal_process(123, int(signal.SIGCONT))
        with pytest.raises(ValueError):
            renice_process(123, 20)
        kill.assert_not_called()
        setpriority.assert_not_called()


def test_ui_delegates_snapshot_rendering_to_renderer(monkeypatch: pytest.MonkeyPatch) -> None:
    ui = ProcessMonitorUI()
    snapshot = ProcessSnapshot(processes=())
    calls: list[tuple[object, ProcessSnapshot]] = []
    monkeypatch.setattr(ui.renderer, "draw", lambda owner, value: calls.append((owner, value)))

    ui._draw(snapshot)

    assert calls == [(ui, snapshot)]


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
    assert ProcessMonitorUI._fmt_rate(None) == "—"
    assert ProcessMonitorUI._fmt_rate(1024.0) == "1.0K/s"
    assert ProcessMonitorUI._fmt_rate(1023.0 * 1024.0) == "1023K/s"


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
        "process_input.signal_process",
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
        "process_input.signal_process",
        lambda pid, signum: signal_calls.append((pid, signum)),
    )

    ui._handle_key(ord("K"))

    assert signal_calls == [(321, int(signal.SIGKILL))]
    assert ui.status == "Sent KILL to PID 321"



def test_tree_view_can_collapse_and_expand_a_subtree() -> None:
    rows = (
        _proc(1, 0, "root", "init"),
        _proc(2, 1, "root", "parent"),
        _proc(3, 2, "root", "child"),
        _proc(4, 1, "root", "sibling"),
    )
    expanded = tree_processes(rows, "PID", descending=False)
    collapsed = tree_processes(rows, "PID", descending=False, collapsed={2})
    assert [(process.pid, depth) for process, depth in expanded] == [
        (1, 0), (2, 1), (3, 2), (4, 1),
    ]
    assert [(process.pid, depth) for process, depth in collapsed] == [
        (1, 0), (2, 1), (4, 1),
    ]


def test_column_parser_preserves_order_and_accepts_display_aliases() -> None:
    assert parse_columns("command, pid, CPU%, S, TIME+") == (
        "COMMAND", "PID", "CPU", "STATE", "TIME",
    )
    assert DEFAULT_COLUMNS[-1] == "COMMAND"
    assert parse_columns("IOR/s,IOW/s") == ("IO_READ", "IO_WRITE")
    with pytest.raises(argparse.ArgumentTypeError, match="unsupported column"):
        parse_columns("PID,NOT_A_COLUMN")
    with pytest.raises(argparse.ArgumentTypeError, match="duplicates"):
        parse_columns("PID,pid")
    with pytest.raises(argparse.ArgumentTypeError, match="empty"):
        parse_columns("PID,,CPU")


def test_refresh_interval_parser_rejects_non_finite_and_too_fast_values() -> None:
    assert parse_refresh_interval("0.25") == pytest.approx(0.25)
    with pytest.raises(argparse.ArgumentTypeError, match="at least 0.05"):
        parse_refresh_interval("0.01")
    with pytest.raises(argparse.ArgumentTypeError, match="finite"):
        parse_refresh_interval("nan")


def test_ui_accepts_custom_columns_and_refresh_interval() -> None:
    ui = ProcessMonitorUI(refresh_interval=0.25, columns=("PID", "COMMAND", "CPU"))
    assert ui.refresh_interval == pytest.approx(0.25)
    assert ui.collector.poll_interval == pytest.approx(0.25)
    assert ui.columns == ("PID", "COMMAND", "CPU")


def test_tree_keyboard_controls_collapse_expand_and_toggle() -> None:
    processes = (
        _proc(10, 1, "tester", "parent"),
        _proc(11, 10, "tester", "child"),
    )
    snapshot = ProcessSnapshot(processes=processes)

    class FakeCollector:
        def get_snapshot(self) -> ProcessSnapshot:
            return snapshot

    ui = ProcessMonitorUI()
    ui.collector = FakeCollector()  # type: ignore[assignment]
    ui.tree_mode = True
    ui._handle_key(curses.KEY_LEFT)
    assert ui.collapsed == {10}
    assert [(item.pid, depth) for item, depth in ui._visible(snapshot)] == [(10, 0)]
    ui._handle_key(curses.KEY_RIGHT)
    assert ui.collapsed == set()
    assert len(ui._visible(snapshot)) == 2
    ui._handle_key(ord(" "))
    assert ui.collapsed == {10}


def test_ui_delegates_keyboard_dispatch_to_input_controller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ui = ProcessMonitorUI()
    calls: list[tuple[object, int]] = []
    monkeypatch.setattr(
        ui.input, "handle_key", lambda owner, key: calls.append((owner, key)),
    )

    ui._handle_key(ord("j"))

    assert calls == [(ui, ord("j"))]


def test_input_controller_owns_prompt_and_tree_expansion() -> None:
    from process_input import ProcessInput

    assert callable(ProcessInput.prompt)
    assert callable(ProcessInput.set_selected_tree_expansion)
    assert callable(ProcessInput.handle_key)


def test_input_controller_navigation_sort_and_filter() -> None:
    snapshot = ProcessSnapshot(processes=(
        _proc(10, 1, "tester", "worker-a", cpu=10),
        _proc(20, 1, "tester", "worker-b", cpu=20),
    ))

    class FakeCollector:
        def get_snapshot(self) -> ProcessSnapshot:
            return snapshot

    ui = ProcessMonitorUI()
    ui.collector = FakeCollector()  # type: ignore[assignment]
    ui.input.handle_key(ui, curses.KEY_DOWN)
    assert ui.selected == 1
    ui.input.handle_key(ui, curses.KEY_HOME)
    assert ui.selected == 0
    ui.input.handle_key(ui, curses.KEY_END)
    assert ui.selected == 1
    ui.input.handle_key(ui, curses.KEY_F6)
    assert ui.descending is False
    ui.input.handle_key(ui, curses.KEY_F6)
    assert ui.sort_field == "MEM"
    ui._prompt = lambda _prompt: "worker-b"  # type: ignore[method-assign]
    ui.input.handle_key(ui, ord("/"))
    assert ui.filter_query == "worker-b"
    assert ui.selected == ui.scroll == 0
    ui.input.handle_key(ui, 27)
    assert ui.filter_query == ""


def test_input_prompt_restores_echo_and_cursor_after_read_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from process_input import ProcessInput

    class FakeScreen:
        def getmaxyx(self) -> tuple[int, int]:
            return (10, 80)

        def addnstr(self, *_args: object) -> None:
            pass

        def refresh(self) -> None:
            pass

        def getstr(self, *_args: int) -> bytes:
            raise curses.error("input interrupted")

    ui = ProcessMonitorUI()
    ui._stdscr = FakeScreen()
    cursor_calls: list[int] = []
    echo_calls: list[str] = []
    monkeypatch.setattr("process_input.curses.echo", lambda: echo_calls.append("echo"))
    monkeypatch.setattr("process_input.curses.noecho", lambda: echo_calls.append("noecho"))
    monkeypatch.setattr("process_input.curses.curs_set", lambda value: cursor_calls.append(value))

    with pytest.raises(curses.error, match="input interrupted"):
        ProcessInput().prompt(ui, "Filter: ")

    assert echo_calls == ["echo", "noecho"]
    assert cursor_calls == [1, 0]


def test_invalid_signal_selection_never_calls_signal_syscall(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = ProcessSnapshot(processes=(_proc(321, 1, "tester", "worker"),))

    class FakeCollector:
        def get_snapshot(self) -> ProcessSnapshot:
            return snapshot

    ui = ProcessMonitorUI()
    ui.collector = FakeCollector()  # type: ignore[assignment]
    ui._prompt = lambda _prompt: "9"  # type: ignore[method-assign]
    monkeypatch.setattr(
        "process_input.signal_process",
        lambda *_args: pytest.fail("invalid signal selection reached syscall"),
    )

    ui.input.handle_key(ui, ord("K"))

    assert ui.status.startswith("Signal failed:")


def test_input_controller_scrolls_process_table_horizontally() -> None:
    ui = ProcessMonitorUI()

    ui.input.handle_key(ui, ord("]"))
    assert ui.horizontal_scroll == 8
    assert ui.status == "Process table shifted right"

    ui.input.handle_key(ui, ord("["))
    assert ui.horizontal_scroll == 0
    assert ui.status == "Process table shifted left"

    ui.input.handle_key(ui, ord("["))
    assert ui.horizontal_scroll == 0



def test_tree_filter_retains_ancestors_but_not_unrelated_processes() -> None:
    rows = (
        _proc(1, 0, "root", "init"),
        _proc(2, 1, "root", "service-manager"),
        _proc(3, 2, "alice", "needle-worker"),
        _proc(4, 1, "bob", "unrelated-sibling"),
        _proc(5, 3, "alice", "unrelated-child"),
    )

    visible = filter_tree_context(rows, "NEEDLE")

    assert [process.pid for process in visible] == [1, 2, 3]


def test_tree_filter_handles_parent_cycles_and_empty_queries() -> None:
    rows = (
        _proc(10, 11, "alice", "needle"),
        _proc(11, 10, "root", "ancestor"),
        _proc(12, 99, "root", "unrelated"),
    )

    assert [process.pid for process in filter_tree_context(rows, "needle")] == [
        10, 11,
    ]
    assert filter_tree_context(rows, "") == rows
    assert filter_tree_context(rows, "not-found") == ()


def test_tree_mode_displays_matching_process_with_ancestor_path() -> None:
    snapshot = ProcessSnapshot(processes=(
        _proc(1, 0, "root", "init"),
        _proc(2, 1, "root", "supervisor"),
        _proc(3, 2, "alice", "needle-worker"),
        _proc(4, 1, "bob", "unrelated"),
    ))
    ui = ProcessMonitorUI()
    ui.tree_mode = True
    ui.filter_query = "needle"

    rows = ui._visible(snapshot)

    assert [(process.pid, depth) for process, depth in rows] == [
        (1, 0), (2, 1), (3, 2),
    ]

def test_thread_selection_tracks_tid_when_cpu_order_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ui = ProcessMonitorUI()
    process = _proc(321, 1, "tester", "python worker")
    first = ProcessSnapshot(processes=(process,), threads=(
        ThreadInfo(322, 321, "R", 20, 0, 80.0, 3.0, "busy-worker", 200),
        ThreadInfo(323, 321, "S", 20, 0, 10.0, 2.0, "idle-worker", 300),
    ))
    reordered = ProcessSnapshot(processes=(process,), threads=(
        ThreadInfo(322, 321, "R", 20, 0, 5.0, 3.0, "busy-worker", 200),
        ThreadInfo(323, 321, "S", 20, 0, 95.0, 2.0, "idle-worker", 300),
    ))
    snapshots = [first, reordered]
    monkeypatch.setattr(ui.collector, "get_snapshot", lambda: snapshots[0])
    ui.thread_panel_mode = True
    ui.thread_target_pid = 321
    ui.thread_target_start_ticks = process.start_ticks

    ui._handle_key(curses.KEY_DOWN)
    assert ui.thread_selected_tid == 323

    snapshots[0] = reordered
    answers = iter(("8", "y"))
    monkeypatch.setattr(ui, "_prompt", lambda _prompt: next(answers))
    calls: list[tuple[int, int, int, int, int]] = []
    monkeypatch.setattr(
        "process_input.renice_thread",
        lambda tgid, tid, parent_start, thread_start, nice: (
            calls.append((tgid, tid, parent_start, thread_start, nice)) or nice
        ),
    )

    ui._handle_key(ord("r"))

    assert ui.thread_selected == 0
    assert ui.thread_selected_tid == 323
    assert calls == [(321, 323, process.start_ticks, 300, 8)]
    assert ui.status == "Reniced TID 323 to 8"


def test_thread_selection_detects_reused_tid_even_when_numeric_tid_matches() -> None:
    ui = ProcessMonitorUI()
    ui.thread_selected = 0
    ui.thread_selected_tid = 322
    ui.thread_selected_start_ticks = 200

    rows = ui._thread_rows(ProcessSnapshot(threads=(
        ThreadInfo(322, 321, "R", 20, 0, 50.0, 1.0, "replacement", 201),
    )))

    assert [thread.tid for thread in rows] == [322]
    assert ui.thread_selected_tid == 322
    assert ui.thread_selected_start_ticks == 201
    assert ui.thread_selection_lost is True
    assert "TID was reused" in ui.status


def test_empty_thread_sample_preserves_selection_identity_for_reuse_detection() -> None:
    ui = ProcessMonitorUI()
    ui.thread_selected_tid = 322
    ui.thread_selected_start_ticks = 200

    assert ui._thread_rows(ProcessSnapshot()) == ()
    assert ui.thread_selected_tid == 322
    assert ui.thread_selected_start_ticks == 200

    ui._thread_rows(ProcessSnapshot(threads=(
        ThreadInfo(322, 321, "R", 20, 0, 50.0, 1.0, "replacement", 201),
    )))

    assert ui.thread_selection_lost is True
    assert ui.thread_selected_start_ticks == 201


def test_thread_selection_recovers_when_selected_tid_exits() -> None:
    ui = ProcessMonitorUI()
    ui.thread_selected = 0
    ui.thread_selected_tid = 999

    rows = ui._thread_rows(ProcessSnapshot(threads=(
        ThreadInfo(322, 321, "R", 20, 0, 50.0, 1.0, "replacement", 200),
    )))

    assert [thread.tid for thread in rows] == [322]
    assert ui.thread_selected == 0
    assert ui.thread_selected_tid == 322
    assert "Selected thread exited" in ui.status
