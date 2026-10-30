"""Keyboard dispatch and interactive prompts for the main-thread curses UI."""
from __future__ import annotations

import curses
from typing import Any

from process_actions import SIGNAL_CHOICES, renice_process, signal_process

SORT_FIELDS = ("CPU", "MEM", "PID", "USER", "TIME", "COMMAND")


class ProcessInput:
    """Handle keyboard actions and prompts against the owning UI state.

    The controller deliberately receives the UI rather than owning curses state:
    every prompt and terminal operation still runs synchronously on the main thread.
    """

    def set_selected_tree_expansion(self, ui: Any, collapse: bool | None) -> None:
        """Collapse, expand, or toggle the selected node in tree mode."""
        if not ui.tree_mode:
            ui.status = "Enable tree view first (t)"
            return
        snapshot = ui.collector.get_snapshot()
        rows = ui._visible(snapshot)
        if not rows or ui.selected >= len(rows):
            ui.status = "No process selected"
            return
        process = rows[ui.selected][0]
        filtered = ui._filtered_processes(snapshot)
        has_children = any(
            item.ppid == process.pid and item.pid != process.pid
            for item in filtered
        )
        if not has_children:
            ui.status = f"PID {process.pid} has no visible children"
            return
        should_collapse = process.pid not in ui.collapsed if collapse is None else collapse
        if should_collapse:
            ui.collapsed.add(process.pid)
            ui.status = f"Collapsed children of PID {process.pid}"
        else:
            ui.collapsed.discard(process.pid)
            ui.status = f"Expanded children of PID {process.pid}"


    def prompt(self, ui: Any, prompt: str) -> str:
        assert ui._stdscr is not None
        height, width = ui._stdscr.getmaxyx()
        ui._put(height - 1, 0, " " * width)
        ui._put(
            height - 1, 0, prompt[:max(0, width - 1)], curses.A_BOLD,
        )
        curses.echo()
        curses.curs_set(1)
        ui._stdscr.refresh()
        try:
            raw = ui._stdscr.getstr(
                height - 1, min(len(prompt), width - 1),
                max(1, width - len(prompt) - 1),
            )
            return raw.decode("utf-8", errors="replace").strip()
        finally:
            curses.noecho()
            curses.curs_set(0)


    def handle_key(self, ui: Any, key: int) -> None:
        rows = ui._visible(ui.collector.get_snapshot())
        if key in (curses.KEY_UP, ord("k")):
            ui.selected = max(0, ui.selected - 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            ui.selected = min(max(0, len(rows) - 1), ui.selected + 1)
        elif key == curses.KEY_PPAGE:
            ui.selected = max(0, ui.selected - 10)
        elif key == curses.KEY_NPAGE:
            ui.selected = min(max(0, len(rows) - 1), ui.selected + 10)
        elif key == curses.KEY_HOME:
            ui.selected = 0
        elif key == curses.KEY_END:
            ui.selected = max(0, len(rows) - 1)
        elif key in (curses.KEY_F5, ord("t"), ord("T")):
            ui.tree_mode = not ui.tree_mode
            ui.selected = 0
            ui.status = f"Tree view {'enabled' if ui.tree_mode else 'disabled'}"
        elif key in (curses.KEY_LEFT, ord("-")):
            ui._set_selected_tree_expansion(True)
        elif key in (curses.KEY_RIGHT, ord("+"), ord("=")):
            ui._set_selected_tree_expansion(False)
        elif key == ord(" "):
            ui._set_selected_tree_expansion(None)
        elif key in (curses.KEY_F6, ord("s"), ord("S")):
            index = SORT_FIELDS.index(ui.sort_field)
            if ui.descending:
                ui.descending = False
            else:
                ui.sort_field = SORT_FIELDS[(index + 1) % len(SORT_FIELDS)]
                ui.descending = True
            ui.selected = 0
            ui.status = (
                f"Sorted by {ui.sort_field} "
                f"{'descending' if ui.descending else 'ascending'}"
            )
        elif key == ord("/"):
            ui.filter_query = ui._prompt("Filter: ")
            ui.selected = ui.scroll = 0
            ui.status = "Filter applied; Esc clears it"
        elif key == 27:
            ui.filter_query = ""
            ui.selected = ui.scroll = 0
            ui.status = "Filter cleared"
        elif key in (ord("r"), ord("R")):
            proc = ui._selected_process()
            if proc:
                value = ui._prompt(f"Renice PID {proc.pid} to [-20..19]: ")
                try:
                    renice_process(proc.pid, int(value))
                    ui.status = f"Reniced PID {proc.pid} to {int(value)}"
                except (ValueError, OSError, PermissionError) as exc:
                    ui.status = f"Renice failed: {exc}"
        elif key == ord("K"):
            proc = ui._selected_process()
            if proc:
                choice = ui._prompt(
                    f"Signal PID {proc.pid}: 1 TERM  2 KILL  3 INT  4 HUP: "
                )
                try:
                    signum, label = SIGNAL_CHOICES[choice]
                    confirmation = ui._prompt(
                        f"Confirm {label} for PID {proc.pid}? [y/N]: "
                    )
                    if confirmation.casefold() != "y":
                        ui.status = f"{label} cancelled for PID {proc.pid}"
                        return
                    signal_process(proc.pid, int(signum))
                    ui.status = f"Sent {label} to PID {proc.pid}"
                except (KeyError, ValueError, OSError, PermissionError) as exc:
                    ui.status = f"Signal failed: {exc}"
        elif key == ord("["):
            ui.horizontal_scroll = max(0, ui.horizontal_scroll - 8)
            ui.status = "Process table shifted left"
        elif key == ord("]"):
            ui.horizontal_scroll += 8
            ui.status = "Process table shifted right"
        elif key == curses.KEY_F1:
            ui.status = (
                "↑/↓ Select  PgUp/PgDn Scroll  [ ] Columns  F5 Tree  "
                "←/→ Collapse  Space Toggle  F6 Sort  / Filter  K Signal  r Renice  q Quit"
            )

