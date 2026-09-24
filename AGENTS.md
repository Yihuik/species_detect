# Project engineering instructions

This repository is the authoritative workspace for the photo library and
species-labeling agent. `D:\codex\qwen-vl` is a reference project; do not write
there as part of this workflow unless the user explicitly requests it.

Read `docs/ENGINEERING.md` before development and `docs/RUNBOOK.md` before
operating or recovering the long-running labeling job. `state.sqlite3` is the
authoritative run state; chat messages and exported JSON are snapshots.

- Reproduce a bug and identify its cause before editing code. Write a focused
  failing test for a feature or fix, then implement the smallest change and run
  the focused and full offline suites.
- Run tests with `python -m pytest -q --basetemp .pytest_tmp`. This directory is
  dedicated to pytest and may be cleared on each invocation; never put real
  photos or state there.
- Keep model calls explicit. Load the project `.env` without printing secrets,
  require `DASHSCOPE_BASE_URL`, and never fall back to a public endpoint.
- Never add `photos/`, `input/inbox/`, `runs/`, `.env`, SQLite files, or generated
  outputs to Git. Verify staged paths before committing or pushing.
- Before restarting a run, reconcile its SQLite phase counts, process identity,
  and output files. Keep one worker; preserve completed work and legitimate
  review decisions. Do not repeat an uncertain model request automatically.
- A code change is not a run completion. Claim completion only after the worker
  exits, every task is terminal, result JSON and annotated photo counts match
  `done`, and review cases remain accounted for.
