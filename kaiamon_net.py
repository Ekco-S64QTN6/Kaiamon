#!/usr/bin/env python3
"""
Kaiamon Net: Network & Firewall Traffic Monitor
================================================

Standalone curses TUI for real-time network and firewall monitoring,
built on the same snapshot-based rendering architecture as kaiamon.py.

Architecture:
  - Frozen NetMonSnapshot dataclass — UI renders only from immutable objects
  - CollectorManager — centralized lifecycle for all collector threads
  - Per-collector locks — no global RLock bottleneck
  - Signal handlers + atexit — guaranteed terminal restoration
  - select()-based log reader — immediate shutdown, no blocking readline()

Features:
  - Interface status: names, IPs, up/down state
  - Live bandwidth: per-interface TX/RX rates with sparklines
  - Firewall overview: chain policies, packet counters, blocked count
  - Active connections: TCP/UDP with PID/program mapping
  - Network log stream: severity-colored with LPS tracking
  - Sort / Filter / Pause / Clear keyboard controls

Controls:
  Q — Quit       C — Clear logs     P — Pause/resume
  F — Filter     S — Sort conns     R — Refresh

Dependencies: stdlib, psutil
Target: Linux workstation with iptables or nftables
"""

import curses
import time
import sys
import os
import threading
import subprocess
import re
import signal
import atexit
import select
import socket
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from collections import deque

import psutil


# ==================== CONSTANTS ====================

# Timing intervals (seconds)
FRAME_INTERVAL_MS: int = 100          # ~10 FPS via stdscr.timeout()
INTERFACE_POLL_INTERVAL: float = 3.0
BANDWIDTH_POLL_INTERVAL: float = 1.0
FIREWALL_POLL_INTERVAL: float = 5.0
CONNECTION_POLL_INTERVAL: float = 2.0
COLLECTOR_SHUTDOWN_TIMEOUT: float = 2.0

# Buffer sizes
LOG_BUFFER_SIZE: int = 500
SPARKLINE_LENGTH: int = 20

# LPS (logs per second) parameters
LPS_WINDOW_SECONDS: float = 5.0
LPS_PEAK_WINDOW_SECONDS: float = 60.0
LPS_MIN_SCALE: float = 5.0
LPS_BAR_WIDTH: int = 10
LPS_BAR_FILLED: str = "■"
LPS_BAR_EMPTY: str = "□"

# Terminal minimum dimensions
MIN_TERMINAL_WIDTH: int = 60
MIN_TERMINAL_HEIGHT: int = 15

# Sparkline block characters (8 levels: empty → full)
SPARKLINE_BLOCKS: Tuple[str, ...] = (" ", "▂", "▃", "▄", "▅", "▆", "▇", "█")

# Box drawing characters
BOX: Dict[str, str] = {
    "tl": "╭", "tr": "╮", "bl": "╰", "br": "╯", "h": "─", "v": "│",
}

# Address family constants
_AF_INET: int = socket.AF_INET
_AF_INET6: int = socket.AF_INET6
_AF_PACKET: int = getattr(socket, "AF_PACKET", 17)

# Connection state color groups
CONN_ESTABLISHED: frozenset = frozenset({"ESTABLISHED"})
CONN_LISTEN: frozenset = frozenset({"LISTEN"})
CONN_WAIT: frozenset = frozenset({
    "TIME_WAIT", "FIN_WAIT1", "FIN_WAIT2", "CLOSING",
})
CONN_ERROR: frozenset = frozenset({"CLOSE_WAIT", "LAST_ACK"})

# Sort modes for connections pane
SORT_MODES: Tuple[str, ...] = ("State", "Proto", "Remote", "PID")

# Filter modes for network log pane
FILTER_MODES: Tuple[str, ...] = ("All", "Drop", "Accept", "DNS")

# Keywords for identifying network/firewall log messages in journalctl
NET_LOG_KEYWORDS: frozenset = frozenset({
    "in=", "out=", "src=", "dst=", "dpt=", "spt=",
    "firewall", "iptables", "nftables", "ufw", "netfilter",
    "drop", "reject", "block", "accept", "deny", "refused",
    "networkmanager", "dhcp", "resolved", "dns",
    "connection", "connected", "disconnect", "roam",
    "wifi", "wlan", "ethernet", "bridge", "carrier",
})

# Firewall log action classification
FW_DROP_KEYWORDS: frozenset = frozenset({
    "DROP", "BLOCK", "DENY", "REJECT", "REFUSED",
})
FW_ACCEPT_KEYWORDS: frozenset = frozenset({
    "ACCEPT", "ALLOW", "PASS",
})


# ==================== DATACLASSES ====================

@dataclass(frozen=True)
class InterfaceInfo:
    """Network interface state."""
    name: str
    ip: str = ""
    mac: str = ""
    is_up: bool = False
    speed_mbps: int = 0


@dataclass(frozen=True)
class BandwidthInfo:
    """Per-interface bandwidth snapshot."""
    interface: str
    tx_rate: float = 0.0      # bytes per second
    rx_rate: float = 0.0
    tx_total: int = 0
    rx_total: int = 0
    rx_sparkline: Tuple[float, ...] = ()


@dataclass(frozen=True)
class FirewallChain:
    """Firewall chain summary."""
    name: str
    policy: str = "ACCEPT"
    packets: int = 0
    rules: int = 0


@dataclass(frozen=True)
class ConnectionInfo:
    """A single network connection."""
    proto: str = "tcp"
    local_addr: str = ""
    local_port: int = 0
    remote_addr: str = ""
    remote_port: int = 0
    state: str = ""
    pid: int = 0
    program: str = ""


@dataclass
class FirewallLogEntry:
    """A single parsed firewall/network log entry."""
    timestamp: str
    action: str       # "DROP" | "ACCEPT" | "LOG" | "INFO"
    message: str
    src: str = ""
    dst: str = ""
    dport: str = ""
    proto: str = ""


