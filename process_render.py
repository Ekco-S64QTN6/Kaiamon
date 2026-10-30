"""Curses process-table rendering, delegated from the main-thread UI."""
from __future__ import annotations

from typing import Any

import curses

from process_core import ProcessSnapshot


class ProcessRenderer:
    """Render one immutable process snapshot using the UI's main-thread surface."""

    def draw(self, ui: Any, snapshot: ProcessSnapshot) -> None:
            screen = ui._stdscr
            if screen is None:
                return
            screen.erase()
            height, width = screen.getmaxyx()
            if height < 8 or width < 60:
                ui._put(
                    0, 0, "Terminal too small (need at least 60x8).",
                    curses.color_pair(4),
                )
                screen.refresh()
                return
            cpu_bar = ui._meter(
                snapshot.cpu_percent, max(8, min(18, width - 50)),
            )
            ui._put(
                0, 0, f"CPU {snapshot.cpu_percent:5.1f}% {cpu_bar}",
                curses.color_pair(1) | curses.A_BOLD,
            )
            core_count = max(1, min(8, (width - 32) // 8))
            core_text = " ".join(
                f"{i}:{ui._meter(value, 3)}"
                for i, value in enumerate(snapshot.per_cpu_percent[:core_count])
            )
            ui._put(
                0, min(width - 1, 32), core_text[:max(0, width - 33)],
                curses.color_pair(2),
            )
            mem_pct = (
                snapshot.memory_used_kb / snapshot.memory_total_kb * 100.0
                if snapshot.memory_total_kb else 0.0
            )
            mem_bar = ui._meter(mem_pct, max(10, min(24, width - 30)))
            ui._put(
                1, 0,
                f"Mem {ui._fmt_size(snapshot.memory_used_kb):>8}/"
                f"{ui._fmt_size(snapshot.memory_total_kb):<8} "
                f"{mem_pct:5.1f}% {mem_bar}",
                curses.color_pair(1),
            )
            mode = (
                f"Sort:{ui.sort_field}{'↓' if ui.descending else '↑'}  "
                + ("TREE" if ui.tree_mode else "FLAT")
                + f"  Filter:{ui.filter_query or '—'}"
            )
            ui._put(2, 0, mode[:width], curses.color_pair(3))
            fixed_width = sum(
                COLUMN_WIDTHS[column]
                for column in ui.columns if column != "COMMAND"
            )
            spaces = max(0, len(ui.columns) - 1)
            command_is_last = bool(ui.columns and ui.columns[-1] == "COMMAND")
            header_command_width = max(1, width - 1 - fixed_width - spaces) if command_is_last else COLUMN_WIDTHS["COMMAND"]
            header_cells = []
            for column in ui.columns:
                cell_width = header_command_width if column == "COMMAND" else COLUMN_WIDTHS[column]
                alignment = "<" if column == "COMMAND" else ">"
                header_cells.append(f"{COLUMN_LABELS[column]:{alignment}{cell_width}}")
            ui._put(
                3, 0, " ".join(header_cells)[:width],
                curses.A_REVERSE | curses.A_BOLD,
            )
        
            rows = ui._visible(snapshot)
            filtered = filter_processes(snapshot.processes, ui.filter_query)
            filtered_pids = {process.pid for process in filtered}
            child_pids = {
                process.ppid for process in filtered
                if process.ppid in filtered_pids and process.ppid != process.pid
            }
            max_rows = max(0, height - 6)
            if ui.selected >= len(rows):
                ui.selected = max(0, len(rows) - 1)
            if ui.selected < ui.scroll:
                ui.scroll = ui.selected
            if ui.selected >= ui.scroll + max_rows:
                ui.scroll = ui.selected - max_rows + 1
            for view_index, (proc, depth) in enumerate(
                rows[ui.scroll:ui.scroll + max_rows]
            ):
                screen_y = 4 + view_index
                values = {
                    "PID": str(proc.pid), "USER": proc.user,
                    "PRI": str(proc.priority), "NI": str(proc.nice),
                    "VIRT": ui._fmt_size(proc.virt_kb),
                    "RES": ui._fmt_size(proc.res_kb),
                    "SHR": ui._fmt_size(proc.shr_kb), "STATE": proc.state[:1],
                    "CPU": f"{proc.cpu_percent:.1f}", "MEM": f"{proc.mem_percent:.1f}",
                    "TIME": ui._fmt_time(proc.cpu_time_s),
                    "COMMAND": proc.command.replace("\n", " "),
                }
                indent = ""
                if ui.tree_mode:
                    marker = ("+" if proc.pid in ui.collapsed else "-") if proc.pid in child_pids else " "
                    indent = "  " * min(depth, 6) + marker + " "
                    target = "COMMAND" if "COMMAND" in values else ("PID" if "PID" in values else ui.columns[0])
                    values[target] = indent + values[target]
                command_width = (
                    max(1, width - 1 - len(indent) - fixed_width - spaces)
                    if command_is_last else COLUMN_WIDTHS["COMMAND"]
                )
                cells = []
                for column in ui.columns:
                    cell_width = command_width if column == "COMMAND" else COLUMN_WIDTHS[column]
                    alignment = "<" if column == "COMMAND" else (">" if column in RIGHT_ALIGNED_COLUMNS else "<")
                    cells.append(f"{values[column]:{alignment}{cell_width}}")
                line = " ".join(cells)
                attr = (
                    curses.color_pair(5)
                    if ui.scroll + view_index == ui.selected else 0
                )
                if proc.state == "Z":
                    attr = curses.color_pair(4)
                ui._put(screen_y, 0, line[:width], attr)
            footer_y = height - 2
            ui._put(footer_y, 0, ui.status[:width], curses.color_pair(2))
            ui._put(
                height - 1, 0,
                f"{len(rows)} processes  |  q Quit  ↑/↓ Select  PgUp/PgDn Scroll"[:width],
                curses.A_DIM,
            )
            screen.refresh()
        
        
