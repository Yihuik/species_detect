# Engineering and operations practice

This document is the durable working agreement for future changes to this
repository. The current application is a Python photo-acquisition and labeling
pipeline with SQLite state and a model-provider integration. There is no
frontend application in this repository yet; a future UI should be designed
against explicit data contracts and tested at its actual boundary.

## Development loop

1. State the observable behavior and the affected files. For a bug, reproduce
   the failure, read the complete error, and trace it to the owning component.
2. Add one focused offline test that fails for the expected reason. Use real
   image files and SQLite state where practical; fake only the external model
   or web service boundary.
3. Make the smallest code change. Run the focused test, then
   `python -m pytest -q --basetemp .pytest_tmp`. Pytest's `--basetemp` is
   cleared before a run, so `.pytest_tmp` is reserved exclusively for tests.
4. Inspect the diff and staged paths. Commit source, tests, and documentation;
   keep photos, credentials, run databases, logs, and rendered outputs local.
5. Report the changed behavior, observed test results, and remaining limits.
   Passing tests do not establish bbox quality: evaluate a representative
   sample of marked-up photos and keep uncertain cases in `needs_review`.

The source for the temporary-directory rule is the
[pytest temporary-path guide](https://docs.pytest.org/en/stable/how-to/tmp_path.html).
An eventual CI job should run the same offline suite on supported Python and
Windows versions, following [GitHub's Python CI guide](https://docs.github.com/actions/automating-builds-and-tests/building-and-testing-python).

## Model and data boundaries

- Only configured `photos/` images and trusted species metadata enter the
  labeling workflow. Sending photo bytes to the project `.env` MaaS endpoint
  was authorized by the user; pushing them to GitHub was not.
- A missing or wrong endpoint is a configuration failure, not an invitation to
  use a default public endpoint. Never print the API key, Authorization header,
  image payload, or raw provider response in logs or chat.
- Classify provider failures by HTTP status and source before recovery. For
  example, Alibaba documents rate limiting as 429 and also distinguishes
  authentication, model, quota, and request errors in its
  [error-code guide](https://help.aliyun.com/zh/model-studio/error-code) and
  [rate-limit guide](https://help.aliyun.com/zh/model-studio/rate-limit).
  Do not treat a transport failure as proof that an image needs human labeling
  review. The current engine still converts some visibility exceptions into
  `needs_review`; that is a known repair target, not a valid final explanation
  for a large batch of photos.

## Long-run operations

- Follow `docs/RUNBOOK.md` for the active run and use `state.sqlite3` for counts.
  Confirm the worker by command line and creation time, not by PID alone.
- Use one worker per run. When a launcher or terminal ends, inspect the child
  process before resuming; an exit of the parent is not proof that the child
  stopped. Preserve completed tasks and narrow any recovery to the affected
  rows after recording a database backup.
- Back up a live SQLite database through its online backup API rather than a
  bare file copy; see [SQLite's backup guidance](https://www.sqlite.org/backup.html).
  SQLite's [WAL guidance](https://www.sqlite.org/wal.html) explains why a
  database file and its WAL cannot be treated as independent state.
- Keep progress checks sparse when requested. A healthy run can continue
  silently; alert on exit, concentrated errors, no validated progress beyond
  the expected stage time, or a request for user action. Generate new
  `annotated/` photos from completed JSON using the command in the runbook.
- Completion requires all tasks in `done` or legitimate `needs_review`, no
  active worker, a consistent JSON/annotated count for `done`, and a final
  reason summary for reviews. Document what still needs manual review.

For long Windows runs, use the documented PowerShell
[`Start-Process` options](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.management/start-process?view=powershell-7.5)
with a hidden Python process and redirected output. A retained foreground
terminal session disappeared during the 2026-09-24 run; its lifetime is not a
reliable ownership mechanism here. Python's
[logging guide](https://docs.python.org/3/howto/logging.html) is the reference
for structured application logs; redact secrets before emitting them.