@dataclass(frozen=True)
class NetMonSnapshot:
    """
    Immutable snapshot of all network monitoring state for rendering.
    Created once per frame by CollectorManager.take_snapshot().
    The UI thread renders exclusively from this object.
    """
    interfaces: Tuple[InterfaceInfo, ...] = field(default_factory=tuple)
    bandwidth: Tuple[BandwidthInfo, ...] = field(default_factory=tuple)
    bandwidth_total_tx: int = 0
    bandwidth_total_rx: int = 0
    firewall_type: str = "none"
    firewall_chains: Tuple[FirewallChain, ...] = field(default_factory=tuple)
    firewall_blocked: int = 0
    connections: Tuple[ConnectionInfo, ...] = field(default_factory=tuple)
    conn_count: int = 0
    conn_established: int = 0
    conn_listening: int = 0
    logs: Tuple[FirewallLogEntry, ...] = field(default_factory=tuple)
    lps: float = 0.0
    lps_scale: float = 5.0
    filter_mode: str = "All"
    sort_mode: str = "State"
    paused: bool = False
    snapshot_time: float = field(default_factory=time.time)


# ==================== LAYOUT MANAGER ====================

@dataclass
class Pane:
    """Represents a rectangular pane in the terminal layout."""
    y: int
    x: int
    height: int
    width: int
    title: str = ""
    footer: str = ""
    color_pair: int = 1


