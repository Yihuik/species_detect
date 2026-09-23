# Photo-labeling runbook

## Objective and scope

`terminal_scope_id`: `label-agent-20260922-142848`.

Finish automatic visibility, location, comparison, and labeling for the frozen
photo-library inputs represented by `runs/agent-20260922-142848/state.sqlite3`.
The scope is complete only when every task is in `done` or `needs_review`, the
worker has exited successfully, and the published result files agree with the
SQLite state. Collection and new remote downloads are outside this scope.

## Authorities and state

- Input manifest: `photos/workflow_metadata.csv`
- Authoritative task state: `runs/agent-20260922-142848/state.sqlite3`
- Run artifacts and bounded logs: `runs/agent-20260922-142848/`
- Generated bbox JSON: `runs/agent-20260922-142848/results/`
- Generated local photos with boxes and species labels: `runs/agent-20260922-142848/annotated/`
- Launcher configuration: project-root `.env`; credentials are never logged,
  committed, or copied into this document.

## Required invariants

1. Use exactly one labeling worker for this run.
2. `$DASHSCOPE_BASE_URL` is required. Do not fall back to a public endpoint.
3. Start recovery with `python` directly after loading the project `.env` and
   passing its base URL explicitly. Before allowing work to continue, inspect
   the child command line and confirm it contains the configured base URL.
4. Do not use `Start-Process powershell -File tools/resume_agent.ps1` until its
   child-environment propagation has been repaired and independently tested:
   prior invocations silently passed the public default endpoint.
5. If stopping a launcher, reconcile and stop its Python descendants before a
   new worker starts.
6. Preserve `done` tasks and legitimate `needs_review` records. If the known
   runtime failure `visibility_error:HTTPError` appears with no model attempts,
   stop the worker, make a timestamped SQLite backup, and reset only those
   rows to `visibility`.
7. Treat `.env`, photos, databases, run logs, and generated audit files as
   local artifacts. Code and documentation may be pushed; photos are never
   uploaded.

## Established failure signatures

| Signature | Cause and recovery |
| --- | --- |
| No `.env` found | Launchers initially resolved their own directory rather than the project root. Use the project-root configuration path. |
| `visibility_error:HTTPError` on many tasks | A recovery PowerShell process passed a public default URL. Terminate its process tree; restore only rows with this reason and no attempts; relaunch with an explicit configured URL. |
| Parent stopped but Python continued | PowerShell cancellation does not guarantee descendant termination. Inspect the process tree and terminate the owned Python child before recovery. |
| Long delay before new model calls | Older CLI builds reprocessed terminal tasks and rewrote result/audit output during resume. Commit `cd0b1fb` skips terminal tasks whose outputs exist. |
| No marked-up photo despite completed JSON | Run the local renderer below to backfill `annotated/` from `results/`; it does not request the model or alter task state. |

## Current recovery procedure

1. Inspect the Python process for this run and the task-phase counts.
2. If no valid worker is active, reconcile only the known recoverable HTTP
   failure rows as described above.
3. Load `.env`, verify both the API key and base URL are present without
   printing either, then launch Python with the explicit `--base-url` value.
4. Verify the launched child uses the configured host. Record the PID and start
   time in the session update.
5. Perform only the user-requested delayed static check. For normal operation,
   do not live-poll the process.
6. After new JSON results appear, backfill marked-up photos with:

   ```powershell
   $env:PYTHONPATH = 'D:\codex\new-agent\src'
   python -m agentized_workflow.render_labels `
     --photos-root 'D:\codex\new-agent\photos' `
     --run-dir 'D:\codex\new-agent\runs\agent-20260922-142848'
   ```

   Rerunning this command skips photos already rendered from current JSON.

## Legal terminal evidence

The run is complete only when phase counts contain no runnable phase, the
worker has ended, the results/audit outputs have timestamps at or after the
last task-state update, and the number of `annotated/*.jpg` files equals the
number of `done` tasks. `needs_review` is a scoped terminal for individual
images and must retain its recorded reason.
