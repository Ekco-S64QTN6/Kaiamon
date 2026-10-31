"""Interactive main-thread curses setup for process-table columns."""
from __future__ import annotations

import curses
from typing import Any

from process_config import DEFAULT_COLUMNS


class ProcessSetup:
    """Edit visible columns and their order without restarting the monitor."""

    def edit(self, ui: Any) -> None:
        """Open the column editor; Enter applies and Escape discards changes."""
        screen = ui._stdscr
        if screen is None:
            return

        order = list(ui.columns)
        order.extend(column for column in DEFAULT_COLUMNS if column not in order)
        enabled = set(ui.columns)
        selected = 0
        message = "Space: show/hide   Left/Right: reorder   Enter: apply   Esc: cancel"

        screen.timeout(-1)
        try:
            while True:
                self._draw(screen, order, enabled, selected, message)
                key = screen.getch()

                if key == 27:
                    ui.status = "Column setup cancelled"
                    return
                if key in (10, 13, curses.KEY_ENTER):
                    columns = tuple(column for column in order if column in enabled)
                    if columns:
                        ui.columns = columns
                        ui.horizontal_scroll = 0
                        ui.selected = 0
                        ui.scroll = 0
                        ui.status = "Columns configured: " + ", ".join(columns)
                    return
                if key in (curses.KEY_UP, ord("k")):
                    selected = max(0, selected - 1)
                elif key in (curses.KEY_DOWN, ord("j")):
                    selected = min(len(order) - 1, selected + 1)
                elif key == curses.KEY_HOME:
                    selected = 0
                elif key == curses.KEY_END:
                    selected = len(order) - 1
                elif key == ord(" "):
                    column = order[selected]
                    if column in enabled:
                        if len(enabled) == 1:
                            message = "At least one column must remain visible"
                        else:
                            enabled.remove(column)
                            message = f"Hidden {column}"
                    else:
                        enabled.add(column)
                        message = f"Showing {column}"
                elif key == curses.KEY_LEFT:
                    if selected > 0:
                        order[selected - 1], order[selected] = (
                            order[selected], order[selected - 1],
                        )
                        selected -= 1
                        message = f"Moved {order[selected]} left"
                elif key == curses.KEY_RIGHT:
                    if selected < len(order) - 1:
                        order[selected + 1], order[selected] = (
                            order[selected], order[selected + 1],
                        )
                        selected += 1
                        message = f"Moved {order[selected]} right"
        finally:
            # Match the monitor's normal non-blocking frame/input cadence.
            screen.timeout(100)

    @staticmethod
    def _draw(
        screen: Any, order: list[str], enabled: set[str],
        selected: int, message: str,
    ) -> None:
        screen.erase()
        height, width = screen.getmaxyx()
        if height <= 0 or width <= 0:
            return

        def write(y: int, value: str, attr: int = 0) -> None:
            if 0 <= y < height and width > 1:
                try:
                    screen.addnstr(y, 0, value, width - 1, attr)
                except curses.error:
                    pass

        write(0, "Kaiamon setup — process columns", curses.A_BOLD)
        write(1, "Up/Down or j/k: select   Home/End: jump")
        write(2, "Space: show/hide   Left/Right: reorder")
        visible_rows = max(1, height - 6)
        first = max(0, min(
            selected - visible_rows // 2,
            len(order) - visible_rows,
        ))
        for offset, column in enumerate(order[first:first + visible_rows]):
            marker = "[x]" if column in enabled else "[ ]"
            attr = curses.A_REVERSE | curses.A_BOLD if first + offset == selected else 0
            write(4 + offset, f"{marker} {column}", attr)
        write(height - 2, message, curses.A_BOLD)
        write(height - 1, "Enter apply   Esc cancel")
        screen.refresh()
