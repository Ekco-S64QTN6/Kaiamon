"""Linux /proc collection, parsing, and immutable process snapshot models.

This module is independent of curses so it can be tested with synthetic
procfs fixtures and reused by alternate presentation layers.
"""
from __future__ import annotations

import os
import pwd
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, TypedDict

POLL_INTERVAL = 1.0


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    ppid: int
    user: str
    priority: int
    nice: int
    virt_kb: int
    res_kb: int
    shr_kb: int
    state: str
    cpu_percent: float
    mem_percent: float
    cpu_time_s: float
    command: str
    start_ticks: int
    io_read_rate: float | None = None
    io_write_rate: float | None = None
    start_time_s: float | None = None
    cgroup: str | None = None


@dataclass(frozen=True)
class ThreadInfo:
    """One kernel thread sampled from /proc/<tgid>/task/<tid>."""

    tid: int
    tgid: int
    state: str
    priority: int
    nice: int
    cpu_percent: float
    cpu_time_s: float
    command: str
    start_ticks: int


@dataclass(frozen=True)
class DiskIORate:
    device: str
    read_bytes_s: float | None = None
    write_bytes_s: float | None = None


@dataclass(frozen=True)
class NetworkIORate:
    interface: str
    receive_bytes_s: float | None = None
    transmit_bytes_s: float | None = None


@dataclass(frozen=True)
class ProcessSnapshot:
    processes: tuple[ProcessInfo, ...] = ()
    cpu_percent: float = 0.0
    per_cpu_percent: tuple[float, ...] = ()
    memory_total_kb: int = 0
    memory_used_kb: int = 0
    sampled_at: float = 0.0
    disk_io: tuple[DiskIORate, ...] = ()
    network_io: tuple[NetworkIORate, ...] = ()
    threads: tuple[ThreadInfo, ...] = ()


@dataclass(frozen=True)
class CpuTimes:
    total: int
    idle: int


class ProcStat(TypedDict):
    pid: int
    command: str
    state: str
    ppid: int
    utime: int
    stime: int
    priority: int
    nice: int
    start_ticks: int
    virt_bytes: int
    rss_pages: int


def parse_proc_stat(contents: str) -> ProcStat:
    """Parse /proc/<pid>/stat, allowing spaces and parentheses in comm."""
    left = contents.find("(")
    right = contents.rfind(")")
    if left <= 0 or right <= left:
        raise ValueError("malformed process stat")
    pid = int(contents[:left].strip())
    command = contents[left + 1:right]
    fields = contents[right + 1:].split()  # starts with field 3 (state)
    if len(fields) < 22:
        raise ValueError("truncated process stat")
    return {
        "pid": pid,
        "command": command,
        "state": fields[0],
        "ppid": int(fields[1]),
        "utime": int(fields[11]),
        "stime": int(fields[12]),
        "priority": int(fields[15]),
        "nice": int(fields[16]),
        "start_ticks": int(fields[19]),
        "virt_bytes": int(fields[20]),
        "rss_pages": int(fields[21]),
    }


@dataclass(frozen=True)
class DiskCounters:
    major: int
    minor: int
    device: str
    read_bytes: int
    write_bytes: int


@dataclass(frozen=True)
class NetworkCounters:
    interface: str
    receive_bytes: int
    transmit_bytes: int


def parse_diskstats(contents: str) -> tuple[DiskCounters, ...]:
    """Parse per-device sector counters from /proc/diskstats (512 bytes/sector)."""
    devices: list[DiskCounters] = []
    for line in contents.splitlines():
        parts = line.split()
        if not parts:
            continue
        if len(parts) < 10:
            raise ValueError("truncated /proc/diskstats line")
        try:
            major, minor = int(parts[0]), int(parts[1])
            read_sectors, write_sectors = int(parts[5]), int(parts[9])
        except ValueError as exc:
            raise ValueError("invalid /proc/diskstats counter") from exc
        if min(major, minor, read_sectors, write_sectors) < 0:
            raise ValueError("negative /proc/diskstats counter")
        devices.append(DiskCounters(
            major, minor, parts[2], read_sectors * 512, write_sectors * 512,
        ))
    return tuple(devices)


