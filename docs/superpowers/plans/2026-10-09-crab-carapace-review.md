# Crab carapace review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Tighten crab localization instructions and send suspected marked carapace asymmetry to human review, even when repeated boxes pass IoU.

**Architecture:** Version 2 requests a per-box carapace_check with status and short visible evidence. Matching and contextual review treat suspected_asymmetry as a human review gate; other targets retain their accepted results. SQLite state and partial JSON retain candidate boxes and evidence. This is a model-reported qualitative flag, not a measurement from the full animal bbox.

**Tech Stack:** Python, Pydantic, SQLite, Pillow, pytest; offline transport fixtures.

**Spec:** The following user-approved behavior is the specification: improve crab prompts; if one side of the visible carapace is much wider relative to its midline, request human review. Apply only to carapace morphology, not asymmetric claws or legs. No numerical ratio was specified.

## Global Constraints

- Change version 2 only; leave version 1 providers.py, workflow.py and tools.py unchanged.
- No real model requests, collection, runtime-state migration, or re-labeling during development.
- Do not infer carapace widths from an animal bounding rectangle or add a guessed numeric threshold.
- Perspective, occlusion and cropping that prevent reliable comparison produce unassessable, not a symmetry-based rejection.
- Any suspected_asymmetry in either full pass blocks automatic acceptance of that target, regardless of IoU; this flag does not trigger another model call.
- A flagged contextual result cannot be accepted. If the flag overlaps another known target, keep that target for human review too.
- Preserve candidate boxes, flag evidence and old results; incomplete photos stay out of complete training outputs and reuse.
- Require per-box checks at the live version 2 provider boundary; old stored states and offline fixtures may omit the new optional field.
- Preserve old JSON serialization when the optional check is absent, to keep historical exports valid.
- Keep photos, input, runs, databases and .env out of Git.

### Task 1: Structured carapace review and prompt boundary

**Files:** Modify src/agentized_workflow/models.py, target_providers.py, target_matching.py, target_workflow.py; create tests/test_carapace_review.py.

**Interfaces:** Box.carapace_check: CarapaceCheck | None. CarapaceCheck.status uses not_applicable, symmetric, unassessable, suspected_asymmetry; evidence is a bounded string and required to be nonblank for suspicion. TargetHttpVision validates checks on full and cropped localization responses. pair_targets and resolve_review consume checks and emit reason carapace_asymmetry. Crop mapping preserves the check.

- [x] Write offline integration tests using actual images and SQLite for: IoU=1 with one suspicious pass; all targets suspicious; unassessable/symmetric/non-crab outcomes; flagged crop including a neighboring accepted target; preserved evidence and terminal resume; malformed/missing provider checks; old JSON serialization.
- [x] Run `python -m pytest -q tests/test_carapace_review.py --basetemp .pytest_tmp` and confirm missing-feature failures.
- [x] Add the optional validated check contract and provider schema. Include the same crab-specific anchor, tight visible-boundary and symmetry instructions in full and cropped prompts.
- [x] Apply the human gate before auto acceptance; do not route morphology flags through the third model request. Preserve checks through crop coordinate conversion.
- [x] Run the focused tests, existing target workflow tests and full offline suite.

Example behavior used by tests:

```python
# Matching boxes with IoU=1 do not override the morphology flag.
assert state.phase == 'partial_review'
assert state.targets[0].status == 'accepted'
assert state.targets[1].status == 'needs_review'
assert state.targets[1].reason == 'carapace_asymmetry'
assert not vision.crop_requests
assert not (store.root / 'results' / (spec.task_id + '.json')).exists()
```

### Task 2: Operational documentation and verification

**Files:** Modify docs/RUNBOOK.md and this plan.

**Interfaces:** Existing photos labels queue/correct and partial_results JSON; no new live command or database migration.

- [x] Document the qualitative gate, status meanings, retained evidence and candidate frames, and the complete-box-list human correction path.
- [x] Explain that the new version 2 implementation fingerprint changes; existing runs/results are not automatically re-labeled or certified against the new prompt.
- [x] Run `python -m pytest -q --basetemp .pytest_tmp`, inspect `git diff --check`, and verify old workflow files were untouched.
- [x] Record observed test counts and limitations; mark completed checkboxes only after verifying the corresponding work.
- [x] Commit only explicitly reviewed source, tests and documentation paths.

## Verified outcome

- RED: initial focused run showed 12 missing-feature failures and 2 existing-behavior passes; the evidence-specific test was then tightened and confirmed failing too.
- GREEN: 35 carapace/target workflow tests passed.
- Full offline suite: 136 passed in 17.16 seconds.
- Read-only compatibility check: all 6,515 historical version 1 task states parsed; providers.py, workflow.py and tools.py matched the prior commit.
- Diff whitespace check passed. Only explicit source, tests and documentation paths are archived.
- No live model requests, collection or historical-state writes occurred. Real-photo accuracy and review volume remain unmeasured; the check is a qualitative model flag, not measured carapace geometry.
