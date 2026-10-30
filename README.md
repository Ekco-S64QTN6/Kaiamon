# Kaiamon

Kaiamon is a Linux terminal process monitor inspired by **htop**. It presents a live, sortable and filterable process table in a curses interface, with CPU and memory meters, parent/child navigation, and guarded process controls.

The default application is process-centric. The former ping, systemd, thermal, and journal dashboard remains in `legacy_dashboard.py` for reference; it is not started by the process-monitor entry point.

## Requirements

- Linux with a mounted `/proc` filesystem
- Python 3.10 or newer
- A terminal with curses support (the standard-library `curses` module)

The process monitor uses only the Python standard library. Listing processes does not require root, although Linux permissions may limit visibility of details and process actions.

## Installation

```bash
git clone https://github.com/Ekco-S64QTN6/Kaiamon.git
cd Kaiamon
python3 kaiamon.py
```

No package installation is required for a source checkout. A terminal at least 60 columns wide and 8 rows high is required; a larger terminal is recommended.

## Usage

```bash
python3 kaiamon.py
```

The process table includes:

| Column | Meaning |
|---|---|
| `PID` | Process ID |
| `USER` | Effective user from `/proc/<pid>/status` |
| `PRI` / `NI` | Scheduler priority and nice value |
| `VIRT` | Virtual address space |
| `RES` | Resident memory |
| `SHR` | Shared resident pages |
| `S` | Process state |
| `CPU%` | CPU use from process and system counter deltas |
| `MEM%` | Resident memory as a percentage of physical memory |
| `TIME+` | Accumulated process CPU time |
| `COMMAND` | Command line, or process name when unavailable |

Aggregate CPU and memory meters appear above the table. Up to eight compact per-core CPU meters are shown in the header. Processes that exit or become inaccessible during a sample are skipped.

## Keyboard controls

| Key | Action |
|---|---|
| `↑` / `↓` or `j` / `k` | Move selection |
| `Page Up` / `Page Down` | Move selection by ten rows |
| `[` / `]` | Scroll the process table horizontally to reveal clipped columns |
| `Home` / `End` | Select first / last process |
| `s` or `F6` | Cycle sort field and direction (CPU, MEM, PID, USER, TIME, COMMAND) |
| `/` | Filter by PID, user, or command substring |
| `Esc` | Clear the filter |
| `t` or `F5` | Toggle parent/child tree view |
| `←` / `-` | Collapse the selected tree node |
| `→` / `+` | Expand the selected tree node |
| `Space` | Toggle the selected tree node |
| `K` | Choose TERM, KILL, INT, or HUP; confirm with `y` (default is no) |
| `r` | Set the selected process nice value (-20 through 19) |
| `F1` | Show the keybinding summary in the status line |
| `q` | Quit |

Process actions follow normal Linux permissions. Kaiamon refuses to signal or renice PID 1 or its own process. Signal selection is explicit and every signal requires a second, default-no confirmation. Use `KILL` only when graceful termination is not appropriate.

## Configuration

The background process sampling interval defaults to one second. Set it at launch with `--refresh` (seconds; minimum 0.05):

```bash
python3 kaiamon.py --refresh 0.5
```

Select and reorder visible columns with `--columns`. Names are comma-separated and case-insensitive. Accepted names are `PID`, `USER`, `PRI`, `NI`, `VIRT`, `RES`, `SHR`, `STATE` (or `S`), `CPU` (or `CPU%`), `MEM` (or `MEM%`), `TIME` (or `TIME+`), and `COMMAND`:

```bash
python3 kaiamon.py --columns PID,USER,CPU,MEM,COMMAND
python3 kaiamon.py --refresh 0.25 --columns PID,CPU,COMMAND
```

There is not yet a persistent configuration file or an interactive setup screen.

## Architecture

```text
/proc (system and per-PID pseudo-files)
              |
              v
       process_core.py
       ProcessCollector thread
       - procfs parsing and sampling
       - per-collector lock
       - CPU counter deltas
              |
              v
       frozen ProcessSnapshot
              |
       +------+----------------+
       |                       |
       v                       v
process_monitor.py       process_render.py       process_input.py       process_actions.py
main-thread lifecycle    snapshot renderer        keys + prompts          validated syscalls
state + delegation      curses only              main-thread curses     signal and renice
```

