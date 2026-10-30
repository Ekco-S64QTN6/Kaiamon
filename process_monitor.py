#!/usr/bin/env python3
"""Linux process monitor with an htop-style curses interface.

The collector owns its /proc reads and publishes frozen snapshots. Curses is
used only by ProcessMonitorUI on the main thread.
"""
from __future__ import annotations

import argparse
import curses
import math
import os
import pwd
import signal
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

POLL_INTERVAL = 1.0
FRAME_INTERVAL_MS = 100
SORT_FIELDS = ("CPU", "MEM", "PID", "USER", "TIME", "COMMAND")
SIGNAL_CHOICES = {
    "1": (signal.SIGTERM, "TERM"),
    "2": (signal.SIGKILL, "KILL"),
    "3": (signal.SIGINT, "INT"),
    "4": (signal.SIGHUP, "HUP"),
}

DEFAULT_COLUMNS = (
    "PID", "USER", "PRI", "NI", "VIRT", "RES", "SHR", "STATE",
    "CPU", "MEM", "TIME", "COMMAND",
)
COLUMN_LABELS = {
    "PID": "PID", "USER": "USER", "PRI": "PRI", "NI": "NI",
    "VIRT": "VIRT", "RES": "RES", "SHR": "SHR", "STATE": "S",
    "CPU": "CPU%", "MEM": "MEM%", "TIME": "TIME+", "COMMAND": "COMMAND",
}
COLUMN_WIDTHS = {
    "PID": 6, "USER": 8, "PRI": 3, "NI": 3, "VIRT": 7, "RES": 7,
    "SHR": 6, "STATE": 1, "CPU": 6, "MEM": 5, "TIME": 8, "COMMAND": 24,
}
RIGHT_ALIGNED_COLUMNS = frozenset({
    "PID", "PRI", "NI", "VIRT", "RES", "SHR", "CPU", "MEM", "TIME",
})
COLUMN_ALIASES = {"CPU%": "CPU", "MEM%": "MEM", "TIME+": "TIME", "S": "STATE"}


def parse_columns(value: str) -> tuple[str, ...]:
    """Parse an ordered, comma-separated list of visible process columns."""
    if not isinstance(value, str):
        raise argparse.ArgumentTypeError("columns must be a comma-separated string")
    requested = [part.strip().upper() for part in value.split(",")]
    if not requested or any(not part for part in requested):
        raise argparse.ArgumentTypeError("columns cannot contain empty entries")
    columns = tuple(COLUMN_ALIASES.get(part, part) for part in requested)
    unsupported = [column for column in columns if column not in COLUMN_LABELS]
    if unsupported:
        raise argparse.ArgumentTypeError(
            "unsupported column(s): " + ", ".join(unsupported)
        )
    if len(set(columns)) != len(columns):
        raise argparse.ArgumentTypeError("columns cannot contain duplicates")
    return columns


