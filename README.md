<div align="center">

# ⚡ Kaiamon

**Cyberpunk curses system companion for Linux workstations**

[![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-A31F34?style=flat-square)](LICENSE)
[![Platform: Linux](https://img.shields.io/badge/platform-Linux-FCC624?style=flat-square&logo=linux&logoColor=black)](https://www.kernel.org/)
[![Curses TUI](https://img.shields.io/badge/interface-curses%20TUI-00CED1?style=flat-square)](#)
[![Code Style](https://img.shields.io/badge/code%20style-PEP%208-000000?style=flat-square)](#)

<br>

*A standalone terminal dashboard that wraps system logs, hardware thermals, service health,*
*and network latency into a single pane — optimized for 80×24 tiled layouts.*

</div>

---

```
╭──── PINGS (MS) ────╮╭─── SERVICES ───╮╭──── THERMALS ────╮
│ Discord    32ms ▅█▆ ││ ollama   ● 2.1%││ CPU Temp: 48.2°C │
│ Ollama      1ms ▂▃█ ││ kaiacrd  ● 0.3%││ GPU Temp: 52°C   │
│ Bluesky   128ms █▇▅ ││ NetMgr   ● 0.0%││ CPU Power: 42 W  │
│ DNS 1.1.1   12ms ▄▅ ││                 ││ Split-Lock: 0    │
╰─────────────────────╯╰─────────────────╯╰──────────────────╯
╭─── SYSTEM LOGS ─ LPS: 2.4 [■■■■□□□□□□] ─ Filter: All ─────╮
│ 22:15:36 systemd: Started FireDragon browser.               │
│ 22:15:44 flatpak: Sandbox created for app.                  │
│ 22:15:51 kernel: usb 1-3: new device connected              │
╰──── [Q]uit ╭─╮ [C]lear ╭─╮ [P]ause ╭─╮ [F]ilter ──────────╯
```

---

## 📦 Installation

```bash
git clone https://github.com/ekco/Kaiamon.git
cd Kaiamon
pip install psutil
pip install pynvml  # optional — for NVIDIA GPU monitoring
```

> **Dependencies:** `psutil` (required) and `pynvml` (optional). No frameworks, no config files — just one Python script.

## 🚀 Usage

```bash
python3 kaiamon.py
```

### Keyboard Controls

| Key | Action |
|:---:|--------|
| `Q` | Quit — cleanly restores terminal |
| `C` | Clear log buffer and LPS history |
| `P` | Pause / resume log ingestion |
| `F` | Cycle filter: All → Network → Hardware |
| `R` | Force full redraw |

---

## ✨ Features

### 📡 Network Latency Sparklines
Real-time ping monitoring with rolling 10-point Unicode sparklines (`▂▃▄▅▆▇█`):

| Target | Method | Purpose |
|--------|--------|---------|
| Discord | HTTP HEAD | Gateway API latency |
| Ollama | HTTP HEAD | Local inference server |
| Bluesky | HTTP HEAD | BSky API endpoint |
| 1.1.1.1 | TCP socket | Cloudflare DNS / internet health |

### 🔧 Service Watchdog
Systemd service monitoring with `psutil` process-scan fallback:
- Live status indicator (● green / yellow / red)
- Per-service CPU % and memory usage
- Restart count tracking (surfaces silent crashes)
- Monitored: `ollama`, `kaiacord`, `NetworkManager`

### 🌡️ Hardware Thermals & Limits
Surfaces metrics that `btop` doesn't show:

| Metric | Source |
|--------|--------|
| CPU Tctl / Tccd1 | `k10temp` sysfs or `psutil` fallback |
| CPU Power Draw | RAPL (Intel + AMD) or estimation |
| CPU Throttling | Kernel `thermal_throttle` sysfs counters |
| GPU Temp / Power / Fan | `pynvml` → `nvidia-smi` → degraded |
| GPU VRAM | `pynvml` → `nvidia-smi` |
| Split-Lock Events | Kernel log parsing with deduplication |

### 📜 Filtered System Logs
- Streamed from `journalctl` with Ollama noise filtered out
- **Severity coloring** — 🔴 Error 🟡 Warning 🟢 Success ⚪ Info
- **Filter modes** — All / Network-only / Hardware-only
- **Pause mode** — Freeze log ingestion for reading
- **Adaptive LPS bar** — Visualizes log flood rate with rolling normalization

---

## 🏗️ Architecture

Kaiamon follows a **snapshot-based rendering** architecture with strict thread ownership boundaries:

```
 ┌─────────────┐  ┌────────────────┐  ┌──────────────────┐  ┌─────────────┐
 │    Ping      │  │    Service     │  │    Telemetry     │  │     Log     │
 │  Collector   │  │   Collector    │  │    Collector     │  │  Collector  │
 │ (HTTP/TCP)   │  │ (systemctl)    │  │ (sysfs/NVML)    │  │ (select())  │
 └──────┬───────┘  └──────┬────────┘  └───────┬──────────┘  └──────┬──────┘
        │                 │                    │                    │
        └────────┬────────┴────────────────────┴────────────┬──────┘
                 │                                          │
          ┌──────┴──────────────────────────────────────────┴──────┐
          │                   CollectorManager                     │
          │              take_snapshot() → frozen dataclass         │
          └────────────────────────┬───────────────────────────────┘
                                   │
                    ┌──────────────▼──────────────┐
                    │       KaiamonSnapshot       │
                    │   @dataclass(frozen=True)    │
                    └──────────────┬──────────────┘
                                   │
                    ┌──────────────▼──────────────┐
                    │         KaiamonUI           │
                    │   (main thread — curses)     │
                    └─────────────────────────────┘
```

**Key principles:**
- Collectors own their data — write behind per-collector locks
- UI reads only from **immutable snapshots** — never touches collector state
- `curses` lives exclusively in the main thread
- Terminal is **always** restored: `Q`, `Ctrl-C`, `SIGTERM`, uncaught exceptions

---

## 🖥️ Hardware Compatibility

| Component | Status | Notes |
|-----------|:------:|-------|
| AMD Ryzen (k10temp) | ✅ | Direct sysfs reads |
| Intel (coretemp) | ✅ | Via `psutil` fallback |
| NVIDIA GPU (pynvml) | ✅ | Preferred — no subprocess overhead |
| NVIDIA GPU (nvidia-smi) | ✅ | Automatic fallback |
| AMD GPU (amdgpu) | ⚠️ | Basic hwmon support |
| CPU Power (RAPL) | ✅ | Intel + AMD powercap paths |
| CPU Power (hwmon) | ✅ | Ryzen 7000/9000 via `power1_input` |
| Thermal Throttle | ✅ | Kernel sysfs + heuristic fallback |

> **Target system:** Ryzen 9600X + RTX 3060, but works on any Linux box with systemd.

---

## 🧩 Desktop Layout

Kaiamon is designed for the bottom-right quadrant of a tiled terminal grid:

```
┌───────────────────────────┬───────────────────────────┐
│                           │                           │
│     btop (Top-Left)       │   Kaiacord (Top-Right)    │
│                           │                           │
├───────────────────────────┼───────────────────────────┤
│                           │                           │
│   cmatrix (Bottom-Left)   │  Kaiamon (Bottom-Right)   │
│                           │                           │
└───────────────────────────┴───────────────────────────┘
```

---

## 📄 License

This project is licensed under the [MIT License](LICENSE).
