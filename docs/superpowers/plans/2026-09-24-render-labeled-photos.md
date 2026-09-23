# Render Labeled Photos Implementation Plan

> **For agentic workers:** Execute this plan in the current session. Use test-driven-development for the new renderer and verify the complete offline suite before applying it to the live run.

**Goal:** Turn completed bbox JSON results into separate annotated photo files without recalling the model or changing task state.

**Architecture:** A standalone renderer reads atomically published `results/<task-id>.json`, resolves each `source_image` under `photos/`, applies the same EXIF orientation as the model request, draws the pixel-space boxes and trusted species name, and atomically writes `annotated/<task-id>.jpg`. It skips up-to-date output, so it can be rerun after an interrupted backfill while the model worker continues.

**Tech Stack:** Python 3, Pillow, pytest, existing `safe_relative` and result JSON contract.

**Spec:** Existing run contract in `docs/RUNBOOK.md`; user requested separate annotated photos and JSON results.

## Global Constraints

- Keep photos, `.env`, run database, and generated outputs local; push only code, tests, and documentation.
- Never re-request model inference or alter `state.sqlite3` when rendering.
- The model sees `ImageOps.exif_transpose` output, so rendered coordinates must use the same orientation.
- Preserve `results/` JSON; publish photos under a distinct `annotated/` directory.

---

### Task 1: Idempotent annotated-photo renderer

**Files:**
- Create: `src/agentized_workflow/render_labels.py`
- Create: `tests/test_render_labels.py`

**Interfaces:**
- Consumes: `render_completed(photos_root: Path, run_dir: Path) -> dict[str, int]`, result JSON with `source_image`, `metadata.species`, `image_width`, `image_height`, and `detections[].bbox_pixel`.
- Produces: `run_dir/annotated/<task-id>.jpg`, counters for rendered, skipped, failed.

- [x] Write a failing test that creates a real image plus result JSON, runs `render_completed`, checks a red border at the expected pixel, unchanged pixels elsewhere, correct output location, and unchanged mtime on rerun.
- [x] Run `python -m pytest -q --basetemp .pytest_tmp tests/test_render_labels.py` and confirm failure is due to missing renderer.
- [x] Implement source-path containment, EXIF transpose, dimensions check, box/name drawing, JPEG atomic publication, and skip logic.
- [x] Rerun the focused test, then the full offline suite (78 passed).
- [x] Backfill the active run once (1,665 rendered, 0 failed), verify output count and inspect a representative annotated image.
- [ ] Commit and push only source, test, and plan files.

### Task 2: Continuing backfill

**Files:**
- Modify: `docs/RUNBOOK.md`

**Interfaces:**
- Consumes: Task 1 renderer command `python -m agentized_workflow.render_labels --photos-root D:\codex\new-agent\photos --run-dir D:\codex\new-agent\runs\agent-20260922-142848`.
- Produces: Durable instruction to rerun rendering after future labeling progress and at terminal validation.

- [x] Add the exact render command and expected `annotated/` output path to the runbook.
- [x] Update the existing low-frequency task follow-up to backfill fresh JSON results at each static check and at completion.
- [ ] At final completion, verify one annotated photo per `done` task and report `needs_review` separately.
