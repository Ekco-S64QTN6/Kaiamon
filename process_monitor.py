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

from process_config import (
    DEFAULT_COLUMNS, SORT_FIELDS, ConfigError, MonitorConfig,
    default_config_path, load_config, normalize_columns, save_config,
)
from process_core import POLL_INTERVAL, ProcessCollector, ProcessInfo, ProcessSnapshot
from process_input import ProcessInput
from process_render import COLUMN_LABELS, ProcessRenderer

FRAME_INTERVAL_MS = 100


def parse_columns(value: str) -> tuple[str, ...]:
    """Parse an ordered, comma-separated list of visible process columns."""
    try:
        return normalize_columns(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


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


def filter_tree_context(
    processes: Iterable[ProcessInfo], query: str,
) -> tuple[ProcessInfo, ...]:
    """Keep matching processes and their available ancestors for tree context."""
    rows = tuple(processes)
    matches = filter_processes(rows, query)
    if not query.strip() or not matches:
        return matches

    by_pid = {process.pid: process for process in rows}
    included_pids = {process.pid for process in matches}
    for process in matches:
        current = process
        visited = {process.pid}
        while current.ppid in by_pid and current.ppid not in visited:
            parent = by_pid[current.ppid]
            included_pids.add(parent.pid)
            visited.add(parent.pid)
            current = parent
    return tuple(process for process in rows if process.pid in included_pids)


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
        sort_field: str = "CPU", descending: bool = True,
        tree_mode: bool = False,
    ) -> None:
        self.refresh_interval = parse_refresh_interval(str(refresh_interval))
        self.columns = parse_columns(",".join(columns))
        self.sort_field = sort_field.upper()
        if self.sort_field not in SORT_FIELDS:
            raise ValueError(f"unsupported sort field: {self.sort_field}")
        self.descending = descending
        self.tree_mode = tree_mode
        self.collector = ProcessCollector(poll_interval=self.refresh_interval)
        self.renderer = ProcessRenderer()
        self.input = ProcessInput()
        self.selected = 0
        self.scroll = 0
        self.horizontal_scroll = 0
        self.filter_query = ""
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
        if self.tree_mode:
            return filter_tree_context(snapshot.processes, self.filter_query)
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
    """Run the Linux process monitor with XDG-backed persistent preferences."""
    if not sys.platform.startswith("linux"):
        raise SystemExit("Kaiamon's process monitor currently requires Linux /proc")
    parser = argparse.ArgumentParser(description="Kaiamon Linux process monitor")
    parser.add_argument(
        "--config", type=Path, default=None, metavar="PATH",
        help="configuration file (default: $XDG_CONFIG_HOME/kaiamon/config.json)",
    )
    parser.add_argument(
        "--no-save-config", action="store_true",
        help="load configuration but do not save preferences on exit",
    )
    parser.add_argument(
        "--refresh", type=parse_refresh_interval, default=None,
        metavar="SECONDS",
        help="override process sampling interval (minimum 0.05 seconds)",
    )
    parser.add_argument(
        "--columns", type=parse_columns, default=None,
        metavar="LIST", help="override ordered comma-separated process columns",
    )
    parser.add_argument(
        "--sort", choices=SORT_FIELDS, default=None,
        help="override the saved process sort field",
    )
    parser.add_argument(
        "--sort-order", choices=("asc", "desc"), default=None,
        help="override saved sort direction",
    )
    tree_group = parser.add_mutually_exclusive_group()
    tree_group.add_argument(
        "--tree", dest="tree_mode", action="store_true", default=None,
        help="start in parent/child tree view",
    )
    tree_group.add_argument(
        "--flat", dest="tree_mode", action="store_false",
        help="start in flat process-list view",
    )
    args = parser.parse_args(argv)

    config_path = args.config.expanduser() if args.config else default_config_path()
    try:
        stored = load_config(config_path)
    except ConfigError as exc:
        parser.error(str(exc))

    ui = ProcessMonitorUI(
        refresh_interval=(
            args.refresh if args.refresh is not None else stored.refresh_interval
        ),
        columns=args.columns if args.columns is not None else stored.columns,
        sort_field=args.sort if args.sort is not None else stored.sort_field,
        descending=(
            stored.descending if args.sort_order is None
            else args.sort_order == "desc"
        ),
        tree_mode=(
            stored.tree_mode if args.tree_mode is None else args.tree_mode
        ),
    )
    ui.run()

    if not args.no_save_config:
        updated = MonitorConfig(
            refresh_interval=ui.refresh_interval,
            columns=ui.columns,
            sort_field=ui.sort_field,
            descending=ui.descending,
            tree_mode=ui.tree_mode,
        )
        try:
            save_config(config_path, updated)
        except OSError as exc:
            print(f"Kaiamon: could not save configuration to {config_path}: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
