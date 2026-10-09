# Mixed visibility and per-target review implementation plan

> Execute inline in this authorized task, with failing tests before implementation and verification before each commit.

**Goal:** Retain targets whose two independent boxes have IoU strictly greater than 0.75, and make one contextual crop review for each reliably associated unresolved target.

**Architecture:** Keep the original version 1 engine, prompts and comparison tools intact for historical runs. Add a version 2 engine and provider with `mixed`, target records, contextual crop coordinates, and composite exports. Store partial results separately from complete results and keep them in the existing human review queue.

**Tech stack:** Python, Pydantic, Pillow, SQLite, PowerShell, pytest; no new dependencies.

**Specification:** User-approved discussion in this task: two whole-image independent localizations; preserve accepted targets; third localization only for uncertain targets; no replay of interrupted calls; all complete targets must pass strict IoU > 0.75.

## Constraints

- Work only in D:/codex/new-agent; photos, SQLite, credentials and outputs remain local.
- No collection or live model requests during development.
- Version 1 uses its original 0.7 inclusive policy. Version 2 defaults to 0.75 strict comparison. A new profile requires explicit authorization before relabeling existing assets.
- The target species comes from metadata; mixed means both visibility types among that species, not every species in the photo.
- Empty, mismatched, ambiguous, extra, invalid and failed responses cannot silently become a complete annotation.
- Correspondence uses conservative overlap components (association IoU >= 0.25). Multi-target components go to review rather than guessing identities.
- Each paired unresolved target gets at most one crop call. Crops use the union of both candidate boxes plus 25% context, clipped to the oriented original image. The request contains no prior prediction boxes or scores.
- Partial accepted boxes remain in SQLite and partial_results/partial_annotated/, excluded from results/annotated/ and from automatic reuse.

## Tasks

- [x] Add real image/SQLite tests for mixed routing, strict boundary, per-target preservation, crop mapping, interrupted recovery, empty results, ambiguity, and count mismatch. Run them red.
- [x] Add target contracts, conservative pairing, crop transforms, version 2 provider and engine. Run focused tests green.
- [x] Add composite/partial exports, registry integrity validation and human correction of partial photos; test no partial reuse and retained lineage.
- [x] Wire version selection into CLI and launchers. Default new work to version 2; infer original policy on resume; retain version 1 profile fingerprints and historical importing.
- [x] Update operations manual with plan/launch/version instructions, partial output paths, and the full-box manual correction boundary.
- [x] Run `python -m pytest -q --basetemp .pytest_tmp`, inspect diff/staged paths, and commit code only.
- [x] Archive to the authorized remote. Commits a269f23 and 6901559 were pushed after using the existing Windows system proxy for the Git command. Direct Git connections reset because Git did not use that proxy.


## Verification evidence

Version 1 core prompts/engine/tools were unchanged. A read-only check confirms the
historical labeling profile still matches and all 6,515 old task states parse as
version 1. New behavior is tested with real temporary images and SQLite, while
only the external model boundary is replaced by fixtures. No real acquisition or
labeling has been started for this iteration.

Final offline suite: 122 passed. Historical profile compatibility: true; historical task states readable: 6,515, all version 1.
