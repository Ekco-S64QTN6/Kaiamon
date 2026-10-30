# Kaiamon Roadmap

Kaiamon is being refocused as a Linux htop-style process monitor. The legacy ping, systemd, thermal, and journal collectors are retained in `legacy_dashboard.py` but are not part of the primary process-monitor UI.

## Completed in this milestone

- [x] Replace the dashboard entry point with a process-centric curses interface.
- [x] Add a `/proc` collector for PID, parent PID, user, priority, nice, virtual/resident/shared memory, state, CPU%, MEM%, cumulative CPU time, and command line.
- [x] Calculate process CPU usage using per-PID and system CPU counter deltas; guard against PID reuse with process start-time ticks.
- [x] Publish frozen `ProcessSnapshot` and `ProcessInfo` objects behind a per-collector lock.
- [x] Add CPU and memory meters, with compact per-core CPU percentages.
- [x] Add process table selection, paging, sorting, filtering, and parent/child tree display.
- [x] Add guarded signal selection and renice operations.
- [x] Add synthetic `/proc` fixtures and tests for parsing, sampling, sorting, filtering, tree handling, and action validation.
- [x] Rewrite README for the htop-style goal and document limitations and keybindings.

## Next

1. Add interactive confirmation for destructive signals and a safer action dialog with visible permission/error feedback.
2. Add configurable columns, configurable refresh rate, and tree collapse/expand.
3. Improve full-screen terminal restoration and signal-shutdown regression coverage with PTY-based tests.
4. Add CI for compile, pytest, ruff, and mypy; establish supported Python versions and typing policy.
5. Consider optional secondary panels for legacy ping/service/thermal/log telemetry without reducing process-table space or polling responsiveness.

## Later / explicitly deferred

- Thread view and per-thread actions.
- CPU affinity and other scheduler controls.
- I/O, disk, network, and GPU process monitoring.
- Cross-platform backends and plugin architecture.
- Full htop configuration/setup parity.

## Acceptance criteria for calling this an htop replacement

The process table, sorting, filtering, tree navigation, CPU/memory meters, process actions, terminal recovery, and tests must be robust and documented. Feature parity should be claimed only after the deferred differences are either implemented or intentionally scoped out.
