from __future__ import annotations

import pytest

from process_core import ProcessSnapshot
from process_monitor import ProcessMonitorUI


class FakeScreen:
    def __init__(self, height: int = 12, width: int = 80) -> None:
        self.height = height
        self.width = width
        self.lines: list[tuple[int, int, str, int]] = []
        self.erased = False
        self.refreshed = False

    def erase(self) -> None:
        self.erased = True

    def getmaxyx(self) -> tuple[int, int]:
        return self.height, self.width

    def addnstr(self, y: int, x: int, value: str, length: int, _attr: int) -> None:
        self.lines.append((y, x, value[:length], length))

    def refresh(self) -> None:
        self.refreshed = True


def test_renderer_reports_minimum_terminal_size(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("process_render.curses.color_pair", lambda value: value)
    ui = ProcessMonitorUI()
    screen = FakeScreen(height=7, width=80)
    ui._stdscr = screen

    ui.renderer.draw(ui, ProcessSnapshot())

    assert screen.erased
    assert any("Terminal too small" in line for _y, _x, line, _n in screen.lines)
    assert screen.refreshed


def test_renderer_draws_snapshot_header_and_empty_process_footer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("process_render.curses.color_pair", lambda value: value)
    ui = ProcessMonitorUI()
    screen = FakeScreen()
    ui._stdscr = screen

    ui.renderer.draw(ui, ProcessSnapshot())

    rendered = "\n".join(line for _y, _x, line, _n in screen.lines)
    assert screen.erased
    assert "CPU" in rendered
    assert "Mem" in rendered
    assert "PID" in rendered
    assert "0 processes" in rendered
    assert screen.refreshed
