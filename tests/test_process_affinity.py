from __future__ import annotations

import curses

import pytest

from process_actions import (
    get_available_cpus,
    get_process_affinity,
    set_process_affinity,
)
from process_affinity import ProcessAffinityEditor
from process_core import ProcessInfo, ProcessSnapshot


class FakeScreen:
    def __init__(self, keys: list[int], height: int = 18, width: int = 80) -> None:
        self.keys = list(keys)
        self.height = height
        self.width = width
        self.timeouts: list[int] = []
        self.lines: list[str] = []

    def timeout(self, value: int) -> None:
        self.timeouts.append(value)

    def erase(self) -> None:
        pass

    def getmaxyx(self) -> tuple[int, int]:
        return self.height, self.width

    def addnstr(self, _y: int, _x: int, value: str, _count: int, _attr: int = 0) -> None:
        self.lines.append(value)

    def refresh(self) -> None:
        pass

    def getch(self) -> int:
        return self.keys.pop(0)


class FakeCollector:
    def __init__(self, snapshot: ProcessSnapshot) -> None:
        self.snapshot = snapshot

    def get_snapshot(self) -> ProcessSnapshot:
        return self.snapshot


class FakeUI:
    def __init__(
        self, process: ProcessInfo, screen: FakeScreen, answer: str = "y",
    ) -> None:
        self._stdscr = screen
        self.collector = FakeCollector(ProcessSnapshot(processes=(process,)))
        self.answer = answer
        self.status = ""

    def _prompt(self, _prompt: str) -> str:
        return self.answer


def _process(pid: int = 321, start_ticks: int = 100) -> ProcessInfo:
    return ProcessInfo(
        pid, 1, "tester", 20, 0, 4096, 1024, 128, "S",
        0.0, 0.0, 1.0, "python worker.py", start_ticks,
    )


