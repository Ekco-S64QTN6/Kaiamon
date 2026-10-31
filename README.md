# Kaiamon

Kaiamon is a Linux terminal process monitor inspired by **htop**. It presents a live, sortable and filterable process table in a curses interface, with CPU and memory meters, parent/child navigation, and guarded process controls.

The default application is process-centric. The former ping, systemd, thermal, and journal dashboard remains in `legacy_dashboard.py` for reference; it is not started by the process-monitor entry point.

## Requirements

- Linux with a mounted `/proc` filesystem
- Python 3.10 or newer
- A terminal with curses support (the standard-library `curses` module)

The process monitor uses only the Python standard library. Listing processes does not require root, although Linux permissions may limit visibility of details and process actions. Per-process I/O rates are shown when `/proc/<pid>/io` is readable; unavailable counters display as `—` without hiding the process.

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
| `IOR/s` | Per-process storage read rate from `/proc/<pid>/io` |
| `IOW/s` | Per-process storage write rate from `/proc/<pid>/io` |

Aggregate CPU and memory meters appear above the table. Up to eight compact per-core CPU meters are shown in the header. Processes that exit or become inaccessible during a sample are skipped. Press F3 for the system I/O panel, which shows read/write throughput for each block device from `/proc/diskstats` and receive/transmit throughput for each interface from `/proc/net/dev`. Rates are shown per device/interface rather than summed, avoiding misleading totals when virtual devices, partitions, bridges, or layered block devices overlap.

## Keyboard controls

| Key | Action |
|---|---|
| `↑` / `↓` or `j` / `k` | Move selection |
| `Page Up` / `Page Down` | Move selection by ten rows |
| `[` / `]` | Scroll the process table horizontally to reveal clipped columns |
| `Home` / `End` | Select first / last process |
| `s` or `F6` | Cycle sort field and direction (CPU, MEM, PID, USER, TIME, COMMAND, I/O read, I/O write) |
| `/` | Filter by PID, user, or command substring |
| `Esc` | Clear the filter |
| `t` or `F5` | Toggle parent/child tree view |
| `←` / `-` | Collapse the selected tree node |
| `→` / `+` | Expand the selected tree node |
| `Space` | Toggle the selected tree node |
| `K` | Choose TERM, KILL, INT, or HUP; confirm with `y` (default is no) |
| `r` | Set the selected process nice value (-20 through 19) |
| `F1` | Show the keybinding summary in the status line |
| `F2` | Open column setup; Space toggles visibility, Left/Right reorders, `-` / `+` resize the selected column, Enter applies, and Esc cancels |
| `F3` | Open the system I/O panel; view per-device disk and per-interface network rates, use Up/Down or Page Up/Down to scroll, and F3/Esc to return |
| `F4` | Inspect threads for the selected process; Up/Down or Page Up/Down selects a thread, `r` prompts for its nice value and requires `y` confirmation, and F4/Esc returns |
| `F8` | Edit the selected process CPU affinity; Space toggles CPUs, Enter asks for confirmation, and Esc cancels. PID 1 and the monitor itself are protected |
| `q` | Quit |

Process actions follow normal Linux permissions. Kaiamon refuses to signal, renice, or change CPU affinity for PID 1 or its own process. Per-thread renice in the F4 inspector also requires a second, default-no confirmation and rechecks both the owning process and thread start-time identities immediately before the syscall, preventing a stale TID from targeting a replacement thread. Signal selection is explicit and every signal requires a second, default-no confirmation. Use `KILL` only when graceful termination is not appropriate.

## Configuration

The sampling interval defaults to one second. Kaiamon loads preferences from a JSON configuration file at `$XDG_CONFIG_HOME/kaiamon/config.json`, or `~/.config/kaiamon/config.json` when `XDG_CONFIG_HOME` is unset or relative. The file is created automatically after a clean exit and stores the sampling interval, visible columns and their widths, sort field/direction, and tree/flat view.

Command-line options override saved preferences for that run. The resulting settings are written back on clean exit:

```bash
python3 kaiamon.py --refresh 0.5
python3 kaiamon.py --columns PID,USER,CPU,MEM,COMMAND
python3 kaiamon.py --sort MEM --sort-order asc --tree
python3 kaiamon.py --config ./kaiamon.json --flat
```

Use `--no-save-config` to load preferences without writing changes. Use `--config PATH` to select another file. Invalid or unsupported configuration values produce a clear startup error rather than being silently overwritten.

The version 1 configuration format is:

```json
{
  "version": 1,
  "refresh_interval": 1.0,
  "columns": ["PID", "USER", "PRI", "NI", "VIRT", "RES", "SHR", "STATE", "CPU", "MEM", "TIME", "COMMAND"],
  "column_widths": {"PID": 6, "USER": 8, "PRI": 3, "NI": 3, "VIRT": 7, "RES": 7, "SHR": 6, "STATE": 1, "CPU": 6, "MEM": 5, "TIME": 8, "COMMAND": 24, "IO_READ": 8, "IO_WRITE": 8},
  "sort_field": "CPU",
  "descending": true,
  "tree_mode": false
}
```

