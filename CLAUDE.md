# CLAUDE.md — working notes for agents

`README.md` describes the tool, `ROADMAP.md` what is next, `CHANGELOG.md` what changed.

## Checks (what CI runs)

```bash
python -m compileall -q .
python -m pytest -q
ruff check kaiamon.py process_*.py tests
mypy process_core.py process_actions.py process_affinity.py process_config.py \
     process_setup.py process_render.py process_input.py process_monitor.py
```

## Layout

`kaiamon.py` launches the htop-style process monitor (`process_monitor.main`).
The original system-companion dashboard (ping, systemd services, thermals,
journal) lives in `legacy_dashboard.py` and still runs on its own:
`python3 legacy_dashboard.py`. It is kept on purpose; do not delete it.

`kaiamon_net.py` may exist untracked in the owner's checkout. It is the owner's
work in progress, standalone (imports nothing from this package). Leave it out
of commits unless asked.

## Rules that must not be reverted (2026-10-30)

- **Every action on a process carries its identity, not just its PID.**
  `signal_process()` and `renice_process()` take the snapshot's
  `start_ticks`; the UI (`process_input.py`) always passes them. Signals are
  sent through a pidfd opened *before* the start-ticks check, so the checked
  process is the one signalled. Before this, `K` → `2` → `y` sent SIGKILL to
  whatever held the PID after two prompts with no time limit.
  `tests/test_process_identity_actions.py` covers it, and it was checked live
  against a real process (wrong ticks refused, right ticks delivered).
  `renice_thread()` already worked this way.
- PID 1 and the monitor's own PID are refused (`validate_process_target`).
