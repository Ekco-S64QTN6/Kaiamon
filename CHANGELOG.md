# Changelog

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