class LayoutManager:
    """Calculates pane positions for the five-panel network monitor layout."""

    def __init__(self) -> None:
        self.height: int = 24
        self.width: int = 80
        self.panes: Dict[str, Pane] = {}

    def calculate_layout(self, height: int, width: int) -> bool:
        """Calculate pane dimensions.  Returns False if terminal is too small."""
        self.height = height
        self.width = width
        if height < MIN_TERMINAL_HEIGHT or width < MIN_TERMINAL_WIDTH:
            return False

        top_height = 8
        left_width = int(width * 0.35)
        middle_width = int(width * 0.30)
        right_width = width - left_width - middle_width

        self.panes["interfaces"] = Pane(
            y=0, x=0, height=top_height, width=left_width,
            title="INTERFACES", color_pair=1,
        )
        self.panes["bandwidth"] = Pane(
            y=0, x=left_width, height=top_height, width=middle_width,
            title="BANDWIDTH", color_pair=2,
        )
        self.panes["firewall"] = Pane(
            y=0, x=left_width + middle_width, height=top_height,
            width=right_width, title="FIREWALL", color_pair=1,
        )

        remaining = height - top_height
        conn_height = min(10, max(5, remaining // 2))
        logs_height = remaining - conn_height

        self.panes["connections"] = Pane(
            y=top_height, x=0, height=conn_height, width=width,
            title="ACTIVE CONNECTIONS", color_pair=2,
        )

        menu_footer = "[Q]uit [C]lear [P]ause [F]ilter [S]ort [R]efresh"
        self.panes["logs"] = Pane(
            y=top_height + conn_height, x=0, height=logs_height,
            width=width, title="NETWORK LOG", footer=menu_footer,
            color_pair=1,
        )
        return True


# ==================== COLLECTORS ====================

class InterfaceCollector:
    """Monitors network interface status and addresses."""

    def __init__(self, stop_event: threading.Event) -> None:
        self._stop = stop_event
        self._lock = threading.Lock()
        self._interfaces: List[InterfaceInfo] = []
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Launch the collector thread."""
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="interface-collector",
        )
        self._thread.start()

    def get_data(self) -> Tuple[InterfaceInfo, ...]:
        """Return an immutable copy of current interface data."""
        with self._lock:
            return tuple(self._interfaces)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._poll()
            except Exception:
                pass
            self._stop.wait(INTERFACE_POLL_INTERVAL)

    def _poll(self) -> None:
        """Poll psutil for interface addresses and stats."""
        addrs = psutil.net_if_addrs()
        stats = psutil.net_if_stats()
        interfaces: List[InterfaceInfo] = []
        for name in sorted(addrs.keys()):
            ip, mac = "", ""
            for addr in addrs[name]:
                if addr.family == _AF_INET:
                    ip = addr.address
                elif addr.family == _AF_PACKET:
                    mac = addr.address
            is_up = False
            speed = 0
            if name in stats:
                is_up = stats[name].isup
                speed = stats[name].speed
            interfaces.append(InterfaceInfo(
                name=name, ip=ip, mac=mac, is_up=is_up,
                speed_mbps=speed,
            ))
        with self._lock:
            self._interfaces = interfaces


class BandwidthCollector:
    """Tracks per-interface bandwidth rates and sparkline history."""

    def __init__(self, stop_event: threading.Event) -> None:
        self._stop = stop_event
        self._lock = threading.Lock()
        self._prev_counters: Dict[str, Tuple[int, int]] = {}
        self._prev_time: float = 0.0
        self._rates: Dict[str, Tuple[float, float]] = {}
        self._totals: Dict[str, Tuple[int, int]] = {}
        self._sparklines: Dict[str, deque] = {}
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Initialize counters and launch the collector thread."""
        try:
            counters = psutil.net_io_counters(pernic=True)
            for name, c in counters.items():
                self._prev_counters[name] = (c.bytes_sent, c.bytes_recv)
                self._totals[name] = (c.bytes_sent, c.bytes_recv)
                self._sparklines[name] = deque(
                    [0.0] * SPARKLINE_LENGTH, maxlen=SPARKLINE_LENGTH,
                )
        except Exception:
            pass
        self._prev_time = time.time()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="bandwidth-collector",
        )
        self._thread.start()

    def get_data(self) -> Tuple[BandwidthInfo, ...]:
        """Return immutable bandwidth info for all interfaces."""
        with self._lock:
            result = []
            for iface in sorted(self._rates.keys()):
                tx_r, rx_r = self._rates.get(iface, (0.0, 0.0))
                tx_t, rx_t = self._totals.get(iface, (0, 0))
                spark = tuple(self._sparklines.get(iface, ()))
                result.append(BandwidthInfo(
                    interface=iface, tx_rate=tx_r, rx_rate=rx_r,
                    tx_total=tx_t, rx_total=rx_t, rx_sparkline=spark,
                ))
            return tuple(result)

    def get_totals(self) -> Tuple[int, int]:
        """Return (total_tx_bytes, total_rx_bytes) across all interfaces."""
        with self._lock:
            total_tx = sum(t[0] for t in self._totals.values())
            total_rx = sum(t[1] for t in self._totals.values())
            return total_tx, total_rx

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._poll()
            except Exception:
                pass
            self._stop.wait(BANDWIDTH_POLL_INTERVAL)

    def _poll(self) -> None:
        """Sample counters and compute per-interface byte rates."""
        now = time.time()
        dt = now - self._prev_time
        if dt <= 0:
            return
        counters = psutil.net_io_counters(pernic=True)
        with self._lock:
            for name, c in counters.items():
                prev = self._prev_counters.get(name)
                if prev:
                    tx_delta = max(0, c.bytes_sent - prev[0])
                    rx_delta = max(0, c.bytes_recv - prev[1])
                    tx_rate = tx_delta / dt
                    rx_rate = rx_delta / dt
                    self._rates[name] = (tx_rate, rx_rate)
                    if name not in self._sparklines:
                        self._sparklines[name] = deque(
                            [0.0] * SPARKLINE_LENGTH, maxlen=SPARKLINE_LENGTH,
                        )
                    self._sparklines[name].append(rx_rate)
                self._prev_counters[name] = (c.bytes_sent, c.bytes_recv)
                self._totals[name] = (c.bytes_sent, c.bytes_recv)
            self._prev_time = now


class FirewallCollector:
    """Polls iptables or nftables for chain policies and packet counters."""

    def __init__(self, stop_event: threading.Event) -> None:
        self._stop = stop_event
        self._lock = threading.Lock()
        self._fw_type: str = "none"
        self._chains: List[FirewallChain] = []
        self._blocked: int = 0
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Detect firewall type and launch the collector thread."""
        self._fw_type = self._detect_firewall()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="firewall-collector",
        )
        self._thread.start()

    def get_data(self) -> Tuple[str, Tuple[FirewallChain, ...], int]:
        """Return (firewall_type, chains, blocked_count)."""
        with self._lock:
            return self._fw_type, tuple(self._chains), self._blocked

    @staticmethod
    def _detect_firewall() -> str:
        """Auto-detect whether nftables or iptables is available."""
        for cmd, name in (("nft", "nftables"), ("iptables", "iptables")):
            try:
                subprocess.run(
                    [cmd, "--version"], capture_output=True, timeout=2.0,
                )
                return name
            except (FileNotFoundError, subprocess.TimeoutExpired):
                continue
        return "none"

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                if self._fw_type == "iptables":
                    self._poll_iptables()
                elif self._fw_type == "nftables":
                    self._poll_nftables()
            except Exception:
                pass
            self._stop.wait(FIREWALL_POLL_INTERVAL)

    def _poll_iptables(self) -> None:
        """Parse `iptables -L -v -n` for chain policies and counters."""
        try:
            result = subprocess.run(
                ["iptables", "-L", "-v", "-n"],
                capture_output=True, text=True, timeout=3.0,
            )
            if result.returncode != 0:
                return
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return

        chains: List[FirewallChain] = []
        blocked = 0
        current_chain = ""
        current_policy = ""
        current_packets = 0
        rule_count = 0

        for line in result.stdout.split("\n"):
            # Chain header: "Chain INPUT (policy ACCEPT 1234 packets, 5678 bytes)"
            chain_match = re.match(
                r"Chain\s+(\S+)\s+\(policy\s+(\S+)\s+(\d+)\s+packets",
                line,
            )
            if chain_match:
                if current_chain:
                    chains.append(FirewallChain(
                        name=current_chain, policy=current_policy,
                        packets=current_packets, rules=rule_count,
                    ))
                current_chain = chain_match.group(1)
                current_policy = chain_match.group(2)
                current_packets = int(chain_match.group(3))
                rule_count = 0
                continue

            # Rule lines start with whitespace + packet count
            rule_match = re.match(r"\s*(\d+)\s+\d+\s+(\S+)", line)
            if rule_match and current_chain:
                rule_count += 1
                pkts = int(rule_match.group(1))
                target = rule_match.group(2)
                if target in ("DROP", "REJECT"):
                    blocked += pkts

        if current_chain:
            chains.append(FirewallChain(
                name=current_chain, policy=current_policy,
                packets=current_packets, rules=rule_count,
            ))

        with self._lock:
            self._chains = chains
            self._blocked = blocked

    def _poll_nftables(self) -> None:
        """Parse `nft list ruleset` for chain info and drop counters."""
        try:
            result = subprocess.run(
                ["nft", "list", "ruleset"],
                capture_output=True, text=True, timeout=3.0,
            )
            if result.returncode != 0:
                return
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return

        chains: List[FirewallChain] = []
        blocked = 0
        current_chain = ""
        current_policy = ""
        rule_count = 0

        for line in result.stdout.split("\n"):
            stripped = line.strip()
            # Chain definition: "chain input { type filter hook input priority 0; policy accept; }"
            chain_match = re.match(r"chain\s+(\S+)\s*\{", stripped)
            if chain_match:
                if current_chain:
                    chains.append(FirewallChain(
                        name=current_chain, policy=current_policy,
                        packets=0, rules=rule_count,
                    ))
                current_chain = chain_match.group(1)
                current_policy = "ACCEPT"
                rule_count = 0
                continue

            if "policy" in stripped and current_chain:
                pol_match = re.search(r"policy\s+(\S+)", stripped)
                if pol_match:
                    current_policy = pol_match.group(1).upper().rstrip(";")

            if current_chain and stripped.startswith(("counter", "drop", "reject",
                                                      "accept", "log", "tcp", "udp",
                                                      "ip", "iif", "oif")):
                rule_count += 1
                if "drop" in stripped or "reject" in stripped:
                    # Try to extract counter value
                    cnt_match = re.search(r"counter\s+packets\s+(\d+)", stripped)
                    if cnt_match:
                        blocked += int(cnt_match.group(1))

            if stripped == "}":
                if current_chain:
                    chains.append(FirewallChain(
                        name=current_chain, policy=current_policy,
                        packets=0, rules=rule_count,
                    ))
                    current_chain = ""
                    rule_count = 0

        with self._lock:
            self._chains = chains
            self._blocked = blocked


class ConnectionCollector:
    """Maps active TCP/UDP connections to PIDs and program names."""

    def __init__(self, stop_event: threading.Event) -> None:
        self._stop = stop_event
        self._lock = threading.Lock()
        self._connections: List[ConnectionInfo] = []
        self._count: int = 0
        self._established: int = 0
        self._listening: int = 0
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Launch the collector thread."""
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="connection-collector",
        )
        self._thread.start()

    def get_data(self) -> Tuple[Tuple[ConnectionInfo, ...], int, int, int]:
        """Return (connections, total_count, established, listening)."""
        with self._lock:
            return (tuple(self._connections), self._count,
                    self._established, self._listening)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._poll()
            except Exception:
                pass
            self._stop.wait(CONNECTION_POLL_INTERVAL)

    def _poll(self) -> None:
        """Snapshot all network connections via psutil."""
        pid_name_cache: Dict[int, str] = {}
        conns: List[ConnectionInfo] = []
        established = 0
        listening = 0

        try:
            raw = psutil.net_connections(kind="inet")
        except (psutil.AccessDenied, OSError):
            # Fallback: try inet4 only
            try:
                raw = psutil.net_connections(kind="inet4")
            except Exception:
                raw = []

        for c in raw:
            proto = "tcp" if c.type == socket.SOCK_STREAM else "udp"
            local_addr = c.laddr.ip if c.laddr else ""
            local_port = c.laddr.port if c.laddr else 0
            remote_addr = c.raddr.ip if c.raddr else ""
            remote_port = c.raddr.port if c.raddr else 0
            state = c.status if c.status else ""

            # Map PID to process name
            pid = c.pid or 0
            program = ""
            if pid > 0:
                if pid not in pid_name_cache:
                    try:
                        pid_name_cache[pid] = psutil.Process(pid).name()
                    except (psutil.NoSuchProcess, psutil.AccessDenied,
                            psutil.ZombieProcess):
                        pid_name_cache[pid] = "?"
                program = pid_name_cache[pid]

            if state == "ESTABLISHED":
                established += 1
            elif state == "LISTEN":
                listening += 1

            conns.append(ConnectionInfo(
                proto=proto, local_addr=local_addr, local_port=local_port,
                remote_addr=remote_addr, remote_port=remote_port,
                state=state, pid=pid, program=program,
            ))

        with self._lock:
            self._connections = conns
            self._count = len(conns)
            self._established = established
            self._listening = listening


