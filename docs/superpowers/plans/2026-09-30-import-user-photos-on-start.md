# Import User Photos on Start Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A fresh one-click run ingests `input/inbox` user photos into the deduplicated `photos/` library before labeling starts.

**Architecture:** Reuse the existing `photos ingest` CLI, which scans inbox batches, applies the library's duplicate rules, and regenerates `workflow_metadata.csv`. Insert it in `tools/start_agent.ps1` after collection and before the live labeler; abort on a nonzero exit code. Keep the resume launcher unchanged so a frozen run never acquires new inputs.

**Tech Stack:** PowerShell, Python CLI, pytest.

**Spec:** `input/README.md` and the user request of 2026-09-30.

## Global Constraints

- Do not send test photos to a model or remote website.
- Preserve the source inbox and existing active photo assets.
- Do not add `photos/`, `input/inbox/`, `runs/`, `.env`, SQLite databases, or generated images to Git.

---

### Task 1: Ingest before fresh-run labeling

**Files:**
- Modify: `tools/start_agent.ps1`
- Modify: `tests/test_start_agent_script.py`
- Modify: `src/agentized_workflow/photo_library.py`
- Modify: `tests/test_photo_library.py`
- Modify: `input/README.md`

**Interfaces:**
- Consumes: `python -m agentized_workflow.cli photos ingest --project-root <root>`.
- Produces: a refreshed `photos/workflow_metadata.csv` before the labeling CLI is invoked.

- [x] Write a Windows integration test that builds a temporary project with a real inbox JPEG, runs the launcher with real catalog/ingest CLI calls and a fake live-model boundary, then asserts that the file is in `photos/` and its metadata is present when labeling begins.
- [x] Run the focused test; confirm it fails because labeling begins before ingestion.
- [x] Add one checked `photos ingest` invocation between collection and labeling in `start_agent.ps1`.
- [x] Test and fix ZIP files placed under a species directory, as documented in `input/README.md`; use that enclosing species for members with no species directory inside the ZIP.
- [x] Run the focused tests, then `python -m pytest -q --basetemp .pytest_tmp` from the project root (81 passed).
- [x] Review the diff and staged paths; this change did not resume or launch the historical long-running batch.
