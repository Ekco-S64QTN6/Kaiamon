"""Integration tests that verify curses restores a real pseudo-terminal."""
from __future__ import annotations

import fcntl
import os
import pty
import signal
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="PTY terminal integration tests require Linux",
)


def _start_in_pty(mode: str) -> tuple[subprocess.Popen[bytes], int, int, list[object]]:
    master_fd, slave_fd = pty.openpty()
    fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 100, 0, 0))
    original = termios.tcgetattr(slave_fd)
    script = """
import sys
from process_monitor import ProcessMonitorUI
ui = ProcessMonitorUI()
mode = sys.argv[1]
if mode == 'exception':
    def fail(_snapshot):
        import time
        time.sleep(0.25)  # Let the parent observe cbreak before raising.
        raise RuntimeError('deliberate PTY restoration test')
    ui._draw = fail
ui.run()
"""
    env = os.environ.copy()
    env["TERM"] = "xterm-256color"
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    process = subprocess.Popen(
        [sys.executable, "-c", script, mode],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        cwd=ROOT,
        env=env,
        close_fds=True,
    )
    return process, master_fd, slave_fd, original


def _wait_until_cbreak(process: subprocess.Popen[bytes], slave_fd: int) -> None:
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError("process exited before curses entered cbreak mode")
        attrs = termios.tcgetattr(slave_fd)
        if not (attrs[3] & termios.ICANON) and not (attrs[3] & termios.ECHO):
            return
        time.sleep(0.02)
    raise AssertionError("curses did not enter cbreak/noecho mode within 5 seconds")


def _assert_restored(
    process: subprocess.Popen[bytes], slave_fd: int,
    original: list[object], *, expected_code: int,
) -> None:
    return_code = process.wait(timeout=5.0)
    assert return_code == expected_code
    restored = termios.tcgetattr(slave_fd)
    assert restored == original


def test_curses_restores_terminal_after_q_exit() -> None:
    process, master_fd, slave_fd, original = _start_in_pty("quit")
    try:
        _wait_until_cbreak(process, slave_fd)
        os.write(master_fd, b"q")
        _assert_restored(process, slave_fd, original, expected_code=0)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)
        os.close(master_fd)
        os.close(slave_fd)


def test_curses_restores_terminal_after_sigterm() -> None:
    process, master_fd, slave_fd, original = _start_in_pty("sigterm")
    try:
        _wait_until_cbreak(process, slave_fd)
        process.send_signal(signal.SIGTERM)
        _assert_restored(process, slave_fd, original, expected_code=0)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)
        os.close(master_fd)
        os.close(slave_fd)


def test_curses_restores_terminal_after_uncaught_ui_exception() -> None:
    process, master_fd, slave_fd, original = _start_in_pty("exception")
    try:
        _wait_until_cbreak(process, slave_fd)
        _assert_restored(process, slave_fd, original, expected_code=1)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)
        os.close(master_fd)
        os.close(slave_fd)
