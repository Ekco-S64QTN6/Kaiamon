#!/usr/bin/env python3
"""
Kaiamon: Standalone Curses System Companion
============================================

Production-quality system monitoring dashboard optimized for an 80×24 tiled
terminal quadrant.  Implements the snapshot-based rendering architecture
from btop_dashboard_v2.py with strict thread-ownership boundaries.

CHANGELOG (v2.0 — Production Rewrite)
--------------------------------------
Architecture:
  - Frozen KaiamonSnapshot dataclass — UI renders only from immutable objects
  - CollectorManager — centralized lifecycle for all collector threads
  - Per-collector locks — no global RLock bottleneck
  - Signal handlers + atexit — guaranteed terminal restoration
  - select()-based log reader — immediate shutdown, no blocking readline()

Bugs Fixed:
  1.  Log collector blocked on readline(), hung on shutdown → select.select()
  2.  lps_history stored full LogEntry objects → timestamps-only deque
  3.  LPS bar hardcoded to 50.0 scale → adaptive rolling normalization
  4.  Split-lock regex too narrow → broadened + deduplication
  5.  CPU throttle relied on heuristics → reads kernel thermal_throttle sysfs
  6.  AMD RAPL path hardcoded to intel-rapl:0 → searches AMD paths too
  7.  gpu_enabled / gpu_poll_counter dead state → removed
  8.  Sparklines could render negative values → clamped
  9.  Service collector crashed on psutil exceptions → explicit handling
  10. raw_logs.clear() didn't reset LPS state → both cleared together
  11. Rendering held global lock for entire draw → lock only during snapshot
  12. Redundant time.sleep(0.1) in main loop → stdscr.timeout handles it
  13. No SIGINT/SIGTERM handler → added signal handlers + atexit
  14. Missing Bluesky ping target → added
  15. Filter keywords duplicated across functions → module-level constants

Features Implemented (per design spec):
  - Ping sparklines: Discord, Ollama, Bluesky, DNS 1.1.1.1
  - Service watchdog: ollama, kaiacord, NetworkManager
  - Thermals: CPU temp/hotspot/power/throttle, GPU temp/power/fan/VRAM
  - Split-lock detection with deduplication
  - Severity-colored logs with All/Net/Hw filter modes
  - Pause mode, adaptive LPS bar, clean scrolling
  - Q/C/P/F/R keyboard controls

Dependencies: stdlib, psutil, optional pynvml
Target: Ryzen 9600X + RTX 3060 Linux workstation
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
import glob
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple
from collections import deque

import psutil

# Optional GPU library
try:
    import pynvml
    HAS_PYNVML = True
except ImportError:
    HAS_PYNVML = False


# ==================== CONSTANTS ====================

# Timing intervals (seconds)
FRAME_INTERVAL_MS: int = 100          # ~10 FPS via stdscr.timeout()
PING_INTERVAL: float = 2.0
SERVICE_POLL_INTERVAL: float = 2.0
TELEMETRY_INTERVAL: float = 1.0
COLLECTOR_SHUTDOWN_TIMEOUT: float = 2.0

# Buffer sizes
LOG_BUFFER_SIZE: int = 500
SPARKLINE_LENGTH: int = 10

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

# Ping targets: name → (method, target)
PING_TARGETS: Dict[str, Tuple[str, str]] = {
    "Discord": ("http", "https://discord.com"),
    "Ollama": ("http", "http://localhost:11434"),
    "Bluesky": ("http", "https://bsky.social"),
    "DNS 1.1.1.1": ("socket", "1.1.1.1"),
}

# Monitored systemd services
MONITORED_SERVICES: Tuple[str, ...] = (
    "ollama.service",
    "kaiacord.service",
    "NetworkManager.service",
)

# Fallback process-name search terms per service
SERVICE_SEARCH_TERMS: Dict[str, str] = {
    "ollama.service": "ollama",
    "kaiacord.service": "kaiacord",
    "NetworkManager.service": "networkmanager",
}

# Log filter keyword sets (frozen for thread safety)
NET_FILTER_KEYWORDS: frozenset = frozenset({
    "ping", "dns", "connection", "socket", "discord", "bluesky",
    "http", "wifi", "ethernet", "resolved", "network",
})
HW_FILTER_KEYWORDS: frozenset = frozenset({
    "temp", "thermal", "throttle", "split lock", "nvidia", "cpu",
    "gpu", "fan", "sensor", "limit", "power", "bus_lock",
})

# Log severity classification keywords
ERROR_KEYWORDS: frozenset = frozenset({
    "ERROR", "FAIL", "CRITICAL", "SEGFAULT", "BUS_LOCK", "ALARM",
})
WARN_KEYWORDS: frozenset = frozenset({"WARN", "WARNING"})
SUCCESS_KEYWORDS: frozenset = frozenset({"SUCCESS", "STARTING", "STARTED"})

# Split-lock detection patterns
SPLIT_LOCK_PATTERN = re.compile(r"split\s+lock", re.IGNORECASE)
SPLIT_LOCK_APP_PATTERN = re.compile(
    r"split\s+lock.*?[\s:/](\S+?)(?:\[|\s|$)", re.IGNORECASE,
)

# HTTP user-agent for ping requests (prevents 403 blocks)
HTTP_USER_AGENT: str = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"


# ==================== DATACLASSES ====================

@dataclass
class ServiceState:
    """Mutable service state owned by the service collector."""
    name: str
    active: bool = False
    substate: str = "inactive"
    pid: int = 0
    restarts: int = 0
    cpu: float = 0.0
    memory_mb: float = 0.0


@dataclass
class LogEntry:
    """A single parsed journal log entry."""
    timestamp: str
    level: str      # "INFO" | "WARN" | "ERROR" | "SUCCESS"
    message: str


@dataclass(frozen=True)
class KaiamonSnapshot:
    """
    Immutable snapshot of all application state for rendering.

    Created once per frame by CollectorManager.take_snapshot().
    The UI thread renders exclusively from this object and never
    inspects collector-owned structures directly.
    """
    pings: Dict[str, Tuple[float, ...]] = field(default_factory=dict)
    services: Tuple[ServiceState, ...] = field(default_factory=tuple)
    cpu_temp: float = 0.0
    cpu_hotspot: float = 0.0
    cpu_power: float = 0.0
    cpu_throttled: bool = False
    gpu_temp: float = 0.0
    gpu_power: float = 0.0
    gpu_fan: float = 0.0
    gpu_mem_used: float = 0.0
    split_lock_events: int = 0
    split_lock_last_app: str = "None"
    logs: Tuple[LogEntry, ...] = field(default_factory=tuple)
    lps: float = 0.0
    lps_scale: float = 5.0
    filter_mode: str = "All"
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
    """Calculates pane positions for the four-panel layout."""

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

        self.panes["pings"] = Pane(
            y=0, x=0, height=top_height, width=left_width,
            title="PINGS (MS)", color_pair=1,
        )
        self.panes["services"] = Pane(
            y=0, x=left_width, height=top_height, width=middle_width,
            title="SERVICES", color_pair=2,
        )
        self.panes["thermals"] = Pane(
            y=0, x=left_width + middle_width, height=top_height,
            width=right_width, title="THERMALS", color_pair=1,
        )

        logs_height = height - top_height
        menu_footer = "[Q]uit ╭─╮ [C]lear ╭─╮ [P]ause ╭─╮ [F]ilter"
        self.panes["logs"] = Pane(
            y=top_height, x=0, height=logs_height, width=width,
            title="SYSTEM LOGS", footer=menu_footer, color_pair=1,
        )
        return True


# ==================== COLLECTORS ====================

class PingCollector:
    """Measures network latency to configured targets via HTTP HEAD / TCP socket."""

    def __init__(self, stop_event: threading.Event) -> None:
        self._stop = stop_event
        self._lock = threading.Lock()
        self._data: Dict[str, deque] = {
            name: deque([0.0] * SPARKLINE_LENGTH, maxlen=SPARKLINE_LENGTH)
            for name in PING_TARGETS
        }
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Launch the collector thread."""
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="ping-collector",
        )
        self._thread.start()

    def get_data(self) -> Dict[str, Tuple[float, ...]]:
        """Return an immutable copy of current ping data."""
        with self._lock:
            return {name: tuple(vals) for name, vals in self._data.items()}

    # --- measurement helpers ---

    @staticmethod
    def _measure_http(url: str) -> float:
        """Measure HTTP HEAD round-trip time in milliseconds.  Returns -1.0 on failure."""
        t0 = time.perf_counter()
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": HTTP_USER_AGENT}, method="HEAD",
            )
            try:
                with urllib.request.urlopen(req, timeout=1.0) as res:
                    res.read()
            except urllib.error.HTTPError:
                pass  # HTTP error still indicates a successful network round-trip
            return (time.perf_counter() - t0) * 1000.0
        except Exception:
            return -1.0

    @staticmethod
    def _measure_socket(host: str, port: int = 53) -> float:
        """Measure TCP connection time in milliseconds.  Returns -1.0 on failure."""
        t0 = time.perf_counter()
        try:
            with socket.create_connection((host, port), timeout=1.0):
                pass
            return (time.perf_counter() - t0) * 1000.0
        except Exception:
            return -1.0

    # --- thread body ---

    def _run(self) -> None:
        while not self._stop.is_set():
            for name, (method, target) in PING_TARGETS.items():
                if self._stop.is_set():
                    return
                if method == "http":
                    latency = self._measure_http(target)
                else:
                    latency = self._measure_socket(target)
                with self._lock:
                    self._data[name].append(
                        max(0.0, latency) if latency >= 0 else -1.0,
                    )
            self._stop.wait(PING_INTERVAL)


