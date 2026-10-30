#!/usr/bin/env python3
"""Curses UI and process controls for the Linux htop-style process monitor.

Procfs sampling and immutable process snapshots live in process_core.py.
All curses operations stay in the main thread.
"""
from __future__ import annotations

import argparse
import curses
import math
import signal
import sys
from typing import Any, Callable, Iterable, Mapping, Sequence

from process_core import POLL_INTERVAL, ProcessCollector, ProcessInfo, ProcessSnapshot
from process_render import COLUMN_LABELS, ProcessRenderer
from process_input import ProcessInput

FRAME_INTERVAL_MS = 100
SORT_FIELDS = ("CPU", "MEM", "PID", "USER", "TIME", "COMMAND")
DEFAULT_COLUMNS = (
    "PID", "USER", "PRI", "NI", "VIRT", "RES", "SHR", "STATE",
    "CPU", "MEM", "TIME", "COMMAND",
)
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

    def mark_hidden(parent_pid: int) -> None:
        """Keep descendants of a collapsed node out of the orphan fallback."""
        for child in children.get(parent_pid, []):
            if child.pid in visited:
                continue
            visited.add(child.pid)
            mark_hidden(child.pid)

    def visit(process: ProcessInfo, depth: int) -> None:
        if process.pid in visited:
            return
        visited.add(process.pid)
        result.append((process, depth))
        if process.pid in collapsed_pids:
            mark_hidden(process.pid)
        else:
            for child in children.get(process.pid, []):
                visit(child, depth + 1)

    for root in roots:
        visit(root, 0)
    # A malformed parent cycle should not make rows disappear.
    for process in ordered:
        if process.pid not in visited:
            visit(process, 0)
    return tuple(result)


class ProcessMonitorUI:
    """Curses front end. All curses calls remain on the main thread."""

    def __init__(
        self, *, refresh_interval: float = POLL_INTERVAL,
        columns: Sequence[str] = DEFAULT_COLUMNS,
    ) -> None:
        self.refresh_interval = parse_refresh_interval(str(refresh_interval))
        self.columns = parse_columns(",".join(columns))
        self.collector = ProcessCollector(poll_interval=self.refresh_interval)
        self.renderer = ProcessRenderer()
        self.input = ProcessInput()
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

    def _filtered_processes(self, snapshot: ProcessSnapshot) -> tuple[ProcessInfo, ...]:
        return filter_processes(snapshot.processes, self.filter_query)

    def _visible(
        self, snapshot: ProcessSnapshot,
    ) -> tuple[tuple[ProcessInfo, int], ...]:
        filtered = self._filtered_processes(snapshot)
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
        self.renderer.draw(self, snapshot)

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
        self.input.set_selected_tree_expansion(self, collapse)

    def _prompt(self, prompt: str) -> str:
        return self.input.prompt(self, prompt)

    def _handle_key(self, key: int) -> None:
        self.input.handle_key(self, key)


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
