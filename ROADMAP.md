# Kaiamon Roadmap

Kaiamon is a Linux htop-style process monitor. The legacy ping, systemd, thermal, and journal collectors remain in `legacy_dashboard.py` for reference; they are not part of the primary process-monitor UI.

## Completed

- [x] Replace the dashboard entry point with a process-centric curses interface.
- [x] Add a `/proc` collector for PID, parent PID, user, priority, nice, virtual/resident/shared memory, state, CPU%, MEM%, cumulative CPU time, and command line.
- [x] Calculate process CPU usage using per-PID and system CPU counter deltas; guard against PID reuse with process start-time ticks.
- [x] Publish frozen `ProcessSnapshot` and `ProcessInfo` objects behind a per-collector lock.
- [x] Add CPU and memory meters, with compact per-core CPU meters.
- [x] Add process table selection, paging, sorting, filtering, and parent/child tree display with collapse/expand controls.
- [x] Add guarded signal selection and renice operations, including default-no signal confirmation and validation against PID 1 and the monitor itself.
- [x] Add synthetic `/proc` fixtures and tests for parsing, sampling, sorting, filtering, tree handling, and action validation.
- [x] Add Linux PTY integration tests for terminal restoration after normal quit, Ctrl-C/SIGINT, SIGTERM, curses startup failure, window resize, and UI/action-prompt exceptions.
- [x] Add GitHub Actions CI for compileall, pytest, Ruff, and Mypy across Python 3.10–3.13.
- [x] Add launch-time column selection and ordering with `--columns`, plus a validated process-sampling interval with `--refresh`.
- [x] Extract immutable process models, typed `/proc` parsers, CPU calculations, and the polling collector into curses-independent `process_core.py`.
- [x] Extract process-action validation and OS calls into curses-independent `process_actions.py`.
- [x] Extract keyboard dispatch, tree-expansion commands, and prompts into `process_input.py` while keeping all curses operations on the main thread.

## Next

1. [x] Extract snapshot-driven curses rendering into `process_render.py`; keep curses calls on the main thread and pass only frozen snapshots.
2. [x] Split keyboard input and prompts into a focused module without moving curses operations off the main thread.
3. [x] Expand renderer/input tests and static type coverage as responsibilities are separated.
4. Consider optional secondary panels for legacy ping/service/thermal/log telemetry only if they preserve process-table space and do not slow process sampling or rendering.

## Later / explicitly deferred

- Thread view and per-thread actions.
- CPU affinity and other scheduler controls.
- I/O, disk, network, and GPU process monitoring.
- Cross-platform backends and plugin architecture.
- Full htop configuration/setup parity.

## Acceptance criteria for calling this an htop replacement

The process table, sorting, filtering, tree navigation, CPU/memory meters, process actions, terminal recovery, and tests must be robust and documented. Feature parity should be claimed only after the deferred differences are either implemented or intentionally scoped out.