class ServiceCollector:
    """Monitors systemd service health via systemctl + psutil fallback."""

    def __init__(self, stop_event: threading.Event) -> None:
        self._stop = stop_event
        self._lock = threading.Lock()
        self._services: Dict[str, ServiceState] = {
            name: ServiceState(name=name) for name in MONITORED_SERVICES
        }
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Launch the collector thread."""
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="service-collector",
        )
        self._thread.start()

    def get_data(self) -> Tuple[ServiceState, ...]:
        """Return defensive copies of current service states."""
        with self._lock:
            return tuple(
                ServiceState(
                    name=s.name, active=s.active, substate=s.substate,
                    pid=s.pid, restarts=s.restarts, cpu=s.cpu,
                    memory_mb=s.memory_mb,
                )
                for s in self._services.values()
            )

    def _run(self) -> None:
        while not self._stop.is_set():
            for sname in MONITORED_SERVICES:
                if self._stop.is_set():
                    return
                self._poll_service(sname)
            self._stop.wait(SERVICE_POLL_INTERVAL)

    def _poll_service(self, sname: str) -> None:
        """Poll a single service for status and resource usage."""
        active, substate, pid, restarts = False, "inactive", 0, 0
        cpu, mem_mb = 0.0, 0.0

        # Primary: systemctl query
        try:
            result = subprocess.run(
                ["systemctl", "show", sname,
                 "-p", "ActiveState,SubState,MainPID,NRestarts"],
                capture_output=True, text=True, timeout=1.0,
            )
            if result.returncode == 0:
                props: Dict[str, str] = {}
                for line in result.stdout.strip().split("\n"):
                    if "=" in line:
                        k, v = line.split("=", 1)
                        props[k.strip()] = v.strip()
                active = props.get("ActiveState") == "active"
                substate = props.get("SubState", "dead")
                pid = int(props.get("MainPID", "0"))
                restarts = int(props.get("NRestarts", "0"))
        except (subprocess.TimeoutExpired, FileNotFoundError, ValueError):
            pass
        except Exception:
            pass

        # Fallback: search running processes if systemd reports inactive
        if not active or pid == 0:
            search = SERVICE_SEARCH_TERMS.get(sname, sname.split(".")[0])
            try:
                for proc in psutil.process_iter(["pid", "name", "cmdline"]):
                    try:
                        cmdline = proc.info.get("cmdline") or []
                        if any(search in arg.lower() for arg in cmdline):
                            active, substate, pid = True, "running", proc.info["pid"]
                            break
                    except (psutil.NoSuchProcess, psutil.AccessDenied,
                            psutil.ZombieProcess):
                        continue
            except Exception:
                pass

        # Get process resource usage
        if active and pid > 0:
            try:
                p = psutil.Process(pid)
                cpu = p.cpu_percent(interval=None)
                mem_mb = p.memory_info().rss / (1024.0 * 1024.0)
            except (psutil.NoSuchProcess, psutil.AccessDenied,
                    psutil.ZombieProcess):
                pass
            except Exception:
                pass

        with self._lock:
            self._services[sname] = ServiceState(
                name=sname, active=active, substate=substate,
                pid=pid, restarts=restarts, cpu=cpu, memory_mb=mem_mb,
            )



class TelemetryCollector:
    """Collects CPU/GPU thermal, power, and throttle data from sysfs and NVML."""

    def __init__(self, stop_event: threading.Event) -> None:
        self._stop = stop_event
        self._lock = threading.Lock()
        # CPU readings
        self._cpu_temp: float = 0.0
        self._cpu_hotspot: float = 0.0
        self._cpu_power: float = 0.0
        self._cpu_throttled: bool = False
        # GPU readings
        self._gpu_temp: float = 0.0
        self._gpu_power: float = 0.0
        self._gpu_fan: float = 0.0
        self._gpu_mem_used: float = 0.0
        # Split-lock (updated by LogCollector via update_split_lock)
        self._split_lock_events: int = 0
        self._split_lock_last_app: str = "None"
        # Hardware paths (discovered once at startup)
        self._k10temp_path: Optional[str] = None
        self._rapl_path: Optional[str] = None
        self._rapl_is_energy: bool = False  # True=energy_uj, False=power1_input
        self._throttle_paths: List[str] = []
        self._prev_throttle_count: int = 0
        # GPU handles
        self._nvml_handle = None
        self._has_nvml: bool = False
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Discover hardware paths and launch the collector thread."""
        self._discover_hardware()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="telemetry-collector",
        )
        self._thread.start()

    def get_data(self) -> dict:
        """Return a plain dict copy of current telemetry readings."""
        with self._lock:
            return {
                "cpu_temp": self._cpu_temp,
                "cpu_hotspot": self._cpu_hotspot,
                "cpu_power": self._cpu_power,
                "cpu_throttled": self._cpu_throttled,
                "gpu_temp": self._gpu_temp,
                "gpu_power": self._gpu_power,
                "gpu_fan": self._gpu_fan,
                "gpu_mem_used": self._gpu_mem_used,
                "split_lock_events": self._split_lock_events,
                "split_lock_last_app": self._split_lock_last_app,
            }

    def update_split_lock(self, events: int, last_app: str) -> None:
        """Thread-safe update from LogCollector's split-lock parser."""
        with self._lock:
            self._split_lock_events = events
            self._split_lock_last_app = last_app

    def shutdown_nvml(self) -> None:
        """Release NVML resources."""
        if self._has_nvml:
            try:
                pynvml.nvmlShutdown()
            except Exception:
                pass

    # --- hardware discovery (runs once at startup) ---

    def _discover_hardware(self) -> None:
        """Probe sysfs for sensor paths and initialize GPU libraries."""
        # k10temp hwmon path (AMD Ryzen)
        for path in glob.glob("/sys/class/hwmon/hwmon*"):
            try:
                with open(os.path.join(path, "name"), "r") as f:
                    if f.read().strip() == "k10temp":
                        self._k10temp_path = path
                        break
            except OSError:
                continue

        # RAPL energy path — try Intel then AMD powercap
        for candidate in (
            "/sys/class/powercap/intel-rapl:0/energy_uj",
            "/sys/class/powercap/amd-rapl:0/energy_uj",
        ):
            if os.path.exists(candidate):
                self._rapl_path = candidate
                self._rapl_is_energy = True
                break

        # Fallback: hwmon power1_input (AMD Ryzen 7000/9000 via k10temp)
        if not self._rapl_path:
            for path in glob.glob("/sys/class/hwmon/hwmon*"):
                power_path = os.path.join(path, "power1_input")
                if os.path.exists(power_path):
                    try:
                        with open(os.path.join(path, "name"), "r") as f:
                            name = f.read().strip()
                        if name in ("k10temp", "amdgpu"):
                            self._rapl_path = power_path
                            self._rapl_is_energy = False
                            break
                    except OSError:
                        continue

        # Kernel thermal throttle sysfs paths
        for cpu_dir in sorted(
            glob.glob("/sys/devices/system/cpu/cpu*/thermal_throttle"),
        ):
            for counter in ("package_throttle_count", "core_throttle_count"):
                p = os.path.join(cpu_dir, counter)
                if os.path.exists(p):
                    self._throttle_paths.append(p)
        self._prev_throttle_count = self._read_total_throttle_count()

        # Initialize NVML (pynvml)
        if HAS_PYNVML:
            try:
                pynvml.nvmlInit()
                self._nvml_handle = pynvml.nvmlDeviceGetHandleByIndex(0)
                self._has_nvml = True
            except Exception:
                self._has_nvml = False

    # --- CPU readings ---

    def _read_total_throttle_count(self) -> int:
        """Sum all kernel throttle counters."""
        total = 0
        for path in self._throttle_paths:
            try:
                with open(path, "r") as f:
                    total += int(f.read().strip())
            except (OSError, ValueError):
                continue
        return total

    def _read_cpu_temps(self) -> Tuple[float, float]:
        """Read CPU temperatures.  Returns (tctl, tccd1)."""
        tctl, tccd1 = 0.0, 0.0
        if self._k10temp_path:
            try:
                with open(os.path.join(self._k10temp_path, "temp1_input")) as f:
                    tctl = float(f.read().strip()) / 1000.0
            except (OSError, ValueError):
                pass
            try:
                with open(os.path.join(self._k10temp_path, "temp3_input")) as f:
                    tccd1 = float(f.read().strip()) / 1000.0
            except (OSError, ValueError):
                tccd1 = tctl
        # Fallback: psutil sensors
        if tctl == 0.0:
            try:
                temps = psutil.sensors_temperatures()
                for source in ("k10temp", "coretemp", "cpu_thermal"):
                    if source in temps and temps[source]:
                        tctl = temps[source][0].current
                        tccd1 = tctl
                        break
            except Exception:
                pass
        return tctl, tccd1

    def _read_cpu_power(self) -> float:
        """Read CPU power via RAPL or estimate from utilization."""
        # RAPL energy_uj path (requires two samples)
        if self._rapl_path and self._rapl_is_energy:
            try:
                with open(self._rapl_path, "r") as f:
                    e1 = float(f.read().strip())
                t1 = time.perf_counter()
                time.sleep(0.1)
                with open(self._rapl_path, "r") as f:
                    e2 = float(f.read().strip())
                dt = time.perf_counter() - t1
                if dt > 0:
                    power = (e2 - e1) / (1e6 * dt)
                    if power > 0:
                        return power
            except (OSError, ValueError):
                pass
        # Direct power1_input path (microwatts)
        elif self._rapl_path and not self._rapl_is_energy:
            try:
                with open(self._rapl_path, "r") as f:
                    return float(f.read().strip()) / 1e6
            except (OSError, ValueError):
                pass
        # Estimation fallback
        try:
            cpu_util = psutil.cpu_percent(interval=None)
            freq = psutil.cpu_freq()
            curr_f = freq.current if freq else 4000.0
            max_f = (freq.max if freq and freq.max else 5486.0)
            return 15.0 + (88.0 - 15.0) * (cpu_util / 100.0) * (curr_f / max_f)
        except Exception:
            return 0.0

    def _check_throttling(self, tctl: float) -> bool:
        """Detect CPU throttling via kernel counters, with heuristic fallback."""
        if self._throttle_paths:
            current = self._read_total_throttle_count()
            if current > self._prev_throttle_count:
                self._prev_throttle_count = current
                return True
        # Heuristic fallback
        try:
            cpu_util = psutil.cpu_percent(interval=None)
            freq = psutil.cpu_freq()
            if freq:
                max_f = freq.max or 5486.0
                if tctl > 90.0 or (cpu_util > 90.0 and freq.current < max_f * 0.75):
                    return True
        except Exception:
            pass
        return False

    # --- GPU readings ---

    def _read_gpu(self) -> Tuple[float, float, float, float]:
        """Read GPU telemetry.  Returns (temp, power_w, fan_pct, mem_used_mb)."""
        # Priority 1: pynvml (subprocess-free)
        if self._has_nvml and self._nvml_handle:
            try:
                temp = float(pynvml.nvmlDeviceGetTemperature(
                    self._nvml_handle, pynvml.NVML_TEMPERATURE_GPU,
                ))
                power = float(pynvml.nvmlDeviceGetPowerUsage(
                    self._nvml_handle,
                )) / 1000.0
                try:
                    fan = float(pynvml.nvmlDeviceGetFanSpeed(self._nvml_handle))
                except pynvml.NVMLError:
                    fan = 0.0  # Not supported on some GPU models
                mem = pynvml.nvmlDeviceGetMemoryInfo(self._nvml_handle)
                return temp, power, fan, float(mem.used) / (1024.0 * 1024.0)
            except Exception:
                pass

        # Priority 2: nvidia-smi subprocess
        try:
            result = subprocess.run(
                ["nvidia-smi",
                 "--query-gpu=temperature.gpu,power.draw,fan.speed,memory.used",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=1.0,
            )
            if result.returncode == 0:
                parts = [v.strip() for v in result.stdout.strip().split(",")]
                if len(parts) >= 4:

                    def _safe_float(s: str) -> float:
                        try:
                            return float(s)
                        except (ValueError, TypeError):
                            return 0.0

                    return (
                        _safe_float(parts[0]),
                        _safe_float(parts[1]),
                        _safe_float(parts[2]),
                        _safe_float(parts[3]),
                    )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        except Exception:
            pass
        # Priority 3: degraded mode (no GPU data)
        return 0.0, 0.0, 0.0, 0.0

    # --- thread body ---

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                tctl, tccd1 = self._read_cpu_temps()
                cpu_power = self._read_cpu_power()
                throttled = self._check_throttling(tctl)
                gpu_temp, gpu_power, gpu_fan, gpu_mem = self._read_gpu()

                with self._lock:
                    self._cpu_temp = tctl
                    self._cpu_hotspot = tccd1
                    self._cpu_power = cpu_power
                    self._cpu_throttled = throttled
                    self._gpu_temp = gpu_temp
                    self._gpu_power = gpu_power
                    self._gpu_fan = gpu_fan
                    self._gpu_mem_used = gpu_mem
            except Exception:
                pass  # Never crash the collector
            self._stop.wait(TELEMETRY_INTERVAL)


class LogCollector:
    """
    Streams and parses journalctl logs with non-blocking select()-based reads.

    Owns all log state including the LPS (logs-per-second) tracking deque
    and the adaptive LPS scaling logic.  The split-lock parser forwards
    events to TelemetryCollector for display in the thermals pane.
    """

    def __init__(self, stop_event: threading.Event) -> None:
        self._stop = stop_event
        self._lock = threading.Lock()
        self._logs: deque = deque(maxlen=LOG_BUFFER_SIZE)
        self._lps_timestamps: deque = deque()       # float timestamps only
        self._lps_peak_history: deque = deque()      # (timestamp, lps_value)
        self._split_lock_events: int = 0
        self._split_lock_last_app: str = "None"
        self._split_lock_seen: Set[str] = set()      # dedup keys
        self._paused: bool = False
        self._filter_mode: str = "All"
        self._process: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Launch the collector thread."""
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="log-collector",
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

    def get_data(self) -> Tuple[Tuple[LogEntry, ...], float, float, str, bool]:
        """Returns (logs, lps, lps_scale, filter_mode, paused)."""
        with self._lock:
            logs = tuple(self._logs)
            lps = self._calculate_logs_per_second()
            scale = self._calculate_lps_scale(lps)
            return logs, lps, scale, self._filter_mode, self._paused

    def get_split_lock_data(self) -> Tuple[int, str]:
        """Return current split-lock counters."""
        with self._lock:
            return self._split_lock_events, self._split_lock_last_app

    # --- LPS computation ---

    def _calculate_logs_per_second(self) -> float:
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
        return max(LPS_MIN_SCALE, peak * 1.2)  # 20 % headroom

    # --- line parsing ---

    def _parse_line(self, line: str) -> Optional[LogEntry]:
        """Parse a raw journalctl line into a LogEntry, or None to skip."""
        stripped = line.strip()
        if not stripped:
            return None
        # Filter out Ollama noise
        if re.search(r"ollama", stripped, re.IGNORECASE):
            return None

        # Extract timestamp and payload
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

        # Classify severity
        msg_upper = msg.upper()
        if any(k in msg_upper for k in ERROR_KEYWORDS):
            level = "ERROR"
        elif any(k in msg_upper for k in WARN_KEYWORDS):
            level = "WARN"
        elif any(k in msg_upper for k in SUCCESS_KEYWORDS):
            level = "SUCCESS"
        else:
            level = "INFO"

        # Detect split-lock events with deduplication
        if SPLIT_LOCK_PATTERN.search(msg):
            app_match = SPLIT_LOCK_APP_PATTERN.search(msg)
            app_name = app_match.group(1) if app_match else "Unknown"
            dedup_key = f"{ts}:{app_name}"
            if dedup_key not in self._split_lock_seen:
                self._split_lock_seen.add(dedup_key)
                self._split_lock_events += 1
                self._split_lock_last_app = app_name
                # Prevent unbounded growth of dedup set
                if len(self._split_lock_seen) > 1000:
                    self._split_lock_seen.clear()

        return LogEntry(timestamp=ts, level=level, message=msg)

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
            return  # journalctl not available on this system

        try:
            while not self._stop.is_set():
                ready, _, _ = select.select(
                    [self._process.stdout], [], [], 0.5,
                )
                if not ready:
                    continue
                line = self._process.stdout.readline()
                if not line:
                    break  # EOF — journalctl process exited

                entry = self._parse_line(line)
                if entry is None:
                    continue

                with self._lock:
                    if not self._paused:
                        self._logs.append(entry)
                        self._lps_timestamps.append(time.time())
        except Exception:
            pass  # Never crash the collector



# ==================== COLLECTOR MANAGER ====================

class CollectorManager:
    """
    Centralized lifecycle manager for all data collectors.

    Responsibilities:
      - Start / stop all collector threads
      - Produce immutable KaiamonSnapshot objects for the UI
      - Manage subprocess and NVML cleanup
    """

    def __init__(self) -> None:
        self._stop_event = threading.Event()
        self._ping = PingCollector(self._stop_event)
        self._service = ServiceCollector(self._stop_event)
        self._telemetry = TelemetryCollector(self._stop_event)
        self._log = LogCollector(self._stop_event)

    @property
    def log_collector(self) -> LogCollector:
        """Expose log collector for pause/filter/clear commands."""
        return self._log

    def start_all(self) -> None:
        """Start every collector thread."""
        self._ping.start()
        self._service.start()
        self._telemetry.start()
        self._log.start()

    def stop_all(self) -> None:
        """Signal shutdown and release all resources."""
        self._stop_event.set()
        self._log.stop()
        self._telemetry.shutdown_nvml()

    def take_snapshot(self) -> KaiamonSnapshot:
        """
        Create an immutable snapshot of all collector data.

        This is the ONLY place where collector-owned state is read.
        The returned object is safe to use from the UI thread without locks.
        """
        pings = self._ping.get_data()
        services = self._service.get_data()
        telemetry = self._telemetry.get_data()
        logs, lps, lps_scale, filter_mode, paused = self._log.get_data()
        sl_events, sl_app = self._log.get_split_lock_data()

        # Forward split-lock data to telemetry for snapshot consistency
        self._telemetry.update_split_lock(sl_events, sl_app)

        return KaiamonSnapshot(
            pings=pings,
            services=services,
            cpu_temp=telemetry["cpu_temp"],
            cpu_hotspot=telemetry["cpu_hotspot"],
            cpu_power=telemetry["cpu_power"],
            cpu_throttled=telemetry["cpu_throttled"],
            gpu_temp=telemetry["gpu_temp"],
            gpu_power=telemetry["gpu_power"],
            gpu_fan=telemetry["gpu_fan"],
            gpu_mem_used=telemetry["gpu_mem_used"],
            split_lock_events=sl_events,
            split_lock_last_app=sl_app,
            logs=logs,
            lps=lps,
            lps_scale=lps_scale,
            filter_mode=filter_mode,
            paused=paused,
        )


# ==================== UI ====================

class KaiamonUI:
    """
    Production-quality curses UI for Kaiamon.

    Architecture rules (matching btop_dashboard_v2.py):
      1. curses runs ONLY in this class, ONLY in the main thread
      2. Rendering is driven exclusively by immutable KaiamonSnapshot objects
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
        curses.init_pair(3, curses.COLOR_GREEN, -1)      # Good / success
        curses.init_pair(4, curses.COLOR_YELLOW, -1)     # Warning
        curses.init_pair(5, curses.COLOR_RED, -1)        # Error / critical
        curses.init_pair(6, -1, -1)                      # Default text
        curses.init_pair(7, curses.COLOR_BLUE, -1)       # Info

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
                pass  # Never crash the UI loop

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
                modes = ("All", "Net", "Hw")
                idx = (modes.index(lc.filter_mode) + 1) % len(modes)
                lc.filter_mode = modes[idx]
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

        # --- top border with title ---
        title_str = f" {pane.title} " if pane.title else ""
        if len(title_str) > w - 4:
            title_str = title_str[:w - 4]
        left_h = (w - 2 - len(title_str)) // 2
        right_h = w - 2 - len(title_str) - left_h
        top = BOX["tl"] + BOX["h"] * left_h + title_str + BOX["h"] * right_h + BOX["tr"]
        self._safe_addstr(y, x, top, color)

        # --- vertical borders + clear interior ---
        for i in range(1, h - 1):
            self._safe_addstr(y + i, x, BOX["v"], color)
            self._safe_addstr(y + i, x + 1, " " * (w - 2))
            self._safe_addstr(y + i, x + w - 1, BOX["v"], color)

        # --- bottom border with footer ---
        footer_str = f" {pane.footer} " if pane.footer else ""
        if len(footer_str) > w - 4:
            footer_str = footer_str[:w - 4]
        left_f = (w - 2 - len(footer_str)) // 2
        right_f = w - 2 - len(footer_str) - left_f
        bot = BOX["bl"] + BOX["h"] * left_f + footer_str + BOX["h"] * right_f + BOX["br"]
        self._safe_addstr(y + h - 1, x, bot, color)

    def _make_sparkline(self, data: Tuple[float, ...]) -> str:
        """Generate a Unicode sparkline from a tuple of float values."""
        if not data:
            return " " * SPARKLINE_LENGTH
        vals = list(data)
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

    # ==================== Pane Drawing ====================

    def _draw_pings_pane(self, snap: KaiamonSnapshot) -> None:
        """Draw the network latency pings panel."""
        pane = self._layout.panes.get("pings")
        if not pane:
            return
        self._draw_box(pane)
        py = pane.y + 1
        px = pane.x + 2
        p_w = pane.width - 4

        for name, vals in snap.pings.items():
            if py >= pane.y + pane.height - 1:
                break
            latest = vals[-1] if vals else 0.0
            disp_name = name[:12].ljust(12)

            if latest < 0.001 or latest < 0:
                val_str = "ERR".rjust(6)
                color = curses.color_pair(5)
                spark = " " * SPARKLINE_LENGTH
            else:
                val_str = f"{int(latest)}ms".rjust(6)
                if latest < 50:
                    color = curses.color_pair(3)
                elif latest < 150:
                    color = curses.color_pair(4)
                else:
                    color = curses.color_pair(5)
                spark = self._make_sparkline(vals)

            spark_w = p_w - len(disp_name) - len(val_str) - 2
            if spark_w > 0:
                row = f"{disp_name} {val_str}  {spark[-spark_w:]}"
            else:
                row = f"{disp_name} {val_str}"
            self._safe_addstr(py, px, row[:p_w], color)
            py += 1

    def _draw_services_pane(self, snap: KaiamonSnapshot) -> None:
        """Draw the service watchdog panel."""
        pane = self._layout.panes.get("services")
        if not pane:
            return
        self._draw_box(pane)
        sy = pane.y + 1
        sx = pane.x + 2
        s_w = pane.width - 4

        for srv in snap.services:
            if sy >= pane.y + pane.height - 1:
                break
            short = srv.name.replace(".service", "")
            disp = short[:10].ljust(10)

            if srv.active and srv.substate == "running":
                dot_color = curses.color_pair(3)
            elif srv.active:
                dot_color = curses.color_pair(4)
            else:
                dot_color = curses.color_pair(5)

            if srv.active and srv.pid > 0:
                stats = f" {srv.cpu:4.1f}% {int(srv.memory_mb)}M"
            else:
                stats = " offline"
            restart_tag = f" R:{srv.restarts}" if srv.restarts > 0 else ""

            self._safe_addstr(sy, sx, disp[:8].ljust(8),
                              curses.color_pair(2) | curses.A_BOLD)
            self._safe_addstr(sy, sx + 9, "●", dot_color)
            remaining = s_w - 11
            self._safe_addstr(sy, sx + 11,
                              (stats + restart_tag)[:remaining],
                              curses.color_pair(6))
            sy += 1

    def _draw_thermals_pane(self, snap: KaiamonSnapshot) -> None:
        """Draw the hardware thermals and limits panel."""
        pane = self._layout.panes.get("thermals")
        if not pane:
            return
        self._draw_box(pane)
        ty = pane.y + 1
        tx = pane.x + 2
        tw = pane.width - 4

        cpu_t = f"CPU Temp:  {snap.cpu_temp:4.1f}\u00b0C / {snap.cpu_hotspot:4.1f}\u00b0C"
        self._safe_addstr(ty, tx, cpu_t[:tw],
                          curses.color_pair(1) | curses.A_BOLD)
        ty += 1

        cpu_p = f"CPU Power: {snap.cpu_power:4.1f} W"
        self._safe_addstr(ty, tx, cpu_p[:tw], curses.color_pair(2))
        ty += 1

        if snap.cpu_throttled:
            self._safe_addstr(ty, tx, "CPU LIMIT: THROTTLED"[:tw],
                              curses.color_pair(5) | curses.A_BOLD)
        else:
            self._safe_addstr(ty, tx, "CPU Limit: Nominal"[:tw],
                              curses.color_pair(3))
        ty += 1

        gpu_t = f"GPU Temp:  {snap.gpu_temp:2.0f}\u00b0C  Fan: {snap.gpu_fan:2.0f}%"
        self._safe_addstr(ty, tx, gpu_t[:tw],
                          curses.color_pair(1) | curses.A_BOLD)
        ty += 1

        gpu_p = (f"GPU Power: {snap.gpu_power:3.0f}W   "
                 f"VRAM: {snap.gpu_mem_used / 1024.0:3.1f}G/12G")
        self._safe_addstr(ty, tx, gpu_p[:tw], curses.color_pair(2))
        ty += 1

        if snap.split_lock_events > 0:
            sl = (f"SPLIT-LOCK: {snap.split_lock_events} "
                  f"({snap.split_lock_last_app})")
            self._safe_addstr(ty, tx, sl[:tw],
                              curses.color_pair(5) | curses.A_BOLD)
        else:
            self._safe_addstr(ty, tx, "Split-Lock: 0 events"[:tw],
                              curses.color_pair(3))

    def _draw_logs_pane(self, snap: KaiamonSnapshot) -> None:
        """Draw the system logs panel with severity coloring and LPS bar."""
        pane = self._layout.panes.get("logs")
        if not pane:
            return

        # Build dynamic title with adaptive LPS bar
        lps_ratio = (min(1.0, snap.lps / snap.lps_scale)
                     if snap.lps_scale > 0 else 0.0)
        filled = int(lps_ratio * LPS_BAR_WIDTH)
        bar = LPS_BAR_FILLED * filled + LPS_BAR_EMPTY * (LPS_BAR_WIDTH - filled)
        pane.title = (f"SYSTEM LOGS \u2500 LPS: {snap.lps:4.1f} [{bar}] "
                      f"\u2500 Filter: {snap.filter_mode}")
        if snap.paused:
            pane.title += " \u2500 PAUSED"

        self._draw_box(pane)
        ly = pane.y + 1
        lx = pane.x + 2
        l_h = pane.height - 2
        l_w = pane.width - 4

        # Apply filter to log snapshot
        filtered: List[LogEntry] = []
        for log in snap.logs:
            if snap.filter_mode == "Net":
                if not any(k in log.message.lower() for k in NET_FILTER_KEYWORDS):
                    continue
            elif snap.filter_mode == "Hw":
                if not any(k in log.message.lower() for k in HW_FILTER_KEYWORDS):
                    continue
            filtered.append(log)

        # Show most recent entries that fit
        for log in filtered[-l_h:]:
            if ly >= pane.y + pane.height - 1:
                break
            if log.level == "ERROR":
                l_color = curses.color_pair(5) | curses.A_BOLD
            elif log.level == "WARN":
                l_color = curses.color_pair(4) | curses.A_BOLD
            elif log.level == "SUCCESS":
                l_color = curses.color_pair(3) | curses.A_BOLD
            else:
                l_color = curses.color_pair(6)

            self._safe_addstr(ly, lx, f"{log.timestamp} ",
                              curses.color_pair(1))
            self._safe_addstr(ly, lx + 9, log.message[:l_w - 9], l_color)
            ly += 1

    # ==================== Frame Composition ====================

    def _draw_frame(self, snap: KaiamonSnapshot) -> None:
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

            self._draw_pings_pane(snap)
            self._draw_services_pane(snap)
            self._draw_thermals_pane(snap)
            self._draw_logs_pane(snap)
        except curses.error:
            pass


# ==================== ENTRY POINT ====================

def main() -> None:
    """Launch the Kaiamon system companion."""
    ui = KaiamonUI()
    ui.run()


if __name__ == "__main__":
    main()

