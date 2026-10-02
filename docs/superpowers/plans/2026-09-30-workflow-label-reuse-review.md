# Workflow-scoped label reuse and human review implementation plan

> **For agentic workers:** Execute these tasks inline with focused red-green tests and a full offline suite. Preserve existing run state and photos.

**Goal:** One-click launch only requests labels for photos lacking a valid result in the current labeling workflow; human review can approve or invalidate a result without rewriting machine history.

**Architecture:** Extend the local photo-library SQLite database with immutable label results and append-only review events. Identify results by full image SHA-256, trusted species, and workflow profile; verify run state and result JSON before reuse. Keep run-local state authoritative for execution. Use a frozen input snapshot for each new run and resume existing tasks from their recorded TaskSpec. The launcher plans work after ingestion and does not create an empty run.

**Tech Stack:** Python 3, SQLite, PowerShell, pytest.

**Spec:** User-approved design in this conversation: machine completion and human approval are separate; rejected results are not reused.

## Global constraints

- Never commit photos, input batches, runs, .env, SQLite databases, or generated output.
- Never repeat an uncertain in-flight model call automatically.
- Never fall back to a public model endpoint.
- Existing run-local state and review decisions remain intact.

## Task 1: Registry and review lifecycle

**Files:** Create `src/agentized_workflow/label_registry.py`; create `tests/test_label_registry.py`.

- [ ] Write tests using real temporary SQLite and image/result files: valid done is reusable; wrong species/hash/profile is not; missing/corrupt result is not; approval is distinct from machine completion; rejection blocks reuse; later correction/approval produces a new effective result; historical records remain auditable.
- [ ] Run the focused tests and observe expected failures.
- [ ] Implement the assets-to-label_results and label_results-to-review_events relationships with full SHA-256 and append-only decisions. Validate state and JSON before publishing a reusable result.
- [ ] Run focused tests and inspect the schema/transaction behavior.

## Task 2: Incremental run and safe resume

**Files:** Modify `src/agentized_workflow/cli.py`, `src/agentized_workflow/storage.py`, `tools/start_agent.ps1`, `tools/resume_agent.ps1`; create `tests/test_incremental_labeling.py` and update launcher integration tests.

- [ ] Write tests: a second identical launch makes no model call and creates no empty run; a new photo is labeled once; rejected results are not reused; an interrupted request is not repeated; resume uses stored TaskSpecs even if the current CSV has changed.
- [ ] Run focused tests and observe expected failures.
- [ ] Add explicit plan/execute/review/import CLI operations. Freeze new-run input metadata and profile. Let resume read existing task specs; keep the historical task IDs and policy.
- [ ] Run focused tests and check no secret or photo payload appears in logs.

## Task 3: Historical results, output, and operations

**Files:** Modify `src/agentized_workflow/cli.py`, `src/agentized_workflow/label_registry.py`, `docs/RUNBOOK.md`; add focused import/review tests.

- [ ] Write tests: import only verified `done` results from a specified historical run; review status stays unreviewed until a human action; a rejected result cannot be restored by reimport; corrupt/missing outputs are reported, not cached.
- [ ] Run focused tests and observe expected failures.
- [ ] Add a read-only historical import command with explicit model/profile parameters, and operator commands for approve/reject/list. Document setup, one-click launch, resume, rendering, review, and status verification with exact PowerShell commands.
- [ ] Run focused tests and the complete offline suite; inspect the Git diff and tracked paths.