class NetworkLogCollector:
    """
    Streams and parses journalctl for network/firewall log messages.

    Uses select()-based non-blocking reads for prompt shutdown.
    Includes LPS tracking with adaptive scaling (same algorithm as kaiamon.py).
    """

    def __init__(self, stop_event: threading.Event) -> None:
        self._stop = stop_event
        self._lock = threading.Lock()
        self._logs: deque = deque(maxlen=LOG_BUFFER_SIZE)
        self._lps_timestamps: deque = deque()
        self._lps_peak_history: deque = deque()
        self._paused: bool = False
        self._filter_mode: str = "All"
        self._process: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Launch the collector thread."""
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="netlog-collector",
        )
        self._thread.start()

    def stop(self) -> None:
        """Terminate the journalctl subprocess."""
        if self._process:
            try:
                self._process.terminate()
                self._process.wait(timeout=COLLECTOR_SHUTDOWN_TIMEOUT)
            except Exception:
                try:
                    self._process.kill()
                except Exception:
                    pass

    # --- public properties ---

    @property
    def paused(self) -> bool:
        return self._paused

    @paused.setter
    def paused(self, value: bool) -> None:
        self._paused = value

    @property
    def filter_mode(self) -> str:
        return self._filter_mode

    @filter_mode.setter
    def filter_mode(self, value: str) -> None:
        self._filter_mode = value

    def clear(self) -> None:
        """Clear all log data AND LPS state together."""
        with self._lock:
            self._logs.clear()
            self._lps_timestamps.clear()
            self._lps_peak_history.clear()

    def get_data(self) -> Tuple[Tuple[FirewallLogEntry, ...], float, float, str, bool]:
        """Returns (logs, lps, lps_scale, filter_mode, paused)."""
        with self._lock:
            logs = tuple(self._logs)
            lps = self._calculate_lps()
            scale = self._calculate_lps_scale(lps)
            return logs, lps, scale, self._filter_mode, self._paused

    # --- LPS computation ---

    def _calculate_lps(self) -> float:
        """Calculate current LPS from the timestamps-only deque."""
        now = time.time()
        cutoff = now - LPS_WINDOW_SECONDS
        while self._lps_timestamps and self._lps_timestamps[0] < cutoff:
            self._lps_timestamps.popleft()
        return len(self._lps_timestamps) / LPS_WINDOW_SECONDS

    def _calculate_lps_scale(self, current_lps: float) -> float:
        """Adaptive LPS bar scale using a rolling 1-minute peak with headroom."""
        now = time.time()
        self._lps_peak_history.append((now, current_lps))
        cutoff = now - LPS_PEAK_WINDOW_SECONDS
        while self._lps_peak_history and self._lps_peak_history[0][0] < cutoff:
            self._lps_peak_history.popleft()
        if not self._lps_peak_history:
            return LPS_MIN_SCALE
        peak = max(v for _, v in self._lps_peak_history)
        return max(LPS_MIN_SCALE, peak * 1.2)

    # --- line parsing ---

    def _classify_action(self, text: str) -> str:
        """Classify a log line's action/severity."""
        upper = text.upper()
        for kw in FW_DROP_KEYWORDS:
            if kw in upper:
                return "DROP"
        for kw in FW_ACCEPT_KEYWORDS:
            if kw in upper:
                return "ACCEPT"
        return "INFO"

    def _parse_line(self, line: str) -> Optional[FirewallLogEntry]:
        """Parse a raw journalctl line into a FirewallLogEntry, or None to skip."""
        stripped = line.strip()
        if not stripped:
            return None

        # Check if this line is network/firewall related
        lower = stripped.lower()
        if not any(kw in lower for kw in NET_LOG_KEYWORDS):
            return None

        # Extract timestamp
        match = re.match(r"^(\S+)\s+(\S+)\s+(.*)$", stripped)
        if match:
            iso_ts, _host, payload = match.groups()
            ts_match = re.search(r"T(\d{2}:\d{2}:\d{2})", iso_ts)
            ts = ts_match.group(1) if ts_match else iso_ts[:8]
        else:
            ts = time.strftime("%H:%M:%S")
            payload = stripped

        # Separate service tag from message body
        srv_match = re.match(r"^([^:]+):\s*(.*)$", payload)
        if srv_match:
            svc, body = srv_match.groups()
            msg = f"{svc.split('[')[0]}: {body}"
        else:
            msg = payload

        action = self._classify_action(msg)

        # Try to extract network fields from iptables/nftables LOG format
        src, dst, dport, proto = "", "", "", ""
        if "SRC=" in msg or "src=" in msg.lower():
            src_m = re.search(r"SRC=(\S+)", msg, re.IGNORECASE)
            if src_m:
                src = src_m.group(1)
            dst_m = re.search(r"DST=(\S+)", msg, re.IGNORECASE)
            if dst_m:
                dst = dst_m.group(1)
            dpt_m = re.search(r"DPT=(\d+)", msg, re.IGNORECASE)
            if dpt_m:
                dport = dpt_m.group(1)
            proto_m = re.search(r"PROTO=(\S+)", msg, re.IGNORECASE)
            if proto_m:
                proto = proto_m.group(1)

        return FirewallLogEntry(
            timestamp=ts, action=action, message=msg,
            src=src, dst=dst, dport=dport, proto=proto,
        )

    # --- thread body (non-blocking select loop) ---

    def _run(self) -> None:
        """Stream journalctl with select() for prompt shutdown."""
        cmd = ["journalctl", "--since", "1 hour ago", "-f", "-o", "short-iso"]
        try:
            self._process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, bufsize=1,
            )
        except FileNotFoundError:
            return

        try:
            while not self._stop.is_set():
                ready, _, _ = select.select(
                    [self._process.stdout], [], [], 0.5,
                )
                if not ready:
                    continue
                line = self._process.stdout.readline()
                if not line:
                    break

                entry = self._parse_line(line)
                if entry is None:
                    continue

                with self._lock:
                    if not self._paused:
                        self._logs.append(entry)
                        self._lps_timestamps.append(time.time())
        except Exception:
            pass


