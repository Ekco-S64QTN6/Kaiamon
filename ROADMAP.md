# Kaiamon Roadmap

Kaiamon is being refocused as a Linux htop-style process monitor. The legacy ping, systemd, thermal, and journal collectors are retained in `legacy_dashboard.py` but are not part of the primary process-monitor UI.

## Completed in this milestone

- [x] Replace the dashboard entry point with a process-centric curses interface.
- [x] Add a `/proc` collector for PID, parent PID, user, priority, nice, virtual/resident/shared memory, state, CPU%, MEM%, cumulative CPU time, and command line.
- [x] Calculate process CPU usage using per-PID and system CPU counter deltas; guard against PID reuse with process start-time ticks.
- [x] Publish frozen `ProcessSnapshot` and `ProcessInfo` objects behind a per-collector lock.
- [x] Add CPU and memory meters, with compact per-core CPU meters.
- [x] Add process table selection, paging, sorting, filtering, and parent/child tree display.
- [x] Add guarded signal selection and renice operations.
- [x] Add synthetic `/proc` fixtures and tests for parsing, sampling, sorting, filtering, tree handling, and action validation.
- [x] Rewrite README for the htop-style goal and document limitations and keybindings.
- [x] Require default-no confirmation before sending TERM, KILL, INT, or HUP; report cancellation and permission errors in the status line.
- [x] Add Linux PTY integration tests verifying terminal attributes are restored after `q`, SIGTERM, and an uncaught UI exception.
- [x] Add GitHub Actions CI for compileall, pytest, Ruff, and Mypy across Python 3.10–3.13; define the lint/type-check scope in `pyproject.toml`.
- [x] Add launch-time column selection and ordering with `--columns`, plus a validated process-sampling interval with `--refresh`.
- [x] Add keyboard-driven process-tree collapse and expansion with retained collapsed PID state across samples.
- [x] Expand Linux PTY coverage to Ctrl-C/SIGINT, curses startup failure, window resizing, and action-prompt exceptions.
- [x] Extract immutable process models, typed `/proc` parsers, CPU calculations, and the polling collector into curses-independent `process_core.py`.
- [x] Expand Ruff and Mypy targets to include `process_core.py`; preserve the four-version Python CI matrix.

## Next

1. Continue splitting curses rendering/input and process-action handling into focused modules, then extend their type-check coverage.
2. Consider optional secondary panels for legacy ping/service/thermal/log telemetry without reducing process-table space or polling responsiveness.

## Later / explicitly deferred

- Thread view and per-thread actions.
- CPU affinity and other scheduler controls.
- I/O, disk, network, and GPU process monitoring.
- Cross-platform backends and plugin architecture.
- Full htop configuration/setup parity.

## Acceptance criteria for calling this an htop replacement

The process table, sorting, filtering, tree navigation, CPU/memory meters, process actions, terminal recovery, and tests must be robust and documented. Feature parity should be claimed only after the deferred differences are either implemented or intentionally scoped out.
