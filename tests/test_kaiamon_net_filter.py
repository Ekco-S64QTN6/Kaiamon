from __future__ import annotations

import importlib
import sys
import threading
import types
from types import SimpleNamespace

import pytest


class _AccessDenied(Exception):
    pass


class _NoSuchProcess(Exception):
    pass


class _ZombieProcess(Exception):
    pass


@pytest.fixture
def net_module(monkeypatch):
    """Load the optional network monitor without requiring host psutil data."""
    fake_psutil = types.ModuleType("psutil")
    fake_psutil.AccessDenied = _AccessDenied
    fake_psutil.NoSuchProcess = _NoSuchProcess
    fake_psutil.ZombieProcess = _ZombieProcess
    fake_psutil.net_if_addrs = lambda: {}
    fake_psutil.net_if_stats = lambda: {}
    fake_psutil.net_io_counters = lambda **_kwargs: {}
    fake_psutil.net_connections = lambda **_kwargs: []
    fake_psutil.Process = lambda _pid: SimpleNamespace(name=lambda: "?")
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)
    sys.modules.pop("kaiamon_net", None)
    module = importlib.import_module("kaiamon_net")
    yield module
    sys.modules.pop("kaiamon_net", None)


def _plasma_line(interface: str = "wlan3") -> str:
    return (
        "2026-11-10T16:00:00-06:00 workstation "
        f'plasmashell[1234]: Wireless scan on "{interface}" failed: '
        '"Scanning not allowed while unavailable"'
    )


def _nmcli_result(value: str, *, returncode: int = 0):
    return SimpleNamespace(
        returncode=returncode,
        stdout=f"GENERAL.NM-MANAGED: {value}\n",
        stderr="",
    )


def test_filter_suppresses_only_confirmed_unmanaged_plasma_adapter(
    net_module, monkeypatch
):
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return _nmcli_result("no")

    monkeypatch.setattr(net_module.subprocess, "run", runner)
    collector = net_module.NetworkLogCollector(threading.Event())

    assert collector._parse_line(_plasma_line("wlan3")) is None
    assert calls == [
        (
            ["nmcli", "-f", "GENERAL.NM-MANAGED", "device", "show", "wlan3"],
            {
                "capture_output": True,
                "text": True,
                "timeout": net_module.NMCLI_TIMEOUT_SECONDS,
                "check": False,
            },
        )
    ]


def test_filter_preserves_plasma_rejection_for_managed_adapter(
    net_module, monkeypatch
):
    calls = []

    def runner(command, **_kwargs):
        calls.append(command)
        return _nmcli_result("yes")

    monkeypatch.setattr(net_module.subprocess, "run", runner)
    entry = net_module.NetworkLogCollector(threading.Event())._parse_line(
        _plasma_line("wlan3")
    )

    assert entry is not None
    assert "Wireless scan on" in entry.message
    assert calls == [
        ["nmcli", "-f", "GENERAL.NM-MANAGED", "device", "show", "wlan3"]
    ]


def test_filter_is_specific_to_the_plasma_unavailable_scan_message(
    net_module, monkeypatch
):
    calls = []
    monkeypatch.setattr(
        net_module.subprocess,
        "run",
        lambda *args, **kwargs: calls.append(args) or _nmcli_result("no"),
    )
    collector = net_module.NetworkLogCollector(threading.Event())

    other_plasma_error = (
        "2026-11-10T16:00:00-06:00 workstation plasmashell[1234]: "
        'Wireless scan on "wlan3" failed: "Hardware is unavailable"'
    )
    non_plasma_process = (
        "2026-11-10T16:00:00-06:00 workstation systemd[1]: "
        'Wireless scan on "wlan3" failed: '
        '"Scanning not allowed while unavailable"'
    )

    assert collector._parse_line(other_plasma_error) is not None
    assert collector._parse_line(non_plasma_process) is not None
    assert calls == []


def test_filter_is_general_for_plasma_nm_and_any_interface(
    net_module, monkeypatch
):
    calls = []

    def runner(command, **_kwargs):
        calls.append(command)
        return _nmcli_result("no")

    monkeypatch.setattr(net_module.subprocess, "run", runner)
    collector = net_module.NetworkLogCollector(threading.Event())
    line = (
        "2026-11-10T16:00:00-06:00 workstation plasma-nm[567]: "
        'Wireless scan on "wlan7" failed: '
        '"Scanning not allowed while unavailable"'
    )

    assert collector._parse_line(line) is None
    assert calls == [
        ["nmcli", "-f", "GENERAL.NM-MANAGED", "device", "show", "wlan7"]
    ]


def test_filter_caches_nm_state_for_repeated_applet_polling(net_module, monkeypatch):
    calls = []

    def runner(command, **_kwargs):
        calls.append(command)
        return _nmcli_result("no")

    monkeypatch.setattr(net_module.subprocess, "run", runner)
    collector = net_module.NetworkLogCollector(threading.Event())

    assert collector._parse_line(_plasma_line("wlan3")) is None
    assert collector._parse_line(_plasma_line("wlan3")) is None
    assert len(calls) == 1


@pytest.mark.parametrize("failure", ["missing", "timeout"])
def test_networkmanager_probe_errors_do_not_suppress_messages(
    net_module, monkeypatch, failure
):
    import subprocess

    error = (
        FileNotFoundError("nmcli not installed")
        if failure == "missing"
        else subprocess.TimeoutExpired("nmcli", 2.0)
    )

    def runner(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(net_module.subprocess, "run", runner)
    entry = net_module.NetworkLogCollector(threading.Event())._parse_line(
        _plasma_line("wlan3")
    )

    assert entry is not None