# ==================== COLLECTOR MANAGER ====================

class CollectorManager:
    """
    Centralized lifecycle manager for all data collectors.

    Responsibilities:
      - Start / stop all collector threads
      - Produce immutable NetMonSnapshot objects for the UI
      - Manage subprocess cleanup
    """

    def __init__(self) -> None:
        self._stop_event = threading.Event()
        self._interface = InterfaceCollector(self._stop_event)
        self._bandwidth = BandwidthCollector(self._stop_event)
        self._firewall = FirewallCollector(self._stop_event)
        self._connection = ConnectionCollector(self._stop_event)
        self._netlog = NetworkLogCollector(self._stop_event)
        self._sort_mode: str = "State"

    @property
    def log_collector(self) -> NetworkLogCollector:
        """Expose log collector for pause/filter/clear commands."""
        return self._netlog

    @property
    def sort_mode(self) -> str:
        return self._sort_mode

    @sort_mode.setter
    def sort_mode(self, value: str) -> None:
        self._sort_mode = value

    def start_all(self) -> None:
        """Start every collector thread."""
        self._interface.start()
        self._bandwidth.start()
        self._firewall.start()
        self._connection.start()
        self._netlog.start()

    def stop_all(self) -> None:
        """Signal shutdown and release all resources."""
        self._stop_event.set()
        self._netlog.stop()

    def take_snapshot(self) -> NetMonSnapshot:
        """
        Create an immutable snapshot of all collector data.
        This is the ONLY place where collector-owned state is read.
        """
        interfaces = self._interface.get_data()
        bandwidth = self._bandwidth.get_data()
        total_tx, total_rx = self._bandwidth.get_totals()
        fw_type, fw_chains, fw_blocked = self._firewall.get_data()
        conns, conn_count, conn_estab, conn_listen = self._connection.get_data()
        logs, lps, lps_scale, filter_mode, paused = self._netlog.get_data()

        # Sort connections based on current sort mode
        sorted_conns = self._sort_connections(conns)

        return NetMonSnapshot(
            interfaces=interfaces,
            bandwidth=bandwidth,
            bandwidth_total_tx=total_tx,
            bandwidth_total_rx=total_rx,
            firewall_type=fw_type,
            firewall_chains=fw_chains,
            firewall_blocked=fw_blocked,
            connections=sorted_conns,
            conn_count=conn_count,
            conn_established=conn_estab,
            conn_listening=conn_listen,
            logs=logs,
            lps=lps,
            lps_scale=lps_scale,
            filter_mode=filter_mode,
            sort_mode=self._sort_mode,
            paused=paused,
        )

    def _sort_connections(self, conns: Tuple[ConnectionInfo, ...],
                          ) -> Tuple[ConnectionInfo, ...]:
        """Sort connections by the current sort mode."""
        if self._sort_mode == "State":
            order = {"ESTABLISHED": 0, "LISTEN": 1, "TIME_WAIT": 2,
                     "CLOSE_WAIT": 3, "FIN_WAIT1": 4, "FIN_WAIT2": 5}
            return tuple(sorted(conns, key=lambda c: (
                order.get(c.state, 99), c.proto, c.local_port,
            )))
        elif self._sort_mode == "Proto":
            return tuple(sorted(conns, key=lambda c: (c.proto, c.local_port)))
        elif self._sort_mode == "Remote":
            return tuple(sorted(conns, key=lambda c: (c.remote_addr, c.remote_port)))
        elif self._sort_mode == "PID":
            return tuple(sorted(conns, key=lambda c: (c.program, c.pid)))
        return conns


