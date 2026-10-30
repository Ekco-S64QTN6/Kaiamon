# Kaiamon

Kaiamon is a Linux terminal process monitor with an **htop-style, live process table**. It reads process and system metrics from `/proc`, updates them in a background collector, and renders immutable snapshots through a curses interface. The current implementation focuses on the process-monitor core rather than the former multi-panel system dashboard.

## Status

The first process-monitor milestone is implemented: live process enumeration, delta-based CPU usage, memory and CPU meters, sorting, text filtering, a parent/child tree, and basic signal/renice actions. See [ROADMAP.md](ROADMAP.md) for what remains before this should be considered a feature-complete htop replacement.

## Requirements

- Linux with a mounted `/proc` filesystem
- Python 3.10 or newer
- A terminal with curses support (the standard Python `curses` module)

The process monitor itself uses only the Python standard library. It does not require root to list processes, though permissions can restrict command lines and process actions. The archived dashboard in `legacy_dashboard.py` retains its previous optional `psutil` and `pynvml` dependencies and is not started by the process-monitor entry point.

## Installation

```bash
git clone https://github.com/Ekco-S64QTN6/Kaiamon.git
cd Kaiamon
python3 kaiamon.py
```

For a source checkout, no package installation is required. Run it in a terminal at least 60 columns wide and 8 rows high; a larger terminal is recommended.

## Usage

```bash
python3 kaiamon.py
```

The process table shows these columns by default. Use `--columns` to select a subset and change its order:

| Column | Meaning |
|---|---|
| `PID` | Process ID |
| `USER` | Effective user from `/proc/<pid>/status` |
| `PRI` / `NI` | Scheduler priority and nice value |
| `VIRT` | Virtual address space |
| `RES` | Resident memory |
| `SHR` | Shared resident pages from `/proc/<pid>/statm` |
| `S` | Process state |
| `CPU%` | CPU use calculated from process and system counter deltas |
| `MEM%` | Resident memory as a percentage of physical memory |
| `TIME+` | Accumulated process CPU time |
| `COMMAND` | Command line, falling back to the process name when empty |

CPU utilization and memory utilization meters appear above the table. Compact per-core CPU meters appear beside the aggregate CPU meter (up to eight cores in the header). Processes that disappear or become inaccessible while sampling are skipped rather than terminating the collector.

## Keyboard controls

| Key | Action |
|---|---|
| `↑` / `↓` or `j` / `k` | Move selection |
| `Page Up` / `Page Down` | Scroll by ten rows |
| `Home` / `End` | Select first / last process |
| `s` or `F6` | Cycle sort direction and column (CPU, MEM, PID, USER, TIME, COMMAND) |
| `/` | Filter by PID, user, or command substring |
| `Esc` | Clear the filter |
| `t` or `F5` | Toggle parent/child tree view |
| `K` | Choose TERM, KILL, INT, or HUP for the selected process, then confirm with `y` (default is no) |
| `r` | Set the selected process nice value (-20 through 19) |
| `F1` | Show the keybinding summary in the status line |
| `←` / `-` | Collapse the selected tree node's children |
| `→` / `+` | Expand the selected tree node |
| `Space` | Toggle collapse/expand for the selected tree node |
| `q` | Quit |

Process actions are subject to normal Linux permissions. Kaiamon refuses to signal or renice PID 1 or its own process. Signal selection is explicit and every signal requires a second, default-no confirmation; declining it leaves the process untouched. `KILL` should be used only when a graceful termination is not appropriate.

## Configuration

The process sampling interval defaults to one second. Set it at launch with `--refresh` (seconds; minimum 0.05):

```bash
python3 kaiamon.py --refresh 0.5
```

Choose and reorder visible table columns with `--columns`. Column names are comma-separated and case-insensitive; accepted names are `PID`, `USER`, `PRI`, `NI`, `VIRT`, `RES`, `SHR`, `STATE` (or `S`), `CPU` (or `CPU%`), `MEM` (or `MEM%`), `TIME` (or `TIME+`), and `COMMAND`:

```bash
python3 kaiamon.py --columns PID,USER,CPU,MEM,COMMAND
python3 kaiamon.py --refresh 0.25 --columns PID,CPU,COMMAND
```

The sampling interval controls background `/proc` collection. The curses input/render loop remains responsive at its own short frame interval. There is not yet a persistent configuration file or environment-variable interface.

## Architecture

```text
/proc (stat, meminfo, <pid>/stat, status, statm, cmdline)
                         |
                         v
               ProcessCollector thread
                 - /proc reads only
                 - per-collector lock
                 - CPU deltas per PID
                         |
                         v
                frozen ProcessSnapshot
                         |
                         v
                ProcessMonitorUI
                 - main thread only
                 - curses rendering/input
                 - sort/filter/tree presentation
                 - validated process actions
```

`ProcessCollector` owns the polling thread, prior CPU counters, and its published snapshot. It skips common process races (`ENOENT`, permission errors, malformed or truncated pseudo-files). CPU percentages are derived from process CPU-time deltas divided by system CPU-time deltas, scaled by the number of online cores; they are not lifetime averages. The UI reads only the frozen `ProcessSnapshot`, and all curses calls remain in the main thread. The UI handles SIGINT and SIGTERM by exiting its main loop; `curses.wrapper` restores terminal mode on normal exit and exceptions, while the collector is stopped and the original signal handlers are restored in a `finally` block.

The previous ping, systemd-service, thermal, and journal collectors are preserved in `legacy_dashboard.py` for reference. They are not launched by the new process-centric entry point and will be reconsidered as optional secondary panels only after the process core has matured.

## Comparison with htop

**Supported in Kaiamon:** live process table; PID/user/priority/nice/virtual/resident/shared/state/CPU/memory/time/command fields; CPU and memory meters; per-core CPU meters; CPU/MEM/PID/USER/TIME/COMMAND sorting; substring filtering; parent/child tree display; signal selection; renice; terminal keyboard navigation.

**Different from htop:** Kaiamon currently reads Linux `/proc` directly and is Linux-only; columns and the sampling interval are configurable at launch rather than through a setup screen; process filtering is substring-based; per-core meters are compact and limited to the first eight cores in the header.

**Not yet implemented:** full htop setup/configuration screens, interactive column-width editing, process/thread toggles, CPU affinity, I/O and disk/network meters, GPU process metrics, custom color schemes, cross-platform backends, and a comprehensive interactive action-confirmation workflow. Kaiamon is htop-style, not yet a drop-in feature-equivalent replacement.

## Testing

Run the synthetic test suite without requiring root or a live host process list:

```bash
python -m compileall -q .
python -m pytest -q
```

The fixtures create small `/proc`-like trees and cover parsing, delta CPU calculation, process-disappearance races, immutable snapshot publication, sorting, filtering, tree construction, and process-action validation. Linux PTY integration tests verify terminal attributes are restored after `q`, SIGTERM, and an uncaught UI exception. GitHub Actions runs compileall, pytest, Ruff, and Mypy across Python 3.10–3.13. Optional local checks:

```bash
python -m ruff check .
python -m mypy .
```

## Contributing

Keep `/proc` access inside collectors and keep curses calls on the main thread. New collector output should be copied into frozen snapshot dataclasses before the UI sees it. Add fixture-based regression tests for parsing and race conditions; do not make the test suite depend on the host's live process list or privileged actions. Run the full test suite and relevant static checks before opening a pull request.

## Roadmap

See [ROADMAP.md](ROADMAP.md) for planned work and current limitations.

## License

Kaiamon is licensed under the [MIT License](LICENSE).