def parse_network_dev(contents: str) -> tuple[NetworkCounters, ...]:
    """Parse receive/transmit byte counters from /proc/net/dev."""
    interfaces: list[NetworkCounters] = []
    for line in contents.splitlines():
        interface_text, separator, counters_text = line.partition(":")
        if not separator:
            continue
        interface = interface_text.strip()
        if not interface:
            continue
        values = counters_text.split()
        if not values or not values[0].isdigit():
            # Skip the two textual column-header lines.
            continue
        if len(values) < 9:
            raise ValueError("truncated /proc/net/dev line")
        try:
            receive_bytes, transmit_bytes = int(values[0]), int(values[8])
        except ValueError as exc:
            raise ValueError(f"invalid /proc/net/dev counters for {interface}") from exc
        if receive_bytes < 0 or transmit_bytes < 0:
            raise ValueError(f"negative /proc/net/dev counters for {interface}")
        interfaces.append(NetworkCounters(interface, receive_bytes, transmit_bytes))
    return tuple(interfaces)



def parse_proc_cgroup(contents: str) -> str | None:
    """Return the most useful process cgroup path, preferring unified v2."""
    paths: list[str] = []
    for line in contents.splitlines():
        hierarchy, separator, remainder = line.partition(":")
        if not separator or not hierarchy.isdigit():
            continue
        controllers, separator, raw_path = remainder.partition(":")
        if not separator:
            continue
        path = raw_path.strip()
        if not path.startswith("/"):
            continue
        if hierarchy == "0" and not controllers:
            return path
        paths.append(path)
    if not paths:
        return None
    # v1 exposes one line per controller; choose the deepest distinct path.
    return max(paths, key=lambda path: (len(path), path))


def parse_proc_io(contents: str) -> tuple[int, int]:
    """Parse cumulative read_bytes/write_bytes counters from /proc/<pid>/io."""
    counters: dict[str, int] = {}
    for line in contents.splitlines():
        key, separator, raw_value = line.partition(":")
        if not separator or key.strip() not in {"read_bytes", "write_bytes"}:
            continue
        try:
            value = int(raw_value.strip())
        except ValueError as exc:
            raise ValueError(f"invalid {key.strip()} counter") from exc
        if value < 0:
            raise ValueError(f"negative {key.strip()} counter")
        counters[key.strip()] = value
    missing = {"read_bytes", "write_bytes"} - counters.keys()
    if missing:
        raise ValueError("missing /proc/<pid>/io counters: " + ", ".join(sorted(missing)))
    return counters["read_bytes"], counters["write_bytes"]


def parse_cpu_times(contents: str) -> tuple[CpuTimes, ...]:
    """Read aggregate and per-core CPU counters from /proc/stat."""
    parsed: list[CpuTimes] = []
    for line in contents.splitlines():
        parts = line.split()
        if not parts or not (
            parts[0] == "cpu"
            or (parts[0].startswith("cpu") and parts[0][3:].isdigit())
        ):
            continue
        values = [int(value) for value in parts[1:]]
        if len(values) < 4:
            continue
        # guest and guest_nice are already included in user and nice.
        total = sum(values[:8])
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        parsed.append(CpuTimes(total=total, idle=idle))
    if not parsed:
        raise ValueError("/proc/stat did not contain CPU counters")
    return tuple(parsed)



def parse_boot_time(contents: str) -> int | None:
    """Return the system boot epoch in seconds from /proc/stat's btime field."""
    for line in contents.splitlines():
        key, separator, raw_value = line.partition(" ")
        if key != "btime":
            continue
        if not separator:
            raise ValueError("malformed /proc/stat boot time")
        try:
            boot_time = int(raw_value.strip())
        except ValueError as exc:
            raise ValueError("invalid /proc/stat boot time") from exc
        if boot_time < 0:
            raise ValueError("negative /proc/stat boot time")
        return boot_time
    return None


