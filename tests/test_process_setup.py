from __future__ import annotations

import curses

from process_config import AVAILABLE_COLUMNS, DEFAULT_COLUMN_WIDTHS, MAX_COLUMN_WIDTH, MIN_COLUMN_WIDTH
from process_setup import ProcessSetup


class FakeScreen:
    def __init__(self, keys: list[int], height: int = 24, width: int = 80) -> None:
        self.keys = list(keys)
        self.height = height
        self.width = width
        self.timeouts: list[int] = []
        self.writes: list[str] = []

    def timeout(self, value: int) -> None:
        self.timeouts.append(value)

    def erase(self) -> None:
        pass

    def getmaxyx(self) -> tuple[int, int]:
        return self.height, self.width

    def addnstr(self, _y: int, _x: int, value: str, _count: int, _attr: int = 0) -> None:
        self.writes.append(value)

    def refresh(self) -> None:
        pass

    def getch(self) -> int:
        return self.keys.pop(0)


class FakeUI:
    def __init__(self, columns: tuple[str, ...], screen: FakeScreen) -> None:
        self.columns = columns
        self.column_widths = dict(DEFAULT_COLUMN_WIDTHS)
        self._stdscr = screen
        self.horizontal_scroll = 16
        self.selected = 4
        self.scroll = 3
        self.status = ""


def test_setup_toggles_and_reorders_columns_then_resets_table_position() -> None:
    screen = FakeScreen([
        curses.KEY_DOWN, curses.KEY_RIGHT, ord(" "), 10,
    ])
    ui = FakeUI(("PID", "CPU", "COMMAND"), screen)

    ProcessSetup().edit(ui)

    assert ui.columns == ("PID", "COMMAND")
    assert ui.horizontal_scroll == 0
    assert ui.selected == 0
    assert ui.scroll == 0
    assert ui.status == "Columns configured: PID, COMMAND"
    assert screen.timeouts == [-1, 100]


def test_setup_can_enable_a_previously_hidden_column() -> None:
    screen = FakeScreen([curses.KEY_DOWN, ord(" "), 10])
    ui = FakeUI(("PID",), screen)

    ProcessSetup().edit(ui)

    assert ui.columns == ("PID", "USER")


def test_setup_can_enable_optional_io_columns() -> None:
    screen = FakeScreen([curses.KEY_DOWN] * len(AVAILABLE_COLUMNS[:12]) + [ord(" "), 10])
    ui = FakeUI(("PID",), screen)

    ProcessSetup().edit(ui)

    assert ui.columns == ("PID", "IO_READ")


def test_setup_can_enable_cgroup_column() -> None:
    screen = FakeScreen([curses.KEY_DOWN] * (len(AVAILABLE_COLUMNS) - 1) + [ord(" "), 10])
    ui = FakeUI(("PID",), screen)

    ProcessSetup().edit(ui)

    assert ui.columns == ("PID", "CGROUP")
    assert ui.column_widths["CGROUP"] == 40


def test_setup_adjusts_widths_and_applies_them_with_layout() -> None:
    screen = FakeScreen([ord("+"), ord("+"), curses.KEY_DOWN, ord("-"), 10])
    ui = FakeUI(("PID", "USER"), screen)

    ProcessSetup().edit(ui)

    assert ui.column_widths["PID"] == 8
    assert ui.column_widths["USER"] == 7
    assert ui.status == "Column layout and widths configured"
    assert screen.timeouts == [-1, 100]
    assert any("8 chars" in line for line in screen.writes)


def test_setup_width_adjustment_respects_minimum_and_maximum() -> None:
    screen = FakeScreen([ord("-"), curses.KEY_DOWN, ord("+"), 10])
    ui = FakeUI(("PID", "USER"), screen)
    ui.column_widths["PID"] = MIN_COLUMN_WIDTH
    ui.column_widths["USER"] = MAX_COLUMN_WIDTH

    ProcessSetup().edit(ui)

    assert ui.column_widths["PID"] == MIN_COLUMN_WIDTH
    assert ui.column_widths["USER"] == MAX_COLUMN_WIDTH


def test_escape_discards_column_and_width_changes() -> None:
    screen = FakeScreen([ord(" "), ord("+"), 27])
    ui = FakeUI(("PID", "COMMAND"), screen)
    original_widths = dict(ui.column_widths)

    ProcessSetup().edit(ui)

    assert ui.columns == ("PID", "COMMAND")
    assert ui.column_widths == original_widths
    assert ui.status == "Column setup cancelled"
    assert screen.timeouts == [-1, 100]


def test_setup_never_allows_hiding_the_final_column() -> None:
    screen = FakeScreen([ord(" "), 10])
    ui = FakeUI(("PID",), screen)

    ProcessSetup().edit(ui)

    assert ui.columns == ("PID",)
    assert any("At least one column" in line for line in screen.writes)
