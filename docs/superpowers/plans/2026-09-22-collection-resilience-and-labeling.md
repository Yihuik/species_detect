# Collection Resilience and Labeling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ensure a partial public-photo collection records failures safely, continues over invalid candidates, refreshes the photo metadata, and proceeds to automatic labeling without duplicating already-seen remote resources.

**Architecture:** The acquisition ledger remains the durable source of remote identity, download claims, policy decisions, and pagination cursors. The collector distinguishes candidate-local validation rejection from a source outage, records each transition atomically, and only keeps a species pending when a source has not finished. The launcher invokes collection in partial-success mode so valid photos become label tasks while incomplete source queries remain resumable.

**Tech Stack:** Python 3.14, SQLite, requests, Pillow, pytest, PowerShell.

**Spec:** `docs/superpowers/plans/2026-09-20-public-photo-crawl-deduplication.md`

## Global Constraints

- Never commit or upload `photos/`, `input/`, `.env`, staging files, logs, or API credentials.
- Preserve the existing SQLite database and migrate schema forward only.
- A known remote version must be skipped before any image response body is requested.
- Candidate validation failures must be auditable but must not terminate its source traversal.
- A nonzero source result keeps that species pending; partial collection must still refresh `photos/workflow_metadata.csv`.
- Default collection remains strict; only the launcher explicitly elects partial-success continuation.
- Pagination cursor data must be scoped by source, Chinese species name, and stable query fingerprint.

---

### Task 1: Durable failed-transfer and query-checkpoint transitions

**Files:**
- Modify: `src/agentized_workflow/acquisition_ledger.py`
- Modify: `tests/test_acquisition_ledger.py`

**Interfaces:**
- Produces `record_download_failure(remote_version_id: int, claim_token: str, outcome: str) -> None`.
- Produces methods to load, checkpoint, complete, and fail a query scope.

- [ ] Write failing tests for claim release after a failed transfer and for loading, checkpointing, and completing a GBIF cursor.
- [ ] Run `python -m pytest -q tests/test_acquisition_ledger.py` and confirm the API is absent.
- [ ] Implement the smallest ledger transitions: failure deletes only the owning claim; cursors use canonical JSON, completion removes the resumable cursor, and a failed scope retains its cursor and error.
- [ ] Re-run focused tests and commit the ledger task.

### Task 2: Candidate-local rejection, source retry, and resumable cursors

**Files:**
- Modify: `tools/collect_species_images.py`
- Modify: `src/agentized_workflow/collector_flow.py`
- Create: `tests/test_collect_species_images.py`
- Modify: `tests/test_collector_flow.py`

**Interfaces:**
- Produces `CandidateRejected` for unsupported content, invalid MIME, and too-small dimensions.
- Source fetchers resume iNaturalist pages, GBIF offsets, and Commons continuation tokens from ledger scopes.
- Consumes the ledger failed-transfer and scope APIs.

- [ ] Write failing tests proving an invalid first candidate returns `rejected_content` and a valid second candidate is accepted, then proving a saved GBIF offset is used first.
- [ ] Run `python -m pytest -q tests/test_collect_species_images.py tests/test_collector_flow.py` and confirm current behavior aborts the source.
- [ ] Implement `CandidateRejected`; release the transfer claim, record `rejected_content` for the current policy, and continue to the next candidate.
- [ ] Make 429 and 5xx retries explicit and bounded; after exhaustion record one source failure while retaining its cursor.
- [ ] Checkpoint each completed API page with a stable fingerprint that excludes pagination values; complete the scope only after an exhausted response.
- [ ] Re-run focused tests and commit the collector task.

### Task 3: Partial collection handoff to automatic labeling

**Files:**
- Modify: `src/agentized_workflow/collection_runner.py`
- Modify: `src/agentized_workflow/cli.py`
- Modify: `tools/start_agent.ps1`
- Modify: `tests/test_collection_runner.py`
- Modify: `tests/test_photo_cli.py`

**Interfaces:**
- Extends `collect_pending(..., allow_partial: bool = False) -> int`.
- Adds `photos collect --allow-partial`.
- Launcher invokes `photos collect --allow-partial` and sets UTF-8 process output.

- [ ] Write failing tests showing partial collection refreshes metadata while retaining the pending state, and strict mode returns the subprocess failure.
- [ ] Run `python -m pytest -q tests/test_collection_runner.py tests/test_photo_cli.py` and confirm strict behavior currently suppresses metadata refresh.
- [ ] Always regenerate `workflow_metadata.csv` after the collector exits. Mark collection complete only on return code zero; partial mode returns zero for the launcher but reports the incomplete result.
- [ ] Set `PYTHONUTF8=1` in the launcher and pass `--allow-partial` before automatic labeling.
- [ ] Re-run focused tests and commit the workflow handoff task.

### Task 4: Operator reporting, cleanup, verification, and documentation

**Files:**
- Modify: `src/agentized_workflow/cli.py`
- Modify: `src/agentized_workflow/acquisition_ledger.py`
- Modify: `tests/test_photo_cli.py`
- Modify: `docs/OPERATIONS.md`

**Interfaces:**
- Adds `photos ledger-report --project-root ...` with source/outcome counts, pending duplicates, claims, and incomplete query scopes.
- Adds `photos reconcile --project-root ...` to mark manually removed local assets inactive before rebuilding metadata.

- [ ] Write failing CLI tests for ledger reporting and for reconciliation after a local image is deleted.
- [ ] Run `python -m pytest -q tests/test_photo_cli.py tests/test_photo_library.py` and confirm the commands are unavailable.
- [ ] Implement read-only reporting and non-destructive reconciliation. Reconciliation updates only database status and rewrites metadata; it never removes a user file.
- [ ] Document launch, partial-success handling, retries, pagination refresh, report, and reconciliation.
- [ ] Run `python -m pytest -q --basetemp=D:/codex/new-agent/.pytest_resilience_full`, inspect the Git diff, commit, and push source code only. Do not start a real crawl or model-labeling run during verification.