def cpu_usage(previous: CpuTimes, current: CpuTimes) -> float:
    total_delta = current.total - previous.total
    idle_delta = current.idle - previous.idle
    if total_delta <= 0:
        return 0.0
    busy = max(0, total_delta - max(0, idle_delta))
    return min(100.0, (busy / total_delta) * 100.0)


class ProcessCollector:
    """Poll /proc in a worker thread and publish immutable process snapshots."""

    def __init__(
        self,
        proc_root: str | os.PathLike[str] = "/proc",
        poll_interval: float = POLL_INTERVAL,
        clock_ticks: int | None = None,
        page_size: int | None = None,
        uid_lookup: Callable[[int], str] | None = None,
        monotonic_clock: Callable[[], float] | None = None,
    ) -> None:
        self.proc_root = Path(proc_root)
        self.poll_interval = max(0.05, float(poll_interval))
        self.clock_ticks = clock_ticks or int(os.sysconf("SC_CLK_TCK"))
        self.page_size = page_size or int(os.sysconf("SC_PAGE_SIZE"))
        self._uid_lookup = uid_lookup or self._username
        self._monotonic_clock = monotonic_clock or time.monotonic
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._snapshot = ProcessSnapshot()
        self._previous_process: dict[int, tuple[int, int]] = {}
        self._previous_io: dict[int, tuple[int, int, int, float]] = {}
        self._previous_disk: dict[tuple[int, int], tuple[int, int, float]] = {}
        self._previous_network: dict[str, tuple[int, int, float]] = {}
        self._thread_target_pid: int | None = None
        self._thread_target_start_ticks: int | None = None
        self._previous_thread_target_pid: int | None = None
        self._previous_thread_target_start_ticks: int | None = None
        self._previous_threads: dict[int, tuple[int, int]] = {}
        self._previous_cpu: tuple[CpuTimes, ...] | None = None

    @staticmethod
    def _username(uid: int) -> str:
        try:
            return pwd.getpwuid(uid).pw_name
        except (KeyError, OverflowError):
            return str(uid)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="process-collector", daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=max(1.0, self.poll_interval * 2))

    def get_snapshot(self) -> ProcessSnapshot:
        with self._lock:
            return self._snapshot

    def set_thread_target(
        self, pid: int | None, start_ticks: int | None = None,
    ) -> None:
        """Collect threads for one PID identity, or disable thread sampling."""
        if pid is None:
            if start_ticks is not None:
                raise ValueError("thread start ticks require a target PID")
        elif isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            raise ValueError("thread target PID must be a positive integer")
        if start_ticks is not None and (
            isinstance(start_ticks, bool)
            or not isinstance(start_ticks, int)
            or start_ticks < 0
        ):
            raise ValueError("thread target start ticks must be a non-negative integer")
        # Read/write the pair under the snapshot lock so the collector cannot
        # combine a new PID with an old start-time identity during UI changes.
        with self._lock:
            self._thread_target_pid = pid
            self._thread_target_start_ticks = start_ticks

    def collect_once(self) -> ProcessSnapshot:
        """Collect one sample; public to make fixture-based tests deterministic."""
        sample_monotonic = self._monotonic_clock()
        with self._lock:
            thread_target_pid = self._thread_target_pid
            thread_target_start_ticks = self._thread_target_start_ticks
        proc_stat_contents = (self.proc_root / "stat").read_text()
        cpu_samples = parse_cpu_times(proc_stat_contents)
        try:
            boot_time_s = parse_boot_time(proc_stat_contents)
        except ValueError:
            boot_time_s = None
        mem_total, mem_used = self._read_memory()
        previous_cpu = self._previous_cpu
        core_values: list[float] = []
        if previous_cpu is not None:
            for index, current in enumerate(cpu_samples[1:], start=1):
                if index < len(previous_cpu):
                    core_values.append(cpu_usage(previous_cpu[index], current))
                else:
                    core_values.append(0.0)
        else:
            core_values = [0.0] * max(0, len(cpu_samples) - 1)
        self._previous_cpu = cpu_samples
        overall_cpu = (
            sum(core_values) / len(core_values) if core_values else 0.0
        )
        disk_io = self._sample_disk_io(sample_monotonic)
        network_io = self._sample_network_io(sample_monotonic)

        processes: list[ProcessInfo] = []
        current_previous: dict[int, tuple[int, int]] = {}
        current_io: dict[int, tuple[int, int, int, float]] = {}
        for entry in self.proc_root.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                raw_stat = parse_proc_stat((entry / "stat").read_text())
                pid = int(raw_stat["pid"])
                proc_ticks = int(raw_stat["utime"]) + int(raw_stat["stime"])
                start_ticks = int(raw_stat["start_ticks"])
                previous = self._previous_process.get(pid)
                cpu_pct = 0.0
                if (
                    previous
                    and previous[1] == start_ticks
                    and previous_cpu is not None
                ):
                    total_delta = cpu_samples[0].total - previous_cpu[0].total
                    proc_delta = proc_ticks - previous[0]
                    if total_delta > 0 and proc_delta >= 0:
                        cpu_pct = (
                            proc_delta / total_delta
                        ) * max(1, len(cpu_samples) - 1) * 100.0
                current_previous[pid] = (proc_ticks, start_ticks)
                io_read_rate: float | None = None
                io_write_rate: float | None = None
                try:
                    io_read_bytes, io_write_bytes = parse_proc_io(
                        (entry / "io").read_text()
                    )
                except (OSError, ValueError):
                    # I/O counters may be hidden by procfs permissions or unavailable.
                    pass
                else:
                    previous_io = self._previous_io.get(pid)
                    if previous_io and previous_io[0] == start_ticks:
                        elapsed = sample_monotonic - previous_io[3]
                        if elapsed > 0:
                            io_read_rate = max(0, io_read_bytes - previous_io[1]) / elapsed
                            io_write_rate = max(0, io_write_bytes - previous_io[2]) / elapsed
                    current_io[pid] = (
                        start_ticks, io_read_bytes, io_write_bytes, sample_monotonic,
                    )
                status = self._read_status(entry / "status")
                uid_fields = status.get("Uid", "0").split()
                uid_text = uid_fields[1] if len(uid_fields) > 1 else uid_fields[0]
                uid = int(uid_text)
                statm = (entry / "statm").read_text().split()
                if len(statm) < 3:
                    raise ValueError("truncated statm")
                virt_kb = int(raw_stat["virt_bytes"]) // 1024
                res_kb = int(statm[1]) * self.page_size // 1024
                shr_kb = int(statm[2]) * self.page_size // 1024
                cmdline_raw = (entry / "cmdline").read_bytes()
                command = " ".join(
                    part.decode("utf-8", errors="replace")
                    for part in cmdline_raw.split(b"\0") if part
                )
                if not command:
                    command = str(raw_stat["command"])
                mem_pct = (
                    res_kb / mem_total * 100.0 if mem_total > 0 else 0.0
                )
                try:
                    cgroup = parse_proc_cgroup((entry / "cgroup").read_text())
                except OSError:
                    cgroup = None

                processes.append(ProcessInfo(
                    pid=pid,
                    ppid=int(raw_stat["ppid"]),
                    user=self._uid_lookup(uid),
                    priority=int(raw_stat["priority"]),
                    nice=int(raw_stat["nice"]),
                    virt_kb=virt_kb,
                    res_kb=res_kb,
                    shr_kb=shr_kb,
                    state=str(raw_stat["state"]),
                    cpu_percent=max(0.0, cpu_pct),
                    mem_percent=max(0.0, mem_pct),
                    cpu_time_s=proc_ticks / self.clock_ticks,
                    command=command,
                    start_ticks=start_ticks,
                    io_read_rate=io_read_rate,
                    io_write_rate=io_write_rate,
                    start_time_s=(
                        boot_time_s + start_ticks / self.clock_ticks
                        if boot_time_s is not None else None
                    ),
                    cgroup=cgroup,
                ))
            except (OSError, ValueError, IndexError, KeyError, OverflowError):
                # Processes routinely exit or become inaccessible while sampled.
                continue
        self._previous_process = current_previous
        self._previous_io = current_io
        target_process = next(
            (
                process for process in processes
                if process.pid == thread_target_pid
                and (
                    thread_target_start_ticks is None
                    or process.start_ticks == thread_target_start_ticks
                )
            ),
            None,
        )
        threads = (
            self._sample_threads(
                thread_target_pid, thread_target_start_ticks,
                cpu_samples, previous_cpu,
            )
            if target_process is not None and thread_target_pid is not None
            else self._sample_threads(None, None, cpu_samples, previous_cpu)
        )
        snapshot = ProcessSnapshot(
            processes=tuple(processes),
            cpu_percent=overall_cpu,
            per_cpu_percent=tuple(core_values),
            memory_total_kb=mem_total,
            memory_used_kb=mem_used,
            sampled_at=time.time(),
            disk_io=disk_io,
            network_io=network_io,
            threads=threads,
        )
        with self._lock:
            self._snapshot = snapshot
        return snapshot

    def _sample_threads(
        self,
        target_pid: int | None,
        target_start_ticks: int | None,
        cpu_samples: tuple[CpuTimes, ...],
        previous_cpu: tuple[CpuTimes, ...] | None,
    ) -> tuple[ThreadInfo, ...]:
        """Collect thread counters for only the requested process identity."""
        if (
            target_pid != self._previous_thread_target_pid
            or target_start_ticks != self._previous_thread_target_start_ticks
        ):
            self._previous_threads = {}
            self._previous_thread_target_pid = target_pid
            self._previous_thread_target_start_ticks = target_start_ticks
        if target_pid is None:
            self._previous_threads = {}
            return ()

        try:
            task_entries = sorted(
                (self.proc_root / str(target_pid) / "task").iterdir(),
                key=lambda entry: int(entry.name) if entry.name.isdigit() else -1,
            )
        except OSError:
            self._previous_threads = {}
            return ()

        total_delta = (
            cpu_samples[0].total - previous_cpu[0].total
            if previous_cpu is not None else 0
        )
        threads: list[ThreadInfo] = []
        current_threads: dict[int, tuple[int, int]] = {}
        for task in task_entries:
            if not task.name.isdigit():
                continue
            try:
                raw = parse_proc_stat((task / "stat").read_text())
                tid = int(raw["pid"])
                ticks = int(raw["utime"]) + int(raw["stime"])
                start_ticks = int(raw["start_ticks"])
                previous = self._previous_threads.get(tid)
                cpu_pct = 0.0
                if (
                    previous is not None
                    and previous[1] == start_ticks
                    and total_delta > 0
                    and ticks >= previous[0]
                ):
                    cpu_pct = (
                        (ticks - previous[0]) / total_delta
                    ) * max(1, len(cpu_samples) - 1) * 100.0
                current_threads[tid] = (ticks, start_ticks)
                threads.append(ThreadInfo(
                    tid=tid,
                    tgid=target_pid,
                    state=str(raw["state"]),
                    priority=int(raw["priority"]),
                    nice=int(raw["nice"]),
                    cpu_percent=max(0.0, cpu_pct),
                    cpu_time_s=ticks / self.clock_ticks,
                    command=str(raw["command"]),
                    start_ticks=start_ticks,
                ))
            except (OSError, ValueError, IndexError, KeyError, OverflowError):
                # Threads can exit or become inaccessible while sampled.
                continue

        # The owner may exit and its PID may be reused while /task is read.
        # Discard the sample unless the process identity still matches.
        if target_start_ticks is not None:
            try:
                current_process = parse_proc_stat(
                    (self.proc_root / str(target_pid) / "stat").read_text()
                )
            except (OSError, ValueError, IndexError, KeyError, OverflowError):
                self._previous_threads = {}
                return ()
            if (
                current_process["pid"] != target_pid
                or current_process["start_ticks"] != target_start_ticks
            ):
                self._previous_threads = {}
                return ()

        self._previous_threads = current_threads
        return tuple(threads)

    def _sample_disk_io(self, sampled_at: float) -> tuple[DiskIORate, ...]:
        """Calculate per-device disk throughput from /proc/diskstats."""
        try:
            counters = parse_diskstats((self.proc_root / "diskstats").read_text())
        except (OSError, ValueError):
            self._previous_disk = {}
            return ()
        current: dict[tuple[int, int], tuple[int, int, float]] = {}
        rates: list[DiskIORate] = []
        for device in counters:
            key = (device.major, device.minor)
            previous = self._previous_disk.get(key)
            read_rate: float | None = None
            write_rate: float | None = None
            if previous is not None:
                elapsed = sampled_at - previous[2]
                if elapsed > 0:
                    read_rate = max(0, device.read_bytes - previous[0]) / elapsed
                    write_rate = max(0, device.write_bytes - previous[1]) / elapsed
            current[key] = (device.read_bytes, device.write_bytes, sampled_at)
            rates.append(DiskIORate(device.device, read_rate, write_rate))
        self._previous_disk = current
        return tuple(rates)

    def _sample_network_io(self, sampled_at: float) -> tuple[NetworkIORate, ...]:
        """Calculate per-interface network throughput from /proc/net/dev."""
        try:
            counters = parse_network_dev((self.proc_root / "net" / "dev").read_text())
        except (OSError, ValueError):
            self._previous_network = {}
            return ()
        current: dict[str, tuple[int, int, float]] = {}
        rates: list[NetworkIORate] = []
        for interface in counters:
            previous = self._previous_network.get(interface.interface)
            receive_rate: float | None = None
            transmit_rate: float | None = None
            if previous is not None:
                elapsed = sampled_at - previous[2]
                if elapsed > 0:
                    receive_rate = max(
                        0, interface.receive_bytes - previous[0],
                    ) / elapsed
                    transmit_rate = max(
                        0, interface.transmit_bytes - previous[1],
                    ) / elapsed
            current[interface.interface] = (
                interface.receive_bytes, interface.transmit_bytes, sampled_at,
            )
            rates.append(NetworkIORate(
                interface.interface, receive_rate, transmit_rate,
            ))
        self._previous_network = current
        return tuple(rates)

    @staticmethod
    def _read_status(path: Path) -> dict[str, str]:
        fields: dict[str, str] = {}
        for line in path.read_text().splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                fields[key] = value.strip()
        return fields

    def _read_memory(self) -> tuple[int, int]:
        values: dict[str, int] = {}
        for line in (self.proc_root / "meminfo").read_text().splitlines():
            parts = line.split()
            if len(parts) >= 2:
                try:
                    values[parts[0].rstrip(":")] = int(parts[1])
                except ValueError:
                    continue
        total = values.get("MemTotal", 0)
        available = values.get("MemAvailable")
        if available is None:
            available = (
                values.get("MemFree", 0) + values.get("Buffers", 0)
                + values.get("Cached", 0) + values.get("SReclaimable", 0)
                - values.get("Shmem", 0)
            )
        return total, max(0, total - available)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.collect_once()
            except (OSError, ValueError):
                # A temporarily unavailable /proc root should not kill the worker.
                pass
            self._stop.wait(self.poll_interval)