# ==================== UI ====================

class NetMonUI:
    """
    Production-quality curses UI for network/firewall monitoring.

    Architecture rules (matching kaiamon.py):
      1. curses runs ONLY in this class, ONLY in the main thread
      2. Rendering is driven exclusively by immutable NetMonSnapshot objects
      3. Terminal is ALWAYS restored on exit (Q, Ctrl-C, SIGTERM, exception)
      4. No collector thread may access curses, layout, or trigger redraws
    """

    def __init__(self) -> None:
        self._running: bool = False
        self._stdscr = None
        self._layout = LayoutManager()
        self._collector_mgr = CollectorManager()

    # ==================== Terminal Safety ====================

    def run(self) -> None:
        """Entry point — must be called from the main thread."""
        atexit.register(self._restore_terminal)

        original_sigint = signal.getsignal(signal.SIGINT)
        original_sigterm = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        try:
            curses.wrapper(self._main_loop)
        except KeyboardInterrupt:
            pass
        except Exception:
            pass
        finally:
            self._running = False
            self._collector_mgr.stop_all()
            self._restore_terminal()
            signal.signal(signal.SIGINT, original_sigint)
            signal.signal(signal.SIGTERM, original_sigterm)

    def _signal_handler(self, signum: int, frame) -> None:
        """Handle SIGINT / SIGTERM by requesting clean shutdown."""
        self._running = False

    def _restore_terminal(self) -> None:
        """Ensure the terminal is fully restored regardless of exit path."""
        try:
            curses.nocbreak()
            curses.echo()
            curses.endwin()
        except Exception:
            pass
        try:
            sys.stdout.write("\033[0m\033[?25h\033[?1049l\033[H\033[2J")
            sys.stdout.flush()
        except Exception:
            pass

    # ==================== Initialization ====================

    def _init_curses(self, stdscr) -> None:
        """Initialize curses settings and color pairs."""
        self._stdscr = stdscr
        curses.curs_set(0)
        stdscr.nodelay(True)
        stdscr.timeout(FRAME_INTERVAL_MS)
        stdscr.keypad(True)

        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_CYAN, -1)      # Borders & titles
        curses.init_pair(2, curses.COLOR_MAGENTA, -1)    # Secondary labels
        curses.init_pair(3, curses.COLOR_GREEN, -1)      # Good / up / accept
        curses.init_pair(4, curses.COLOR_YELLOW, -1)     # Warning / wait
        curses.init_pair(5, curses.COLOR_RED, -1)        # Error / drop / down
        curses.init_pair(6, -1, -1)                      # Default text
        curses.init_pair(7, curses.COLOR_BLUE, -1)       # Info / UDP

    # ==================== Main Loop ====================

    def _main_loop(self, stdscr) -> None:
        """Main UI loop — runs in main thread only."""
        self._init_curses(stdscr)
        self._running = True
        self._collector_mgr.start_all()

        while self._running:
            if not self._handle_input():
                self._running = False
                break

            try:
                snapshot = self._collector_mgr.take_snapshot()
                self._draw_frame(snapshot)
                stdscr.refresh()
            except Exception:
                pass

    # ==================== Input Handling ====================

    def _handle_input(self) -> bool:
        """Process keyboard input.  Returns False to exit."""
        if not self._stdscr:
            return True
        try:
            ch = self._stdscr.getch()
            if ch == curses.ERR:
                return True
            if ch in (ord("q"), ord("Q")):
                return False
            elif ch in (ord("c"), ord("C")):
                self._collector_mgr.log_collector.clear()
            elif ch in (ord("p"), ord("P")):
                lc = self._collector_mgr.log_collector
                lc.paused = not lc.paused
            elif ch in (ord("f"), ord("F")):
                lc = self._collector_mgr.log_collector
                idx = (FILTER_MODES.index(lc.filter_mode) + 1) % len(FILTER_MODES)
                lc.filter_mode = FILTER_MODES[idx]
            elif ch in (ord("s"), ord("S")):
                mgr = self._collector_mgr
                idx = (SORT_MODES.index(mgr.sort_mode) + 1) % len(SORT_MODES)
                mgr.sort_mode = SORT_MODES[idx]
            elif ch in (ord("r"), ord("R")):
                self._stdscr.clear()
        except Exception:
            pass
        return True

    # ==================== Drawing Primitives ====================

    def _safe_addstr(self, y: int, x: int, text: str, attr: int = 0) -> None:
        """Safely write a string with screen-bounds checking."""
        if not self._stdscr:
            return
        max_y, max_x = self._stdscr.getmaxyx()
        if y < 0 or y >= max_y or x < 0 or x >= max_x:
            return
        text = text[:max_x - x]
        try:
            self._stdscr.addstr(y, x, text, attr)
        except curses.error:
            pass

    def _draw_box(self, pane: Pane) -> None:
        """Draw a Unicode box around a pane with optional title and footer."""
        if not self._stdscr:
            return
        max_y, max_x = self._stdscr.getmaxyx()
        y, x, h, w = pane.y, pane.x, pane.height, pane.width
        if y >= max_y or x >= max_x:
            return
        h = min(h, max_y - y)
        w = min(w, max_x - x)
        if h < 3 or w < 3:
            return

        color = curses.color_pair(pane.color_pair)

        # Top border with title
        title_str = f" {pane.title} " if pane.title else ""
        if len(title_str) > w - 4:
            title_str = title_str[:w - 4]
        left_h = (w - 2 - len(title_str)) // 2
        right_h = w - 2 - len(title_str) - left_h
        top = BOX["tl"] + BOX["h"] * left_h + title_str + BOX["h"] * right_h + BOX["tr"]
        self._safe_addstr(y, x, top, color)

        # Vertical borders + clear interior
        for i in range(1, h - 1):
            self._safe_addstr(y + i, x, BOX["v"], color)
            self._safe_addstr(y + i, x + 1, " " * (w - 2))
            self._safe_addstr(y + i, x + w - 1, BOX["v"], color)

        # Bottom border with footer
        footer_str = f" {pane.footer} " if pane.footer else ""
        if len(footer_str) > w - 4:
            footer_str = footer_str[:w - 4]
        left_f = (w - 2 - len(footer_str)) // 2
        right_f = w - 2 - len(footer_str) - left_f
        bot = BOX["bl"] + BOX["h"] * left_f + footer_str + BOX["h"] * right_f + BOX["br"]
        self._safe_addstr(y + h - 1, x, bot, color)

    def _make_sparkline(self, data: Tuple[float, ...], width: int = 0) -> str:
        """Generate a Unicode sparkline from a tuple of float values."""
        if not data:
            return " " * (width or SPARKLINE_LENGTH)
        vals = list(data)
        if width and len(vals) > width:
            vals = vals[-width:]
        limit = max((v for v in vals if v >= 0), default=0.0)
        if limit == 0:
            return " " * len(vals)
        spark = ""
        for val in vals:
            clamped = max(0.0, val)
            ratio = clamped / limit
            idx = min(len(SPARKLINE_BLOCKS) - 1, int(ratio * (len(SPARKLINE_BLOCKS) - 1)))
            spark += SPARKLINE_BLOCKS[idx]
        return spark

    @staticmethod
    def _format_bytes(n: float, rate: bool = False) -> str:
        """Format bytes to human-readable string (e.g., 1.2M/s)."""
        suffix = "/s" if rate else ""
        for unit in ("B", "K", "M", "G", "T"):
            if abs(n) < 1024.0:
                if unit == "B":
                    return f"{int(n)}{unit}{suffix}"
                return f"{n:.1f}{unit}{suffix}"
            n /= 1024.0
        return f"{n:.1f}P{suffix}"

    # ==================== Pane Drawing ====================

    def _draw_interfaces_pane(self, snap: NetMonSnapshot) -> None:
        """Draw the network interfaces panel."""
        pane = self._layout.panes.get("interfaces")
        if not pane:
            return
        self._draw_box(pane)
        py = pane.y + 1
        px = pane.x + 2
        p_w = pane.width - 4

        for iface in snap.interfaces:
            if py >= pane.y + pane.height - 1:
                break
            name = iface.name[:10].ljust(10)

            if iface.is_up:
                dot_color = curses.color_pair(3)
            else:
                dot_color = curses.color_pair(5)

            ip_str = iface.ip[:p_w - 14] if iface.ip else "no addr"

            self._safe_addstr(py, px, name,
                              curses.color_pair(2) | curses.A_BOLD)
            self._safe_addstr(py, px + 11, "●", dot_color)
            self._safe_addstr(py, px + 13, ip_str, curses.color_pair(6))
            py += 1

    def _draw_bandwidth_pane(self, snap: NetMonSnapshot) -> None:
        """Draw the bandwidth rates and sparklines panel."""
        pane = self._layout.panes.get("bandwidth")
        if not pane:
            return
        self._draw_box(pane)
        by = pane.y + 1
        bx = pane.x + 2
        b_w = pane.width - 4

        # Show top interfaces by RX rate (skip loopback if others exist)
        active_bw = [b for b in snap.bandwidth if b.interface != "lo"]
        if not active_bw:
            active_bw = list(snap.bandwidth)
        active_bw.sort(key=lambda b: b.rx_rate, reverse=True)

        max_rows = pane.height - 3  # Reserve 1 for totals
        for bw in active_bw[:max_rows]:
            if by >= pane.y + pane.height - 2:
                break
            name = bw.interface[:6].ljust(6)
            rx_str = self._format_bytes(bw.rx_rate, rate=True).rjust(8)
            spark_w = max(1, b_w - 15)
            spark = self._make_sparkline(bw.rx_sparkline, width=spark_w)

            # Color by rate magnitude
            if bw.rx_rate > 1024 * 1024:  # > 1MB/s
                color = curses.color_pair(4) | curses.A_BOLD
            elif bw.rx_rate > 1024:  # > 1KB/s
                color = curses.color_pair(3)
            else:
                color = curses.color_pair(6)

            self._safe_addstr(by, bx, name, curses.color_pair(2))
            self._safe_addstr(by, bx + 6, rx_str, color)
            self._safe_addstr(by, bx + 15, spark[-spark_w:],
                              curses.color_pair(1))
            by += 1

        # Totals row at bottom of pane
        total_row = pane.y + pane.height - 2
        tx_str = self._format_bytes(float(snap.bandwidth_total_tx))
        rx_str = self._format_bytes(float(snap.bandwidth_total_rx))
        totals = f"TX:{tx_str} RX:{rx_str}"
        self._safe_addstr(total_row, bx, totals[:b_w],
                          curses.color_pair(1) | curses.A_BOLD)

    def _draw_firewall_pane(self, snap: NetMonSnapshot) -> None:
        """Draw the firewall chains and blocked packet counters panel."""
        pane = self._layout.panes.get("firewall")
        if not pane:
            return
        self._draw_box(pane)
        fy = pane.y + 1
        fx = pane.x + 2
        f_w = pane.width - 4

        # Firewall type header
        fw_label = snap.firewall_type.upper() if snap.firewall_type != "none" else "N/A"
        self._safe_addstr(fy, fx, f"Type: {fw_label}"[:f_w],
                          curses.color_pair(1) | curses.A_BOLD)
        fy += 1

        # Chain summaries
        for chain in snap.firewall_chains:
            if fy >= pane.y + pane.height - 2:
                break
            cname = chain.name[:8].ljust(8)
            policy = chain.policy[:6].ljust(6)

            if chain.policy in ("DROP", "REJECT"):
                pol_color = curses.color_pair(5) | curses.A_BOLD
            else:
                pol_color = curses.color_pair(3)

            pkts = f"{chain.packets}" if chain.packets else f"R:{chain.rules}"
            row = f"{cname} {policy} {pkts}"
            self._safe_addstr(fy, fx, cname, curses.color_pair(2))
            self._safe_addstr(fy, fx + 9, policy, pol_color)
            self._safe_addstr(fy, fx + 16, pkts[:f_w - 16],
                              curses.color_pair(6))
            fy += 1

        # Blocked summary at bottom
        blocked_row = pane.y + pane.height - 2
        if snap.firewall_blocked > 0:
            self._safe_addstr(blocked_row, fx,
                              f"Blocked: {snap.firewall_blocked}"[:f_w],
                              curses.color_pair(5) | curses.A_BOLD)
        else:
            self._safe_addstr(blocked_row, fx,
                              "Blocked: 0"[:f_w],
                              curses.color_pair(3))

    def _draw_connections_pane(self, snap: NetMonSnapshot) -> None:
        """Draw the active connections table panel."""
        pane = self._layout.panes.get("connections")
        if not pane:
            return

        # Dynamic title with connection stats
        pane.title = (f"ACTIVE CONNECTIONS ─ "
                      f"{snap.conn_count} total  "
                      f"{snap.conn_established} estab  "
                      f"{snap.conn_listening} listen  "
                      f"Sort: {snap.sort_mode}")
        self._draw_box(pane)
        cy = pane.y + 1
        cx = pane.x + 2
        c_w = pane.width - 4

        # Column header
        hdr = f"{'PROTO':<6}{'LOCAL':<18}{'REMOTE':<22}{'STATE':<12}{'PID/PROG'}"
        self._safe_addstr(cy, cx, hdr[:c_w],
                          curses.color_pair(1) | curses.A_BOLD)
        cy += 1

        # Connection rows
        rows_available = pane.height - 3
        for conn in snap.connections[:rows_available]:
            if cy >= pane.y + pane.height - 1:
                break

            proto = conn.proto[:5].ljust(6)
            local = f"{conn.local_addr}:{conn.local_port}"
            local = local[:17].ljust(18)
            if conn.remote_addr:
                remote = f"{conn.remote_addr}:{conn.remote_port}"
            else:
                remote = "—"
            remote = remote[:21].ljust(22)
            state = conn.state[:11].ljust(12) if conn.state else "".ljust(12)

            if conn.program:
                pid_prog = f"{conn.program}/{conn.pid}" if conn.pid else conn.program
            else:
                pid_prog = str(conn.pid) if conn.pid else ""

            # Color by connection state
            if conn.state in CONN_ESTABLISHED:
                row_color = curses.color_pair(3)
            elif conn.state in CONN_LISTEN:
                row_color = curses.color_pair(1)
            elif conn.state in CONN_WAIT:
                row_color = curses.color_pair(4)
            elif conn.state in CONN_ERROR:
                row_color = curses.color_pair(5)
            elif conn.proto == "udp":
                row_color = curses.color_pair(7)
            else:
                row_color = curses.color_pair(6)

            row = f"{proto}{local}{remote}{state}{pid_prog}"
            self._safe_addstr(cy, cx, row[:c_w], row_color)
            cy += 1

    def _draw_logs_pane(self, snap: NetMonSnapshot) -> None:
        """Draw the network log panel with severity coloring and LPS bar."""
        pane = self._layout.panes.get("logs")
        if not pane:
            return

        # Build dynamic title with adaptive LPS bar
        lps_ratio = (min(1.0, snap.lps / snap.lps_scale)
                     if snap.lps_scale > 0 else 0.0)
        filled = int(lps_ratio * LPS_BAR_WIDTH)
        bar = LPS_BAR_FILLED * filled + LPS_BAR_EMPTY * (LPS_BAR_WIDTH - filled)
        pane.title = (f"NETWORK LOG \u2500 LPS: {snap.lps:4.1f} [{bar}] "
                      f"\u2500 Filter: {snap.filter_mode}")
        if snap.paused:
            pane.title += " \u2500 PAUSED"

        self._draw_box(pane)
        ly = pane.y + 1
        lx = pane.x + 2
        l_h = pane.height - 2
        l_w = pane.width - 4

        # Apply filter to log snapshot
        filtered: List[FirewallLogEntry] = []
        for log in snap.logs:
            if snap.filter_mode == "Drop":
                if log.action != "DROP":
                    continue
            elif snap.filter_mode == "Accept":
                if log.action != "ACCEPT":
                    continue
            elif snap.filter_mode == "DNS":
                lower = log.message.lower()
                if "dpt=53" not in lower and "spt=53" not in lower and "dns" not in lower:
                    continue
            filtered.append(log)

        # Show most recent entries that fit
        for log in filtered[-l_h:]:
            if ly >= pane.y + pane.height - 1:
                break

            if log.action == "DROP":
                l_color = curses.color_pair(5) | curses.A_BOLD
                tag = "[DROP] "
            elif log.action == "ACCEPT":
                l_color = curses.color_pair(3)
                tag = "[ACPT] "
            else:
                l_color = curses.color_pair(6)
                tag = ""

            self._safe_addstr(ly, lx, f"{log.timestamp} ",
                              curses.color_pair(1))
            self._safe_addstr(ly, lx + 9, tag,
                              l_color | curses.A_BOLD if tag else l_color)
            msg_start = 9 + len(tag)
            self._safe_addstr(ly, lx + msg_start,
                              log.message[:l_w - msg_start], l_color)
            ly += 1

    # ==================== Frame Composition ====================

    def _draw_frame(self, snap: NetMonSnapshot) -> None:
        """Draw a complete frame from an immutable snapshot."""
        if not self._stdscr:
            return
        try:
            self._stdscr.erase()
            max_y, max_x = self._stdscr.getmaxyx()

            if not self._layout.calculate_layout(max_y, max_x):
                self._safe_addstr(0, 0, "Terminal too small!",
                                  curses.color_pair(5) | curses.A_BOLD)
                return

            self._draw_interfaces_pane(snap)
            self._draw_bandwidth_pane(snap)
            self._draw_firewall_pane(snap)
            self._draw_connections_pane(snap)
            self._draw_logs_pane(snap)
        except curses.error:
            pass


# ==================== ENTRY POINT ====================

def main() -> None:
    """Launch the Kaiamon Net network monitor."""
    ui = NetMonUI()
    ui.run()


if __name__ == "__main__":
    main()

