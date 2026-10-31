from __future__ import annotations

import pytest

from process_core import DiskIORate, NetworkIORate, ProcessInfo, ProcessSnapshot, ThreadInfo
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


def test_renderer_horizontal_scroll_reveals_default_command_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("process_render.curses.color_pair", lambda value: value)
    ui = ProcessMonitorUI()
    screen = FakeScreen(width=80)
    ui._stdscr = screen
    command = "daemon-command-that-exceeds-width"
    process = ProcessInfo(
        321, 1, "tester", 20, 0, 4096, 1024, 128, "S",
        0.0, 0.0, 1.0, command, 10,
    )
    snapshot = ProcessSnapshot(processes=(process,))

    ui.renderer.draw(ui, snapshot)
    first_header = next(line for y, _x, line, _n in screen.lines if y == 3)
    first_row = next(line for y, _x, line, _n in screen.lines if y == 4)
    assert "COMMAND" in first_header
    assert command[:24] not in first_row
    assert len(first_row) <= screen.width
    assert ui.horizontal_scroll == 0

    screen.lines.clear()
    ui.horizontal_scroll = 999
    ui.renderer.draw(ui, snapshot)
    scrolled_header = next(line for y, _x, line, _n in screen.lines if y == 3)
    scrolled_row = next(line for y, _x, line, _n in screen.lines if y == 4)

    assert ui.horizontal_scroll == 15
    assert "COMMAND" in scrolled_header
    assert command[:24] in scrolled_row
    assert len(scrolled_header) <= screen.width
    assert len(scrolled_row) <= screen.width

def test_renderer_keeps_reordered_columns_in_fixed_width_layout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("process_render.curses.color_pair", lambda value: value)
    ui = ProcessMonitorUI(columns=("COMMAND", "PID", "CPU"))
    screen = FakeScreen(width=60)
    ui._stdscr = screen

    ui.renderer.draw(ui, ProcessSnapshot())

    header = next(line for y, _x, line, _n in screen.lines if y == 3)
    assert header.rstrip().startswith("COMMAND")
    assert "PID" in header
    assert "CPU%" in header
    assert ui.horizontal_scroll == 0

def test_renderer_uses_persisted_custom_column_widths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("process_render.curses.color_pair", lambda value: value)
    ui = ProcessMonitorUI(
        columns=("PID", "COMMAND"),
        column_widths={"PID": 10, "COMMAND": 40},
    )
    screen = FakeScreen(width=80)
    ui._stdscr = screen
    process = ProcessInfo(
        321, 1, "tester", 20, 0, 4096, 1024, 128, "S",
        0.0, 0.0, 1.0, "custom-width-command", 10,
    )

    ui.renderer.draw(ui, ProcessSnapshot(processes=(process,)))

    header = next(line for y, _x, line, _n in screen.lines if y == 3)
    row = next(line for y, _x, line, _n in screen.lines if y == 4)
    assert header.startswith("       PID")
    assert header[11:].startswith("COMMAND")
    assert row.startswith("       321")
    assert "custom-width-command" in row

def test_renderer_displays_process_io_rates_and_unavailable_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("process_render.curses.color_pair", lambda value: value)
    ui = ProcessMonitorUI(columns=("PID", "IO_READ", "IO_WRITE"))
    screen = FakeScreen(width=80)
    ui._stdscr = screen
    process = ProcessInfo(
        321, 1, "tester", 20, 0, 4096, 1024, 128, "S",
        0.0, 0.0, 1.0, "worker", 10,
        io_read_rate=2048.0, io_write_rate=None,
    )

    ui.renderer.draw(ui, ProcessSnapshot(processes=(process,)))

    header = next(line for y, _x, line, _n in screen.lines if y == 3)
    row = next(line for y, _x, line, _n in screen.lines if y == 4)
    assert "IOR/s" in header
    assert "IOW/s" in header
    assert "2.0K/s" in row
    assert "—" in row

def test_renderer_draws_system_io_panel_with_device_and_interface_rates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("process_render.curses.color_pair", lambda value: value)
    ui = ProcessMonitorUI()
    ui.system_io_mode = True
    screen = FakeScreen(height=18, width=100)
    ui._stdscr = screen
    snapshot = ProcessSnapshot(
        disk_io=(DiskIORate("nvme0n1", 2048.0, 4096.0),),
        network_io=(NetworkIORate("enp1s0", 1024.0, 512.0),),
    )

    ui.renderer.draw(ui, snapshot)

    rendered = "\n".join(line for _y, _x, line, _n in screen.lines)
    assert "SYSTEM I/O" in rendered
    assert "nvme0n1" in rendered
    assert "enp1s0" in rendered
    assert "2.0K/s" in rendered
    assert "4.0K/s" in rendered
    assert "1.0K/s" in rendered
    assert "512B/s" in rendered
    assert "F3/Esc Process table" in rendered


def test_renderer_shows_unavailable_system_io_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("process_render.curses.color_pair", lambda value: value)
    ui = ProcessMonitorUI()
    ui.system_io_mode = True
    screen = FakeScreen(height=18, width=100)
    ui._stdscr = screen

    ui.renderer.draw(ui, ProcessSnapshot())

    rendered = "\n".join(line for _y, _x, line, _n in screen.lines)
    assert "No disk device counters available" in rendered
    assert "No network interface counters available" in rendered

def test_renderer_displays_selected_process_thread_cpu_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("process_render.curses.color_pair", lambda value: value)
    ui = ProcessMonitorUI()
    ui.thread_panel_mode = True
    ui.thread_target_pid = 321
    ui.thread_target_start_ticks = 10
    ui._stdscr = FakeScreen(height=18, width=100)
    snapshot = ProcessSnapshot(
        processes=(ProcessInfo(
            321, 1, "tester", 20, 0, 4096, 1024, 128, "S",
            0.0, 0.0, 3.0, "python worker.py", 10,
        ),),
        threads=(
            ThreadInfo(321, 321, "S", 20, 0, 12.5, 2.0, "python", 10),
            ThreadInfo(322, 321, "R", 20, 0, 55.0, 4.0, "worker", 20),
        ),
    )

    ui.renderer.draw(ui, snapshot)

    rendered = "\n".join(line for _y, _x, line, _n in ui._stdscr.lines)
    assert "THREADS OF PID 321" in rendered
    assert "python worker.py" in rendered
    assert "322" in rendered
    assert "55.0" in rendered
    assert "worker" in rendered
    assert "2 threads for PID 321" in rendered
    assert "F4/Esc Process table" in rendered
    assert "r Renice thread" in rendered


def test_renderer_handles_exited_thread_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("process_render.curses.color_pair", lambda value: value)
    ui = ProcessMonitorUI()
    ui.thread_panel_mode = True
    ui.thread_target_pid = 999
    ui._stdscr = FakeScreen(height=18, width=100)

    ui.renderer.draw(ui, ProcessSnapshot())

    rendered = "\n".join(line for _y, _x, line, _n in ui._stdscr.lines)
    assert "THREADS OF PID 999" in rendered
    assert "No readable threads for this process yet." in rendered