Accepted sort fields are `CPU`, `MEM`, `PID`, `USER`, `TIME`, `COMMAND`, `IO_READ`, and `IO_WRITE` (also displayed as `IOR/s` and `IOW/s`). Column names and aliases are the same as the `--columns` option. `column_widths` is a per-column character width map; omitted entries use defaults, and accepted widths range from 1 to 120. Existing version-1 files without this key remain valid and receive default widths. Config writes use an atomic replacement and a private 0600 file, so an interrupted save does not leave a partially-written preferences file.

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

- `process_core.py` owns immutable process and thread models, typed `/proc` parsers, CPU and I/O delta calculations, and the polling collector. Thread counters are sampled only for the process requested by the F4 inspector, limiting overhead. It has no curses dependency and is tested with synthetic procfs fixtures.
- `process_config.py` owns versioned preference validation, XDG config-path resolution, and atomic JSON persistence. It does not import curses or depend on the UI.
- `process_monitor.py` owns the main-thread curses lifecycle and UI state. It reads only frozen `ProcessSnapshot` values; collectors never call curses.
- `process_input.py` owns keyboard dispatch, tree expansion commands, thread selection, and interactive prompts. It acts on the UI synchronously, so curses remains on the main thread.
- `process_setup.py` owns the F2 column editor. It stages visibility, order, and per-column widths on the curses main thread; Enter applies them and Escape discards them.
- `process_render.py` owns snapshot-driven process-table and meter rendering. It receives a frozen snapshot and the UI's main-thread drawing surface; it does not access collector state.
- `process_actions.py` owns validation and process-control syscalls, including CPU-affinity read/write operations and start-time-verified per-thread renicing. It has no curses dependency, making the safety policy independently testable.
- `process_affinity.py` owns the main-thread F8 CPU-mask editor. It stages changes, blocks an empty mask, verifies the selected PID/start-time before applying, and requires default-no confirmation.

CPU percentages use per-PID CPU-time deltas divided by system CPU-time deltas and scaled by the number of online cores; they are not lifetime averages. Process start-time ticks guard against PID reuse. Common procfs races (process exit, permission errors, malformed or truncated pseudo-files) are skipped. The curses wrapper restores terminal mode on exit and exceptions; the collector is stopped and original SIGINT/SIGTERM handlers are restored in a `finally` block.

The design follows two useful htop lessons: keep platform-specific collection behind clean interfaces, and keep UI concerns separate from polling and process control. See the [htop “Going cross-platform” talk](https://htop.dev/assets/static/htop_talk_fosdem2016_hisham.pdf) and [htoprs](https://docs.rs/crate/htoprs/latest/source/README.md), a function-by-function port that treats htop's C implementation as its specification. Kaiamon remains Linux-only today; portability is deliberately deferred.

## Comparison with htop

**Supported:** live process table; PID/user/priority/nice/virtual/resident/shared/state/CPU/memory/time/command fields plus per-process I/O rates; on-demand per-process thread inspection with per-thread CPU percentages, time, and individual-thread renicing; process CPU-affinity inspection and editing; configurable visible columns, ordering, and widths; CPU and memory meters; per-core CPU meters; CPU/MEM/PID/USER/TIME/COMMAND/I/O sorting; substring filtering; parent/child tree display and collapse; signal selection; renice; keyboard navigation.

**Different:** Kaiamon reads Linux `/proc` directly and is Linux-only. Its sampling interval is configured at launch; visible columns can be changed at runtime with F2 or at launch. Filtering is substring-based (tree filtering retains available ancestors); per-core meters are compact and limited to the first eight cores shown.

**Not yet implemented:** full htop setup/configuration parity, per-thread signalling and other per-thread controls, GPU process metrics, configurable color schemes, and non-Linux backends. Kaiamon is htop-style, not a drop-in feature-equivalent replacement.

## Testing

Run the synthetic regression suite without root or dependence on the host process list:

```bash
python -m compileall -q .
python -m pytest -q
```

The tests cover procfs parsing, delta CPU calculations, process-disappearance races, immutable snapshot publication, atomic configuration persistence and CLI precedence, column-width validation and setup interactions, input navigation/prompts, renderer boundaries, sorting/filtering/tree behavior, selected-process thread sampling and rendering, CPU-affinity validation/editor behavior, per-thread renice validation and UI behavior, action validation, and Linux PTY terminal restoration after quit, Ctrl-C/SIGINT, SIGTERM, startup failure, resize, and UI/action-prompt exceptions.

Optional local static checks:

```bash
python -m ruff check kaiamon.py process_core.py process_actions.py process_affinity.py process_config.py process_setup.py process_render.py process_input.py process_monitor.py tests
python -m mypy process_core.py process_actions.py process_affinity.py process_config.py process_setup.py process_render.py process_input.py process_monitor.py
```

GitHub Actions runs compileall, pytest, Ruff, and Mypy across Python 3.10–3.13.

## Contributing

Keep procfs reads in `process_core.py`, process-control validation/syscalls in `process_actions.py`, and all curses operations on the main thread in `process_monitor.py`. Collectors own their mutable state behind per-collector locks and publish frozen snapshots. Add fixture-based tests for parsing, races, invalid actions, and configuration round trips; do not rely on live host process data or privileged operations. Run the complete suite and static checks before opening a pull request.

## Roadmap

See [ROADMAP.md](ROADMAP.md) for completed milestones, current limitations, and next steps.

## License

Kaiamon is licensed under the [MIT License](LICENSE).
