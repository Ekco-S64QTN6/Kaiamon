from __future__ import annotations

import curses

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


def test_escape_discards_setup_changes() -> None:
    screen = FakeScreen([ord(" "), 27])
    ui = FakeUI(("PID", "COMMAND"), screen)

    ProcessSetup().edit(ui)

    assert ui.columns == ("PID", "COMMAND")
    assert ui.status == "Column setup cancelled"
    assert screen.timeouts == [-1, 100]


def test_setup_never_allows_hiding_the_final_column() -> None:
    screen = FakeScreen([ord(" "), 10])
    ui = FakeUI(("PID",), screen)

    ProcessSetup().edit(ui)

    assert ui.columns == ("PID",)
    assert any("At least one column" in line for line in screen.writes)
