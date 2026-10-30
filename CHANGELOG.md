# Changelog

## Unreleased — Process-monitor core

- Replaced the primary dashboard entry point with an htop-style Linux process monitor.
- Added a lock-protected \`/proc\` collector with immutable snapshots and delta-based per-process CPU percentages.
- Added process CPU/memory meters, per-core CPU usage, a sortable/filterable process table, tree view, signal selection, and renice.
- Preserved the former ping/service/thermal/log dashboard in \`legacy_dashboard.py\`; it is not launched by the new default entry point.
- Rewrote README and added a roadmap that distinguishes implemented behavior from remaining htop gaps.
- Added synthetic \`/proc\` regression tests.