def parse_refresh_interval(value: str) -> float:
    """Validate a /proc sampling interval in seconds."""
    try:
        interval = float(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("refresh interval must be a number") from exc
    if not math.isfinite(interval) or interval < 0.05:
        raise argparse.ArgumentTypeError(
            "refresh interval must be a finite number of at least 0.05 seconds"
        )
    return interval


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


@dataclass(frozen=True)
class ProcessSnapshot:
    processes: tuple[ProcessInfo, ...] = ()
    cpu_percent: float = 0.0
    per_cpu_percent: tuple[float, ...] = ()
    memory_total_kb: int = 0
    memory_used_kb: int = 0
    sampled_at: float = 0.0


@dataclass(frozen=True)
class CpuTimes:
    total: int
    idle: int


def parse_proc_stat(contents: str) -> dict[str, int | str]:
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


def cpu_usage(previous: CpuTimes, current: CpuTimes) -> float:
    total_delta = current.total - previous.total
    idle_delta = current.idle - previous.idle
    if total_delta <= 0:
        return 0.0
    busy = max(0, total_delta - max(0, idle_delta))
    return min(100.0, (busy / total_delta) * 100.0)


def filter_processes(
    processes: Iterable[ProcessInfo], query: str,
) -> tuple[ProcessInfo, ...]:
    """Filter by PID, user, or command (case-insensitive substring)."""
    needle = query.strip().casefold()
    if not needle:
        return tuple(processes)
    return tuple(
        process for process in processes
        if needle in str(process.pid)
        or needle in process.user.casefold()
        or needle in process.command.casefold()
    )


def sort_processes(
    processes: Iterable[ProcessInfo], field: str = "CPU",
    descending: bool = True,
) -> tuple[ProcessInfo, ...]:
    """Return processes sorted by a supported column, with PID tie-breaker."""
    field = field.upper()
    getters: Mapping[str, Callable[[ProcessInfo], object]] = {
        "CPU": lambda p: p.cpu_percent,
        "MEM": lambda p: p.mem_percent,
        "PID": lambda p: p.pid,
        "USER": lambda p: p.user.casefold(),
        "TIME": lambda p: p.cpu_time_s,
        "COMMAND": lambda p: p.command.casefold(),
    }
    if field not in getters:
        raise ValueError(f"unsupported sort field: {field}")
    return tuple(sorted(
        processes, key=lambda p: (getters[field](p), p.pid),
        reverse=descending,
    ))


def tree_processes(
    processes: Iterable[ProcessInfo], field: str = "CPU",
    descending: bool = True, collapsed: Iterable[int] | None = None,
) -> tuple[tuple[ProcessInfo, int], ...]:
    """Flatten a process hierarchy, omitting descendants of collapsed PIDs."""
    ordered = sort_processes(processes, field, descending)
    collapsed_pids = set(collapsed or ())
    by_pid = {process.pid: process for process in ordered}
    children: dict[int, list[ProcessInfo]] = {}
    roots: list[ProcessInfo] = []
    for process in ordered:
        if process.ppid in by_pid and process.ppid != process.pid:
            children.setdefault(process.ppid, []).append(process)
        else:
            roots.append(process)
    result: list[tuple[ProcessInfo, int]] = []
    visited: set[int] = set()

    def visit(process: ProcessInfo, depth: int) -> None:
        if process.pid in visited:
            return
        visited.add(process.pid)
        result.append((process, depth))
        if process.pid not in collapsed_pids:
            for child in children.get(process.pid, []):
                visit(child, depth + 1)

    for root in roots:
        visit(root, 0)
    # A malformed parent cycle should not make rows disappear.
    for process in ordered:
        if process.pid not in visited:
            visit(process, 0)
    return tuple(result)


def validate_process_target(pid: int, *, current_pid: int | None = None) -> None:
    """Reject invalid and self-targeted process operations."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        raise ValueError("PID must identify a non-system process greater than 1")
    if current_pid is None:
        current_pid = os.getpid()
    if pid == current_pid:
        raise ValueError("refusing to signal or renice the monitor itself")


def validate_nice_value(nice: int) -> int:
    if isinstance(nice, bool) or not isinstance(nice, int) or not -20 <= nice <= 19:
        raise ValueError("nice value must be an integer from -20 through 19")
    return nice


def signal_process(pid: int, signum: int) -> None:
    validate_process_target(pid)
    allowed = {int(value) for value, _label in SIGNAL_CHOICES.values()}
    if signum not in allowed:
        raise ValueError("unsupported process signal")
    os.kill(pid, signum)


def renice_process(pid: int, nice: int) -> None:
    validate_process_target(pid)
    os.setpriority(os.PRIO_PROCESS, pid, validate_nice_value(nice))


class ProcessCollector:
    """Poll /proc in a worker thread and publish immutable process snapshots."""

    def __init__(
        self,
        proc_root: str | os.PathLike[str] = "/proc",
        poll_interval: float = POLL_INTERVAL,
        clock_ticks: int | None = None,
        page_size: int | None = None,
        uid_lookup: Callable[[int], str] | None = None,
    ) -> None:
        self.proc_root = Path(proc_root)
        self.poll_interval = max(0.05, float(poll_interval))
        self.clock_ticks = clock_ticks or int(os.sysconf("SC_CLK_TCK"))
        self.page_size = page_size or int(os.sysconf("SC_PAGE_SIZE"))
        self._uid_lookup = uid_lookup or self._username
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._snapshot = ProcessSnapshot()
        self._previous_process: dict[int, tuple[int, int]] = {}
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

    def collect_once(self) -> ProcessSnapshot:
        """Collect one sample; public to make fixture-based tests deterministic."""
        cpu_samples = parse_cpu_times((self.proc_root / "stat").read_text())
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

        processes: list[ProcessInfo] = []
        current_previous: dict[int, tuple[int, int]] = {}
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
                ))
            except (OSError, ValueError, IndexError, KeyError, OverflowError):
                # Processes routinely exit or become inaccessible while sampled.
                continue
        self._previous_process = current_previous
        snapshot = ProcessSnapshot(
            processes=tuple(processes),
            cpu_percent=overall_cpu,
            per_cpu_percent=tuple(core_values),
            memory_total_kb=mem_total,
            memory_used_kb=mem_used,
            sampled_at=time.time(),
        )
        with self._lock:
            self._snapshot = snapshot
        return snapshot

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


class ProcessMonitorUI:
    """Curses front end. All curses calls remain on the main thread."""

    def __init__(
        self, *, refresh_interval: float = POLL_INTERVAL,
        columns: Sequence[str] = DEFAULT_COLUMNS,
    ) -> None:
        self.refresh_interval = parse_refresh_interval(str(refresh_interval))
        self.columns = parse_columns(",".join(columns))
        self.collector = ProcessCollector(poll_interval=self.refresh_interval)
        self.selected = 0
        self.scroll = 0
        self.sort_field = "CPU"
        self.descending = True
        self.filter_query = ""
        self.tree_mode = False
        self.collapsed: set[int] = set()
        self.status = (
            "F1 Help  F5 Tree  F6 Sort  / Filter  K Signal  r Renice  q Quit"
        )
        self._stdscr: Any = None
        self._running = False

    def run(self) -> None:
        original_sigint = signal.getsignal(signal.SIGINT)
        original_sigterm = signal.getsignal(signal.SIGTERM)
        self._running = True
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
        try:
            self.collector.start()
            curses.wrapper(self._main_loop)
        finally:
            self._running = False
            self.collector.stop()
            signal.signal(signal.SIGINT, original_sigint)
            signal.signal(signal.SIGTERM, original_sigterm)

    def _signal_handler(self, _signum: int, _frame: object) -> None:
        self._running = False

    def _main_loop(self, stdscr) -> None:
        self._stdscr = stdscr
        stdscr.keypad(True)
        stdscr.timeout(FRAME_INTERVAL_MS)
        curses.curs_set(0)
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_CYAN, -1)
        curses.init_pair(2, curses.COLOR_GREEN, -1)
        curses.init_pair(3, curses.COLOR_YELLOW, -1)
        curses.init_pair(4, curses.COLOR_RED, -1)
        curses.init_pair(5, curses.COLOR_BLACK, curses.COLOR_CYAN)
        while self._running:
            self._draw(self.collector.get_snapshot())
            key = stdscr.getch()
            if key in (ord("q"), ord("Q")):
                self._running = False
                break
            self._handle_key(key)

    def _visible(
        self, snapshot: ProcessSnapshot,
    ) -> tuple[tuple[ProcessInfo, int], ...]:
        filtered = filter_processes(snapshot.processes, self.filter_query)
        if self.tree_mode:
            return tree_processes(
                filtered, self.sort_field, self.descending, self.collapsed,
            )
        return tuple(
            (process, 0)
            for process in sort_processes(
                filtered, self.sort_field, self.descending,
            )
        )

    def _draw(self, snapshot: ProcessSnapshot) -> None:
        screen = self._stdscr
        if screen is None:
            return
        screen.erase()
        height, width = screen.getmaxyx()
        if height < 8 or width < 60:
            self._put(
                0, 0, "Terminal too small (need at least 60x8).",
                curses.color_pair(4),
            )
            screen.refresh()
            return
        cpu_bar = self._meter(
            snapshot.cpu_percent, max(8, min(18, width - 50)),
        )
        self._put(
            0, 0, f"CPU {snapshot.cpu_percent:5.1f}% {cpu_bar}",
            curses.color_pair(1) | curses.A_BOLD,
        )
        core_count = max(1, min(8, (width - 32) // 8))
        core_text = " ".join(
            f"{i}:{self._meter(value, 3)}"
            for i, value in enumerate(snapshot.per_cpu_percent[:core_count])
        )
        self._put(
            0, min(width - 1, 32), core_text[:max(0, width - 33)],
            curses.color_pair(2),
        )
        mem_pct = (
            snapshot.memory_used_kb / snapshot.memory_total_kb * 100.0
            if snapshot.memory_total_kb else 0.0
        )
        mem_bar = self._meter(mem_pct, max(10, min(24, width - 30)))
        self._put(
            1, 0,
            f"Mem {self._fmt_size(snapshot.memory_used_kb):>8}/"
            f"{self._fmt_size(snapshot.memory_total_kb):<8} "
            f"{mem_pct:5.1f}% {mem_bar}",
            curses.color_pair(1),
        )
        mode = (
            f"Sort:{self.sort_field}{'↓' if self.descending else '↑'}  "
            + ("TREE" if self.tree_mode else "FLAT")
            + f"  Filter:{self.filter_query or '—'}"
        )
        self._put(2, 0, mode[:width], curses.color_pair(3))
        fixed_width = sum(
            COLUMN_WIDTHS[column]
            for column in self.columns if column != "COMMAND"
        )
        spaces = max(0, len(self.columns) - 1)
        command_is_last = bool(self.columns and self.columns[-1] == "COMMAND")
        header_command_width = max(1, width - 1 - fixed_width - spaces) if command_is_last else COLUMN_WIDTHS["COMMAND"]
        header_cells = []
        for column in self.columns:
            cell_width = header_command_width if column == "COMMAND" else COLUMN_WIDTHS[column]
            alignment = "<" if column == "COMMAND" else ">"
            header_cells.append(f"{COLUMN_LABELS[column]:{alignment}{cell_width}}")
        self._put(
            3, 0, " ".join(header_cells)[:width],
            curses.A_REVERSE | curses.A_BOLD,
        )

        rows = self._visible(snapshot)
        filtered = filter_processes(snapshot.processes, self.filter_query)
        filtered_pids = {process.pid for process in filtered}
        child_pids = {
            process.ppid for process in filtered
            if process.ppid in filtered_pids and process.ppid != process.pid
        }
        max_rows = max(0, height - 6)
        if self.selected >= len(rows):
            self.selected = max(0, len(rows) - 1)
        if self.selected < self.scroll:
            self.scroll = self.selected
        if self.selected >= self.scroll + max_rows:
            self.scroll = self.selected - max_rows + 1
        for view_index, (proc, depth) in enumerate(
            rows[self.scroll:self.scroll + max_rows]
        ):
            screen_y = 4 + view_index
            values = {
                "PID": str(proc.pid), "USER": proc.user,
                "PRI": str(proc.priority), "NI": str(proc.nice),
                "VIRT": self._fmt_size(proc.virt_kb),
                "RES": self._fmt_size(proc.res_kb),
                "SHR": self._fmt_size(proc.shr_kb), "STATE": proc.state[:1],
                "CPU": f"{proc.cpu_percent:.1f}", "MEM": f"{proc.mem_percent:.1f}",
                "TIME": self._fmt_time(proc.cpu_time_s),
                "COMMAND": proc.command.replace("\n", " "),
            }
            indent = ""
            if self.tree_mode:
                marker = ("+" if proc.pid in self.collapsed else "-") if proc.pid in child_pids else " "
                indent = "  " * min(depth, 6) + marker + " "
                target = "COMMAND" if "COMMAND" in values else ("PID" if "PID" in values else self.columns[0])
                values[target] = indent + values[target]
            command_width = (
                max(1, width - 1 - len(indent) - fixed_width - spaces)
                if command_is_last else COLUMN_WIDTHS["COMMAND"]
            )
            cells = []
            for column in self.columns:
                cell_width = command_width if column == "COMMAND" else COLUMN_WIDTHS[column]
                alignment = "<" if column == "COMMAND" else (">" if column in RIGHT_ALIGNED_COLUMNS else "<")
                cells.append(f"{values[column]:{alignment}{cell_width}}")
            line = " ".join(cells)
            attr = (
                curses.color_pair(5)
                if self.scroll + view_index == self.selected else 0
            )
            if proc.state == "Z":
                attr = curses.color_pair(4)
            self._put(screen_y, 0, line[:width], attr)
        footer_y = height - 2
        self._put(footer_y, 0, self.status[:width], curses.color_pair(2))
        self._put(
            height - 1, 0,
            f"{len(rows)} processes  |  q Quit  ↑/↓ Select  PgUp/PgDn Scroll"[:width],
            curses.A_DIM,
        )
        screen.refresh()

    def _put(self, y: int, x: int, value: str, attr: int = 0) -> None:
        if self._stdscr is None:
            return
        height, width = self._stdscr.getmaxyx()
        if not (0 <= y < height and 0 <= x < width):
            return
        try:
            self._stdscr.addnstr(y, x, value, max(0, width - x), attr)
        except curses.error:
            pass

    @staticmethod
    def _meter(value: float, width: int) -> str:
        filled = max(0, min(width, round(width * value / 100.0)))
        return "[" + "█" * filled + " " * (width - filled) + "]"

    @staticmethod
    def _fmt_size(kb: int) -> str:
        if kb >= 1024 * 1024:
            return f"{kb / (1024 * 1024):.1f}G"
        if kb >= 1024:
            return f"{kb / 1024:.1f}M"
        return f"{kb}K"

    @staticmethod
    def _fmt_time(seconds: float) -> str:
        hours, remainder = divmod(int(seconds), 3600)
        minutes, secs = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"

    def _selected_process(self) -> ProcessInfo | None:
        rows = self._visible(self.collector.get_snapshot())
        if not rows or self.selected >= len(rows):
            return None
        return rows[self.selected][0]

    def _set_selected_tree_expansion(self, collapse: bool | None) -> None:
        """Collapse, expand, or toggle the selected node in tree mode."""
        if not self.tree_mode:
            self.status = "Enable tree view first (t)"
            return
        snapshot = self.collector.get_snapshot()
        rows = self._visible(snapshot)
        if not rows or self.selected >= len(rows):
            self.status = "No process selected"
            return
        process = rows[self.selected][0]
        filtered = filter_processes(snapshot.processes, self.filter_query)
        has_children = any(
            item.ppid == process.pid and item.pid != process.pid
            for item in filtered
        )
        if not has_children:
            self.status = f"PID {process.pid} has no visible children"
            return
        should_collapse = process.pid not in self.collapsed if collapse is None else collapse
        if should_collapse:
            self.collapsed.add(process.pid)
            self.status = f"Collapsed children of PID {process.pid}"
        else:
            self.collapsed.discard(process.pid)
            self.status = f"Expanded children of PID {process.pid}"

    def _prompt(self, prompt: str) -> str:
        assert self._stdscr is not None
        height, width = self._stdscr.getmaxyx()
        self._put(height - 1, 0, " " * width)
        self._put(
            height - 1, 0, prompt[:max(0, width - 1)], curses.A_BOLD,
        )
        curses.echo()
        curses.curs_set(1)
        self._stdscr.refresh()
        try:
            raw = self._stdscr.getstr(
                height - 1, min(len(prompt), width - 1),
                max(1, width - len(prompt) - 1),
            )
            return raw.decode("utf-8", errors="replace").strip()
        finally:
            curses.noecho()
            curses.curs_set(0)

    def _handle_key(self, key: int) -> None:
        rows = self._visible(self.collector.get_snapshot())
        if key in (curses.KEY_UP, ord("k")):
            self.selected = max(0, self.selected - 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            self.selected = min(max(0, len(rows) - 1), self.selected + 1)
        elif key == curses.KEY_PPAGE:
            self.selected = max(0, self.selected - 10)
        elif key == curses.KEY_NPAGE:
            self.selected = min(max(0, len(rows) - 1), self.selected + 10)
        elif key == curses.KEY_HOME:
            self.selected = 0
        elif key == curses.KEY_END:
            self.selected = max(0, len(rows) - 1)
        elif key in (curses.KEY_F5, ord("t"), ord("T")):
            self.tree_mode = not self.tree_mode
            self.selected = 0
            self.status = f"Tree view {'enabled' if self.tree_mode else 'disabled'}"
        elif key in (curses.KEY_LEFT, ord("-")):
            self._set_selected_tree_expansion(True)
        elif key in (curses.KEY_RIGHT, ord("+"), ord("=")):
            self._set_selected_tree_expansion(False)
        elif key == ord(" "):
            self._set_selected_tree_expansion(None)
        elif key in (curses.KEY_F6, ord("s"), ord("S")):
            index = SORT_FIELDS.index(self.sort_field)
            if self.descending:
                self.descending = False
            else:
                self.sort_field = SORT_FIELDS[(index + 1) % len(SORT_FIELDS)]
                self.descending = True
            self.selected = 0
            self.status = (
                f"Sorted by {self.sort_field} "
                f"{'descending' if self.descending else 'ascending'}"
            )
        elif key == ord("/"):
            self.filter_query = self._prompt("Filter: ")
            self.selected = self.scroll = 0
            self.status = "Filter applied; Esc clears it"
        elif key == 27:
            self.filter_query = ""
            self.selected = self.scroll = 0
            self.status = "Filter cleared"
        elif key in (ord("r"), ord("R")):
            proc = self._selected_process()
            if proc:
                value = self._prompt(f"Renice PID {proc.pid} to [-20..19]: ")
                try:
                    renice_process(proc.pid, int(value))
                    self.status = f"Reniced PID {proc.pid} to {int(value)}"
                except (ValueError, OSError, PermissionError) as exc:
                    self.status = f"Renice failed: {exc}"
        elif key == ord("K"):
            proc = self._selected_process()
            if proc:
                choice = self._prompt(
                    f"Signal PID {proc.pid}: 1 TERM  2 KILL  3 INT  4 HUP: "
                )
                try:
                    signum, label = SIGNAL_CHOICES[choice]
                    confirmation = self._prompt(
                        f"Confirm {label} for PID {proc.pid}? [y/N]: "
                    )
                    if confirmation.casefold() != "y":
                        self.status = f"{label} cancelled for PID {proc.pid}"
                        return
                    signal_process(proc.pid, int(signum))
                    self.status = f"Sent {label} to PID {proc.pid}"
                except (KeyError, ValueError, OSError, PermissionError) as exc:
                    self.status = f"Signal failed: {exc}"
        elif key == curses.KEY_F1:
            self.status = (
                "↑/↓ Select  PgUp/PgDn Scroll  F5 Tree  ←/→ Collapse  "
                "Space Toggle  F6 Sort  / Filter  K Signal  r Renice  q Quit"
            )


def main(argv: Sequence[str] | None = None) -> None:
    """Run the Linux process monitor."""
    if not sys.platform.startswith("linux"):
        raise SystemExit("Kaiamon's process monitor currently requires Linux /proc")
    parser = argparse.ArgumentParser(description="Kaiamon Linux process monitor")
    parser.add_argument(
        "--refresh", type=parse_refresh_interval, default=POLL_INTERVAL,
        metavar="SECONDS",
        help="process sampling interval in seconds (minimum 0.05; default: 1.0)",
    )
    parser.add_argument(
        "--columns", type=parse_columns, default=DEFAULT_COLUMNS,
        metavar="LIST", help="ordered comma-separated process columns",
    )
    args = parser.parse_args(argv)
    ProcessMonitorUI(refresh_interval=args.refresh, columns=args.columns).run()


if __name__ == "__main__":
    main()