- `process_core.py` owns immutable process models, typed `/proc` parsers, CPU-delta calculations, and the polling collector. It has no curses dependency and is tested with synthetic procfs fixtures.
- `process_monitor.py` owns the main-thread curses lifecycle and UI state. It reads only frozen `ProcessSnapshot` values; collectors never call curses.
- `process_input.py` owns keyboard dispatch, tree expansion commands, and interactive prompts. It acts on the UI synchronously, so curses remains on the main thread.
- `process_render.py` owns snapshot-driven process-table and meter rendering. It receives a frozen snapshot and the UI's main-thread drawing surface; it does not access collector state.
- `process_actions.py` owns validation and the small set of process-control syscalls. It has no curses dependency, making the safety policy independently testable.

CPU percentages use per-PID CPU-time deltas divided by system CPU-time deltas and scaled by the number of online cores; they are not lifetime averages. Process start-time ticks guard against PID reuse. Common procfs races (process exit, permission errors, malformed or truncated pseudo-files) are skipped. The curses wrapper restores terminal mode on exit and exceptions; the collector is stopped and original SIGINT/SIGTERM handlers are restored in a `finally` block.

The design follows two useful htop lessons: keep platform-specific collection behind clean interfaces, and keep UI concerns separate from polling and process control. See the [htop “Going cross-platform” talk](https://htop.dev/assets/static/htop_talk_fosdem2016_hisham.pdf) and [htoprs](https://docs.rs/crate/htoprs/latest/source/README.md), a function-by-function port that treats htop's C implementation as its specification. Kaiamon remains Linux-only today; portability is deliberately deferred.

## Comparison with htop

**Supported:** live process table; PID/user/priority/nice/virtual/resident/shared/state/CPU/memory/time/command fields; CPU and memory meters; per-core CPU meters; CPU/MEM/PID/USER/TIME/COMMAND sorting; substring filtering; parent/child tree display and collapse; signal selection; renice; keyboard navigation.

**Different:** Kaiamon reads Linux `/proc` directly and is Linux-only. Its visible columns and sampling interval are configured at launch; filtering is substring-based; per-core meters are compact and limited to the first eight cores shown.

**Not yet implemented:** full htop setup/configuration screens, interactive column-width editing, process/thread toggles, CPU affinity, I/O and disk/network meters, GPU process metrics, configurable color schemes, and non-Linux backends. Kaiamon is htop-style, not a drop-in feature-equivalent replacement.

## Testing

Run the synthetic regression suite without root or dependence on the host process list:

```bash
python -m compileall -q .
python -m pytest -q
```

The tests cover procfs parsing, delta CPU calculations, process-disappearance races, immutable snapshot publication, input navigation/prompts, renderer boundaries, sorting/filtering/tree behavior, action validation, and Linux PTY terminal restoration after quit, Ctrl-C/SIGINT, SIGTERM, startup failure, resize, and UI/action-prompt exceptions.

Optional local static checks:

```bash
python -m ruff check kaiamon.py process_core.py process_actions.py process_render.py process_input.py process_monitor.py tests
python -m mypy process_core.py process_actions.py process_render.py process_input.py process_monitor.py
```

GitHub Actions runs compileall, pytest, Ruff, and Mypy across Python 3.10–3.13.

## Contributing

Keep procfs reads in `process_core.py`, process-control validation/syscalls in `process_actions.py`, and all curses operations on the main thread in `process_monitor.py`. Collectors own their mutable state behind per-collector locks and publish frozen snapshots. Add fixture-based tests for parsing, races, and invalid actions; do not rely on live host process data or privileged operations. Run the complete suite and static checks before opening a pull request.

## Roadmap

See [ROADMAP.md](ROADMAP.md) for completed milestones, current limitations, and next steps.

## License

Kaiamon is licensed under the [MIT License](LICENSE).
