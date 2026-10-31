# Changelog

## Unreleased — Safe per-thread renicing

- Add selectable thread rows in F4 and allow `r` to request a nice value for the selected TID.
- Require default-no confirmation and verify both parent PID and thread TID start-time ticks against procfs immediately before `setpriority`.
- Bind active thread sampling to both the target PID and its original start time so PID reuse cannot silently change the inspected process.
- Keep thread signalling deferred; add tests for action validation, parent/TID reuse, F4 selection, confirmed/declined renice, and inspector lifecycle.


## Unreleased — Safe process CPU-affinity editor

- Add F8 editor for a selected process's allowed CPU mask, with scrolling for large CPU sets and Space to toggle CPUs.
- Require a default-no confirmation before applying; never allow an empty mask, and report the effective mask returned by the kernel/cpuset.
- Reuse process-action PID validation to protect PID 1 and the monitor itself, and compare process start-time ticks immediately before applying to guard PID reuse.
- Add CPU-affinity operation and editor tests for valid/invalid masks, self/system PID rejection, cancellation, stale PID detection, and UI routing.


## Unreleased — Selected-process thread inspector

- Add F4 view for per-thread TID, CPU percentage, cumulative CPU time, state, priority, nice value, and thread name.
- Collect `/proc/<pid>/task/<tid>/stat` only for the selected process while the inspector is open, avoiding a full-system thread scan on each poll.
- Calculate per-thread CPU deltas against system CPU counters and guard TID reuse with thread start-time ticks.
- Keep the process row visible if its task directory or individual thread entries become inaccessible; support scrolling and F4/Esc return.
- Add synthetic tests for targeted collection, CPU deltas, TID reuse, inaccessible task directories, keyboard lifecycle, and panel rendering.


## Unreleased — System I/O overview panel

- Add F3 panel for per-device disk throughput from `/proc/diskstats` and per-interface network throughput from `/proc/net/dev`.
- Calculate rates from monotonic counter deltas, clamp counter resets, and keep missing system telemetry from interrupting process collection.
- Display each device/interface independently instead of combining partitions, virtual devices, or network layers into potentially double-counted totals.
- Add synthetic parser, collector, reset, unavailable-source, panel-rendering, and keyboard-navigation tests.


## Unreleased — Per-process storage I/O rates

- Parse Linux `/proc/<pid>/io` read/write byte counters and calculate per-process rates from monotonic sample intervals.
- Guard deltas against PID reuse and counter resets; inaccessible or malformed I/O data leaves the process visible with unavailable rates.
- Add optional `IOR/s` and `IOW/s` columns, I/O sorting, setup-screen column selection, and 8-character default widths.
- Add synthetic parser, collector, PID-reuse, unavailable-file, sorter, setup, and renderer tests.

## Unreleased — Editable and persistent column widths

- Extend the F2 setup screen with per-column width controls using `-` and `+`, bounded to 1–120 characters.
- Render configured widths immediately and save them with the existing XDG preferences.
- Extend version-1 config validation while keeping older files valid with default widths.
- Add tests for width normalization, invalid values, legacy-config defaults, editor apply/cancel/bounds, runtime loading, and renderer layout.

## Unreleased — Interactive column setup

- Add an F2 setup screen to show or hide process columns and reorder their display without restarting.
- Apply changes immediately, reset table selection and horizontal scroll, and retain them through the existing persistent configuration save.
- Keep Escape non-destructive and prevent the user from hiding every column.
- Add synthetic tests for toggling, enabling hidden columns, reordering, cancel behavior, and the non-empty-column invariant.

## Unreleased — Persistent user configuration

- Load versioned JSON preferences from the XDG configuration directory, with a `~/.config` fallback.
- Persist refresh interval, column order, sort field/direction, and tree mode after a clean exit.
- Add command-line overrides, custom config paths, and a no-save mode.
- Write preferences through an atomic replacement with private file permissions; reject invalid configuration instead of silently resetting it.
- Add validation, round-trip, permission, CLI precedence, and persistence regression tests.

## Unreleased — Preserve process ancestry in tree filters

- Keep each matching process's available parent chain visible while tree mode is filtered.
- Exclude unrelated siblings and descendants so filtered results remain focused.
- Guard traversal against malformed parent cycles and cover both UI behavior and pure filtering logic with synthetic tests.

## Unreleased — Horizontal process-table scrolling

- Preserve stable fixed widths for every configured column and allow the process table to scroll horizontally with `[` and `]`.
- Clamp the horizontal offset when the terminal is resized or the selected column layout fits the viewport.
- Add regression tests for default-column clipping, scroll bounds, and reordered columns.

## Unreleased — Expand renderer and input regression coverage

- Add focused renderer tests for minimum terminal handling and normal snapshot/footer drawing.
- Exercise input navigation, sorting, filtering, prompt cleanup on read errors, and invalid signal choices.
- Keep these tests synthetic and independent of live procfs process state.

## Unreleased — Extract keyboard input and prompts

- Move key dispatch, tree expansion controls, and interactive prompts into `process_input.py`.
- Keep input handling synchronous and on the curses main thread; the UI delegates to a controller over existing UI state.
- Add delegation and controller-boundary regression tests and include the module in Ruff/Mypy CI.

## Unreleased — Extract process-table renderer

- Move snapshot-driven process table, meter, header, and row rendering into `process_render.py`.
- Keep all curses operations on the main thread; the UI delegates each frozen snapshot to the renderer.
- Add a regression test for the UI-to-renderer boundary and include the module in Ruff/Mypy CI.

## Unreleased — Isolate process actions from the curses UI

- Move PID/nice/signal validation and process-control syscalls into curses-independent `process_actions.py`.
- Add regression coverage proving rejected targets, unsupported signals, and invalid nice values never reach operating-system calls.
- Extend the configured Ruff and Mypy scopes to cover the new module.

## Unreleased — Separate process core module

- Extract immutable process models, typed procfs parsing, CPU accounting, and the `/proc` polling collector into curses-independent `process_core.py`.
- Extend Ruff and Mypy coverage to both active process-monitor modules while preserving Python 3.10–3.13 CI.

## Unreleased — Expanded terminal recovery coverage

- Add PTY integration tests for Ctrl-C/SIGINT, curses startup failure, terminal resizing, and exceptions during process-action prompts.

## Unreleased — Configurable process table and tree navigation

- Add `--columns` to select and reorder visible process-table columns at launch.
- Add a validated `--refresh` option for the background `/proc` sampling interval.
- Add Left/Right and Space controls to collapse, expand, and toggle process-tree nodes.

## Previous unreleased milestone — Process-monitor safety and CI

- Require a default-no confirmation before sending TERM, KILL, INT, or HUP to the selected process.
- Add Linux PTY integration tests that verify curses restores the original terminal attributes after normal quit, SIGTERM, and an uncaught UI exception.
- Add GitHub Actions CI for compileall, pytest, Ruff, and Mypy across Python 3.10–3.13.
- Configure Ruff and Mypy scopes for the active process-monitor application.

## Previous milestone — Process-monitor core

- Replaced the primary dashboard entry point with an htop-style Linux process monitor.
- Added a lock-protected `/proc` collector with immutable snapshots and delta-based per-process CPU percentages.
- Added process CPU/memory meters, per-core CPU meters, a sortable/filterable process table, tree view, signal selection, and renice.
- Preserved the former ping/service/thermal/log dashboard in `legacy_dashboard.py`; it is not launched by the new default entry point.
- Rewrote README and added a roadmap that distinguishes implemented behavior from remaining htop gaps.
- Added synthetic `/proc` regression tests.
