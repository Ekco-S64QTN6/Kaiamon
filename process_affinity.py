"""Interactive CPU-affinity editor for one selected process."""
from __future__ import annotations

import curses
from typing import Any

from process_actions import (
    get_available_cpus,
    get_process_affinity,
    set_process_affinity,
)
from process_core import ProcessInfo


class ProcessAffinityEditor:
    """Stage a process CPU mask and require confirmation before applying it."""

    def edit(self, ui: Any, process: ProcessInfo) -> None:
        screen = ui._stdscr
        if screen is None:
            return

        try:
            original = get_process_affinity(process.pid)
            cpu_ids = sorted(get_available_cpus() | original)
        except (OSError, ValueError, PermissionError) as exc:
            ui.status = f"Could not read CPU affinity for PID {process.pid}: {exc}"
            return

        enabled = set(original)
        selected = 0
        message = "Space: toggle CPU   Enter: review/apply   Esc: cancel"
        screen.timeout(-1)
        try:
            while True:
                self._draw(
                    screen, process, cpu_ids, enabled, selected, original, message,
                )
                key = screen.getch()

                if key == 27:
                    ui.status = f"CPU affinity edit cancelled for PID {process.pid}"
                    return
                if key in (10, 13, curses.KEY_ENTER):
                    requested = frozenset(enabled)
                    if requested == original:
                        ui.status = f"CPU affinity unchanged for PID {process.pid}"
                        return
                    if not self._same_process(ui, process):
                        ui.status = (
                            f"PID {process.pid} exited or was reused; "
                            "CPU affinity was not changed"
                        )
                        return
                    cpu_text = ",".join(str(cpu) for cpu in sorted(requested))
                    answer = ui._prompt(
                        f"Apply CPUs [{cpu_text}] to PID {process.pid}? [y/N]: "
                    )
                    if answer.casefold() != "y":
                        ui.status = f"CPU affinity change cancelled for PID {process.pid}"
                        return
                    if not self._same_process(ui, process):
                        ui.status = (
                            f"PID {process.pid} exited or was reused; "
                            "CPU affinity was not changed"
                        )
                        return
                    try:
                        effective = set_process_affinity(process.pid, requested)
                    except (OSError, ValueError, PermissionError) as exc:
                        ui.status = f"CPU affinity failed for PID {process.pid}: {exc}"
                    else:
                        effective_text = ", ".join(
                            str(cpu) for cpu in sorted(effective)
                        )
                        if effective != requested:
                            ui.status = (
                                f"PID {process.pid} effective CPUs: {effective_text} "
                                "(restricted by kernel/cpuset)"
                            )
                        else:
                            ui.status = (
                                f"PID {process.pid} affinity set to CPUs: {effective_text}"
                            )
                    return
                if key in (curses.KEY_UP, ord("k")):
                    selected = max(0, selected - 1)
                elif key in (curses.KEY_DOWN, ord("j")):
                    selected = min(len(cpu_ids) - 1, selected + 1)
                elif key == curses.KEY_HOME:
                    selected = 0
                elif key == curses.KEY_END:
                    selected = len(cpu_ids) - 1
                elif key == curses.KEY_PPAGE:
                    selected = max(0, selected - max(1, screen.getmaxyx()[0] - 7))
                elif key == curses.KEY_NPAGE:
                    selected = min(
                        len(cpu_ids) - 1,
                        selected + max(1, screen.getmaxyx()[0] - 7),
                    )
                elif key == ord(" "):
                    cpu = cpu_ids[selected]
                    if cpu in enabled:
                        if len(enabled) == 1:
                            message = "At least one CPU must remain enabled"
                        else:
                            enabled.remove(cpu)
                            message = f"Disabled CPU {cpu}"
                    else:
                        enabled.add(cpu)
                        message = f"Enabled CPU {cpu}"
        finally:
            screen.timeout(100)

    @staticmethod
    def _same_process(ui: Any, process: ProcessInfo) -> bool:
        """Avoid applying a mask to a new process that reused the selected PID."""
        snapshot = ui.collector.get_snapshot()
        current = next(
            (item for item in snapshot.processes if item.pid == process.pid),
            None,
        )
        return current is not None and current.start_ticks == process.start_ticks

    @staticmethod
    def _draw(
        screen: Any,
        process: ProcessInfo,
        cpu_ids: list[int],
        enabled: set[int],
        selected: int,
        original: frozenset[int],
        message: str,
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

        write(0, f"CPU AFFINITY — PID {process.pid}", curses.A_BOLD)
        write(1, process.command[:max(0, width - 1)])
        write(2, "Space: toggle CPU   Up/Down: select   PgUp/PgDn: scroll")
        visible_rows = max(1, height - 6)
        first = max(0, min(
            selected - visible_rows // 2,
            len(cpu_ids) - visible_rows,
        ))
        for offset, cpu in enumerate(cpu_ids[first:first + visible_rows]):
            marker = "[x]" if cpu in enabled else "[ ]"
            original_marker = "*" if cpu in original else " "
            attr = curses.A_REVERSE | curses.A_BOLD if first + offset == selected else 0
            write(4 + offset, f"{marker} CPU {cpu:<4} {original_marker}", attr)
        write(
            height - 2,
            f"Staged mask: {len(enabled)} CPU(s)  |  {message}",
            curses.A_BOLD,
        )
        write(height - 1, "Enter review/apply (confirmation required)   Esc cancel")
        screen.refresh()
