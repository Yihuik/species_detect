# Group annotated outputs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Organize marked-up photos by trusted species and user-confirmed visibility route, then reorganize the previous run without model requests.

**Architecture:** A shared output_layout module computes safe paths. The renderer moves current flat JPEGs byte-for-byte or generates missing images, and writes a local CSV index. Registry backfill and human correction use the same layout. Canonical JSON and SQLite paths stay stable.

**Tech Stack:** Python, pathlib, csv, Pillow, SQLite, pytest.

**Spec:** User confirmed grouping by visibility: whole/partial/mixed. The previous actual run is runs/agent-20260922-142848, containing 5,024 done tasks, 5,024 JSONs and 5,024 annotated JPEGs. These images are the migration scope.

## Global Constraints

- Output layout: annotated/<species>/<01_完整或大部分可见|02_局部可见|03_混合可见>/<source-stem>__<result-id>.jpg.
- Partial results use partial_annotated with the same layout; they do not enter annotated.
- Create the three classification directories for each represented species; empty directories are valid for absent categories.
- Index CSV maps species, route, original image, result ID, output path and canonical result JSON path; use UTF-8 BOM for Windows spreadsheet readers.
- Validate all resolved file movement paths under the intended run output root. Sanitize Windows filename components; never let metadata escape the root.
- A missing/unknown route fails explicitly; never guess a visibility class.
- Preserve current existing JPEG bytes and timestamps during moves; repeated rendering skips current files.
- Conflicting existing flat/grouped images fail without overwriting either; identical duplicates may be collapsed only after byte equality is verified.
- Never modify canonical JSON, model decisions, review events, source photos or .env; no collection or model requests.
- Output-only changes must not change either model workflow profile fingerprint.
- Keep generated images, indexes, migration reports and databases local.

### Task 1: Grouped renderer and migration

**Files:** Create src/agentized_workflow/output_layout.py and tests/test_output_layout.py; modify render_labels.py and test_render_labels.py.

**Interfaces:** annotation_path(run_dir, result_path, result=None) -> Path; render_result(photos_root, result_path, run_dir) -> str (rendered/moved/skipped); write_annotation_indexes(run_dir) -> None. Existing render_completed adds a moved count.

- [x] Add offline tests using actual images: all three routes; preserving current legacy image bytes and JSON; repeat rendering; repeated source basenames; partial isolation; conflicting outputs; unsafe metadata names; invalid route.
- [x] Run `python -m pytest -q tests/test_output_layout.py --basetemp .pytest_tmp` and inspect expected missing-feature failures.
- [x] Implement path sanitization and root containment, grouped rendering/migration, and stable local indexes.
- [x] Run focused renderer tests.

Example independently derived expectation:

```python
assert (run / 'annotated' / '甲蟹' / '01_完整或大部分可见' / 'photo_one__t1.jpg').is_file()
assert report['moved'] == 1
assert output.read_bytes() == previous_image_bytes
assert json_path.read_bytes() == previous_json_bytes
```

### Task 2: Registry integration and staff instructions

**Files:** Modify label_registry.py, test_label_registry.py, test_start_agent_script.py, test_target_workflow.py, README.md and docs/RUNBOOK.md.

**Interfaces:** Registry backfill and manual correction invoke render_result; output locations are never copied into canonical JSON/database records. Existing model profile_for excludes output-layout files.

- [x] Add/update integration assertions for model backfill, manual corrections, partial results and scripts using the grouped path.
- [x] Use the shared renderer from all registry output consumers, and refresh indexes for affected run roots.
- [x] Document the new directory tree, offline migration command and recursive image counting.
- [x] Run `python -m pytest -q --basetemp .pytest_tmp`; verify unchanged model profile fingerprints and `git diff --check`.

### Task 3: Previous-output migration and archive

**Files:** Runtime only: runs/agent-20260922-142848/annotated and reports/annotation-layout-20261010.json; source archive is code/tests/docs only.

- [x] Record a read-only baseline of SQLite rows, canonical JSON hashes and existing JPEG hashes, with old-to-new output mappings.
- [x] Run the local render_labels command on the previous run, respecting all path containment checks.
- [x] Compare all 5,024 moved JPEG hashes and canonical JSON/SQLite content against the baseline; reconcile per-species/route counts with done tasks.
- [x] Run the renderer again and confirm no further move/render work; preserve audit summaries locally.
- [x] Commit and archive only explicitly inspected source, tests and documentation paths.

## Observed implementation checks

- Initial grouped-output tests failed in all 10 missing-feature cases; two registry-entry-point tests also failed before integration.
- Focused suites passed after implementation; additional invalid-JSON and conflict-index regressions were reproduced and resolved.
- Complete offline suite: 149 passed in 18.82 seconds.
- Model workflow fingerprints for versions 1 and 2 were unchanged.
- Actual baseline: 5,024 images across 9 species, 4,681 whole/mostly visible and 343 partially visible.
- Nine images used case1..case7 and 漏框1/漏框2 aliases; local deterministic re-rendering established nine unique byte-exact matches. Their aliases and hashes are recorded in runtime audit reports; canonical JSONs remain unchanged.
- Actual migration: rendered 0, moved 5,024, skipped 0, failed 0.

## Final migration verification

All 5,024 image hashes and modification timestamps matched the baseline; all 5,024 canonical JSON hashes matched. Both SQLite file hashes and authoritative task rows were unchanged. The index had exactly 5,024 unique task IDs, and there were no loose JPEGs at annotated/ root. Repeating the local renderer returned rendered=0, moved=0, skipped=5024, failed=0 with a byte-identical CSV index. Temporary match-verification JPEGs were removed only after validation; their hash/alias evidence remains in local reports. Runtime reports and photos are not part of the source archive.
