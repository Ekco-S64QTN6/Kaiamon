"""Curses process-table rendering, delegated from the main-thread UI."""
from __future__ import annotations

from typing import Any

import curses

from process_config import DEFAULT_COLUMN_WIDTHS\nfrom process_core import ProcessSnapshot

COLUMN_LABELS = {
    "PID": "PID", "USER": "USER", "PRI": "PRI", "NI": "NI",
    "VIRT": "VIRT", "RES": "RES", "SHR": "SHR", "STATE": "S",
    "CPU": "CPU%", "MEM": "MEM%", "TIME": "TIME+", "COMMAND": "COMMAND",
}
COLUMN_WIDTHS = {
    "PID": 6, "USER": 8, "PRI": 3, "NI": 3, "VIRT": 7, "RES": 7,
    "SHR": 6, "STATE": 1, "CPU": 6, "MEM": 5, "TIME": 8, "COMMAND": 24,
}
RIGHT_ALIGNED_COLUMNS = frozenset({"PID", "PRI", "NI", "VIRT", "RES", "SHR", "CPU", "MEM", "TIME"})


class ProcessRenderer:
    """Render one immutable process snapshot using the UI main-thread surface."""

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
        table_width = sum(ui.column_widths[column] for column in ui.columns)
        table_width += max(0, len(ui.columns) - 1)
        max_scroll = max(0, table_width - width)
        ui.horizontal_scroll = min(max(0, ui.horizontal_scroll), max_scroll)
        horizontal_scroll = ui.horizontal_scroll

        header_cells = []
        for column in ui.columns:
            cell_width = ui.column_widths[column]
            alignment = "<" if column == "COMMAND" else ">"
            label = COLUMN_LABELS[column][:cell_width]
            header_cells.append(f"{label:{alignment}{cell_width}}")
        header_line = " ".join(header_cells)
        ui._put(
            3, 0, header_line[horizontal_scroll:horizontal_scroll + width],
            curses.A_REVERSE | curses.A_BOLD,
        )

        rows = ui._visible(snapshot)
        filtered = ui._filtered_processes(snapshot)
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
            cells = []
            for column in ui.columns:
                cell_width = COLUMN_WIDTHS[column]
                alignment = (
                    "<" if column == "COMMAND"
                    else (">" if column in RIGHT_ALIGNED_COLUMNS else "<")
                )
                cell_value = str(values[column])[:cell_width]
                cells.append(f"{cell_value:{alignment}{cell_width}}")
            line = " ".join(cells)
            attr = (
                curses.color_pair(5)
                if ui.scroll + view_index == ui.selected else 0
            )
            if proc.state == "Z":
                attr = curses.color_pair(4)
            ui._put(screen_y, 0, line[horizontal_scroll:horizontal_scroll + width], attr)
        footer_y = height - 2
        ui._put(footer_y, 0, ui.status[:width], curses.color_pair(2))
        ui._put(
            height - 1, 0,
            f"{len(rows)} processes  |  F2 Setup  q Quit  ↑/↓ Select  PgUp/PgDn Scroll  [ ] Columns"[:width],
            curses.A_DIM,
        )
        screen.refresh()
