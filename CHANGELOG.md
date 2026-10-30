# Changelog\n\n## Unreleased — Extract keyboard input and prompts\n\n- Move key dispatch, tree expansion controls, and interactive prompts into `process_input.py`.\n- Keep input handling synchronous and on the curses main thread; the UI delegates to a controller over existing UI state.\n- Add delegation and controller-boundary regression tests and include the module in Ruff/Mypy CI.\n\n Unreleased — Extract process-table renderer

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