def test_get_and_set_process_affinity_use_kernel_mask(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = {1, 3}
    observed: list[tuple[int, set[int]]] = []

    def fake_set(pid: int, cpus: set[int]) -> None:
        observed.append((pid, set(cpus)))
        current.clear()
        current.update(cpus)

    monkeypatch.setattr("process_actions.os.getpid", lambda: 999)
    monkeypatch.setattr(
        "process_actions.os.sched_getaffinity",
        lambda pid: {0, 2, 4} if pid == 0 else current,
    )
    monkeypatch.setattr("process_actions.os.sched_setaffinity", fake_set)

    assert get_available_cpus() == frozenset({0, 2, 4})
    assert get_process_affinity(321) == frozenset({1, 3})
    assert set_process_affinity(321, {2, 4}) == frozenset({2, 4})
    assert observed == [(321, {2, 4})]


@pytest.mark.parametrize(
    "cpus, message",
    [
        ([], "at least one CPU"),
        (set(), "at least one CPU"),
        ({True}, "non-negative integers"),
        ({-1}, "non-negative integers"),
        ({"1"}, "non-negative integers"),
        ("1,2", "iterable of CPU IDs"),
    ],
)
def test_set_process_affinity_rejects_invalid_masks(
    monkeypatch: pytest.MonkeyPatch, cpus: object, message: str,
) -> None:
    monkeypatch.setattr("process_actions.os.getpid", lambda: 999)
    monkeypatch.setattr(
        "process_actions.os.sched_setaffinity",
        lambda *_args: pytest.fail("invalid CPU mask reached the system call"),
    )

    with pytest.raises(ValueError, match=message):
        set_process_affinity(321, cpus)  # type: ignore[arg-type]


@pytest.mark.parametrize("pid", [1, 999])
def test_affinity_actions_reject_system_and_monitor_pids(
    monkeypatch: pytest.MonkeyPatch, pid: int,
) -> None:
    monkeypatch.setattr("process_actions.os.getpid", lambda: 999)
    monkeypatch.setattr(
        "process_actions.os.sched_getaffinity",
        lambda _pid: pytest.fail("unsafe PID reached sched_getaffinity"),
    )

    with pytest.raises(ValueError):
        get_process_affinity(pid)


def test_editor_confirms_and_applies_staged_cpu_mask(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("process_affinity.get_available_cpus", lambda: frozenset({0, 1, 2}))
    monkeypatch.setattr("process_affinity.get_process_affinity", lambda _pid: frozenset({0, 1}))
    applied: list[tuple[int, frozenset[int]]] = []

    def fake_set(pid: int, cpus: set[int] | frozenset[int]) -> frozenset[int]:
        applied.append((pid, frozenset(cpus)))
        return frozenset(cpus)

    monkeypatch.setattr("process_affinity.set_process_affinity", fake_set)
    screen = FakeScreen([curses.KEY_DOWN, ord(" "), 10])
    ui = FakeUI(_process(), screen)

    ProcessAffinityEditor().edit(ui, _process())

    assert applied == [(321, frozenset({0}))]
    assert ui.status == "PID 321 affinity set to CPUs: 0"
    assert screen.timeouts == [-1, 100]
    assert any("[x] CPU 0" in line for line in screen.lines)


def test_editor_default_no_confirmation_does_not_change_affinity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("process_affinity.get_available_cpus", lambda: frozenset({0, 1}))
    monkeypatch.setattr("process_affinity.get_process_affinity", lambda _pid: frozenset({0, 1}))
    monkeypatch.setattr(
        "process_affinity.set_process_affinity",
        lambda *_args: pytest.fail("declined affinity change reached system call"),
    )
    ui = FakeUI(_process(), FakeScreen([curses.KEY_DOWN, ord(" "), 10]), answer="n")

    ProcessAffinityEditor().edit(ui, _process())

    assert ui.status == "CPU affinity change cancelled for PID 321"


def test_editor_refuses_to_remove_the_final_cpu(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("process_affinity.get_available_cpus", lambda: frozenset({0, 1}))
    monkeypatch.setattr("process_affinity.get_process_affinity", lambda _pid: frozenset({0}))
    ui = FakeUI(_process(), FakeScreen([ord(" "), 10]))

    ProcessAffinityEditor().edit(ui, _process())

    assert ui.status == "CPU affinity unchanged for PID 321"
    assert any("At least one CPU must remain enabled" in line for line in ui._stdscr.lines)


def test_editor_cancel_and_stale_pid_never_apply_affinity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("process_affinity.get_available_cpus", lambda: frozenset({0, 1}))
    monkeypatch.setattr("process_affinity.get_process_affinity", lambda _pid: frozenset({0, 1}))
    monkeypatch.setattr(
        "process_affinity.set_process_affinity",
        lambda *_args: pytest.fail("cancelled or stale target reached system call"),
    )
    original = _process()
    cancelled = FakeUI(original, FakeScreen([27]))
    ProcessAffinityEditor().edit(cancelled, original)
    assert cancelled.status == "CPU affinity edit cancelled for PID 321"

    stale = FakeUI(original, FakeScreen([curses.KEY_DOWN, ord(" "), 10]))
    stale.collector.snapshot = ProcessSnapshot(processes=(_process(start_ticks=999),))
    ProcessAffinityEditor().edit(stale, original)
    assert "exited or was reused" in stale.status

def test_editor_reports_kernel_or_cpuset_restrictions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("process_affinity.get_available_cpus", lambda: frozenset({0, 1, 2}))
    monkeypatch.setattr("process_affinity.get_process_affinity", lambda _pid: frozenset({0, 1}))
    monkeypatch.setattr(
        "process_affinity.set_process_affinity",
        lambda _pid, _cpus: frozenset({0}),
    )
    ui = FakeUI(
        _process(),
        FakeScreen([curses.KEY_DOWN, ord(" "), curses.KEY_DOWN, ord(" "), 10]),
    )

    ProcessAffinityEditor().edit(ui, _process())

    assert "effective CPUs: 0" in ui.status
    assert "restricted by kernel/cpuset" in ui.status


def test_editor_does_not_prompt_when_mask_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("process_affinity.get_available_cpus", lambda: frozenset({0, 1}))
    monkeypatch.setattr("process_affinity.get_process_affinity", lambda _pid: frozenset({0, 1}))
    monkeypatch.setattr(
        "process_affinity.set_process_affinity",
        lambda *_args: pytest.fail("unchanged mask reached system call"),
    )
    ui = FakeUI(_process(), FakeScreen([10]))

    ui._prompt = lambda _prompt: pytest.fail("unchanged mask requested confirmation")  # type: ignore[method-assign]
    ProcessAffinityEditor().edit(ui, _process())

    assert ui.status == "CPU affinity unchanged for PID 321"
