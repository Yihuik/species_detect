from __future__ import annotations

import csv
import json
from pathlib import Path
import sqlite3

import pytest
from PIL import Image

from agentized_workflow.label_registry import LabelRegistry, profile_for
from agentized_workflow.metadata import load_tasks
from agentized_workflow.photo_library import PhotoLibrary
from agentized_workflow.storage import Store
from agentized_workflow.workflow import Engine


class Vision:
    def visibility(self, request):
        return {"route": "whole_or_mostly_visible"}

    def localize(self, request):
        return {"boxes": [{"bbox": [100, 100, 800, 800]}]}


def prepared(tmp_path: Path):
    catalog = tmp_path / "catalog"
    catalog.mkdir()
    (catalog / "metadata.csv").write_text("species,status\n石磺,active\n", encoding="utf-8")
    batch = tmp_path / "input" / "inbox" / "one" / "石磺"
    batch.mkdir(parents=True)
    Image.new("RGB", (64, 48), "white").save(batch / "one.jpg")
    photos = tmp_path / "photos"
    library = PhotoLibrary(catalog, photos)
    assert library.ingest_batch(batch.parent).accepted == 1
    metadata = library.write_workflow_metadata()
    spec = load_tasks(photos, metadata)[0]
    profile = profile_for("qwen3-vl-plus", "https://example.invalid/v1", .7, 10, False, None)
    return photos, metadata, spec, profile


def completed(tmp_path: Path, spec):
    store = Store(tmp_path / "runs" / "first")
    engine = Engine(store, Vision())
    engine.add(spec)
    state = engine.run(spec.task_id)
    assert state.phase == "done"
    return store, state


def test_valid_machine_result_is_reused_but_human_rejection_blocks_it(tmp_path: Path):
    photos, _, spec, profile = prepared(tmp_path)
    store, state = completed(tmp_path, spec)
    registry = LabelRegistry(photos)
    case_id = registry.record(state, store.root, profile)
    assert registry.lookup(spec, profile).action == "reuse"
    assert registry.lookup(spec, profile).review_status == "unreviewed"

    registry.review(case_id, "approve", "staff-a", "checked box and species")
    assert registry.lookup(spec, profile).review_status == "approved"
    registry.review(case_id, "reject", "staff-a", "box includes background")
    assert registry.lookup(spec, profile).action == "review"
    assert registry.lookup(spec, profile).review_status == "rejected"
    assert [item["decision"] for item in registry.review_history(case_id)] == ["approve", "reject"]
    assert store.get(spec.task_id).phase == "done"


def test_cache_requires_same_photo_species_profile_and_intact_result(tmp_path: Path):
    photos, _, spec, profile = prepared(tmp_path)
    store, state = completed(tmp_path, spec)
    registry = LabelRegistry(photos)
    registry.record(state, store.root, profile)
    assert registry.lookup(spec.model_copy(update={"species": "其他物种"}), profile).action == "pending"
    assert registry.lookup(spec, "different-workflow-profile").action == "pending"
    assert registry.plan([spec], "different-workflow-profile")["profile_changed"] == 1
    (store.root / "results" / f"{spec.task_id}.json").write_text("{}", encoding="utf-8")
    assert registry.lookup(spec, profile).action == "resume"


def test_import_rejects_result_json_that_disagrees_with_sqlite_model_attempt(tmp_path: Path):
    photos, _, spec, profile = prepared(tmp_path)
    store, state = completed(tmp_path, spec)
    path = store.root / "results" / f"{spec.task_id}.json"
    result = json.loads(path.read_text(encoding="utf-8"))
    result["detections"][0]["bbox"] = [200, 200, 500, 500]
    path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    registry = LabelRegistry(photos)
    with pytest.raises(ValueError, match="valid result"):
        registry.record(state, store.root, profile)
    report = registry.import_run(store.root, profile, threshold=.7, max_targets=10,
                                 model="qwen3-vl-plus")
    assert report["invalid"] == 1
    assert not (store.root / "label_profile.json").exists()


def test_corrupt_export_is_rebuilt_from_done_state_without_model_call(tmp_path: Path, monkeypatch):
    from agentized_workflow.cli import main

    photos, _, spec, profile = prepared(tmp_path)
    store, state = completed(tmp_path, spec)
    registry = LabelRegistry(photos)
    registry.record(state, store.root, profile)
    store.atomic_json("label_profile.json", {"profile": profile, "model": "qwen3-vl-plus",
                                             "threshold": .7, "max_targets": 10})
    result = store.root / "results" / f"{spec.task_id}.json"
    result.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "offline-test")
    assert main(["--resume-existing", "--live", "--base-url", "https://example.invalid/v1",
                 "--label-registry", str(photos), "--run-dir", str(store.root)]) == 0
    assert registry.lookup(spec, profile).action == "reuse"


def test_retry_of_rejected_result_creates_new_case_without_losing_review_history(tmp_path: Path):
    photos, _, spec, profile = prepared(tmp_path)
    store, state = completed(tmp_path, spec)
    registry = LabelRegistry(photos)
    old_id = registry.record(state, store.root, profile)
    registry.review(old_id, "reject", "staff-a", "wrong position")
    with pytest.raises(ValueError, match="rejected"):
        registry.plan([spec], profile, retry_case_id=old_id + 999)
    assert registry.lookup(spec, profile, retry_case_id=old_id + 999).action == "review"
    assert registry.lookup(spec, profile, retry_case_id=old_id).action == "pending"
    assert registry.lookup(spec, profile, retry_rejected=True).action == "pending"
    next_store = Store(tmp_path / "runs" / "second")
    engine = Engine(next_store, Vision())
    engine.add(spec)
    new_id = registry.record(engine.run(spec.task_id), next_store.root, profile)
    assert new_id != old_id
    assert registry.lookup(spec, profile).action == "reuse"
    assert registry.lookup(spec, profile).review_status == "unreviewed"
    assert registry.review_history(old_id)[0]["decision"] == "reject"


def test_human_correction_keeps_original_and_approves_new_boxes(tmp_path: Path):
    photos, _, spec, profile = prepared(tmp_path)
    store, state = completed(tmp_path, spec)
    registry = LabelRegistry(photos)
    old_id = registry.record(state, store.root, profile)
    new_id = registry.correct(old_id, [[2, 3, 40, 30]], "staff-a", "left edge was wrong")
    assert new_id != old_id
    assert registry.lookup(spec, profile).action == "reuse"
    assert registry.lookup(spec, profile).review_status == "approved"
    assert registry.review_history(old_id)[-1]["decision"] == "reject"
    assert registry.review_history(new_id)[-1]["decision"] == "approve"
    corrected = registry.case(new_id)
    assert corrected["machine_status"] == "not_applicable"
    assert corrected["result_kind"] == "human_correction"
    result = json.loads(Path(corrected["result_path"]).read_text(encoding="utf-8"))
    assert result["detections"][0]["bbox_pixel"] == [2, 3, 40, 30]
    assert (Path(corrected["run_dir"]) / "annotated" / "石磺" / "01_完整或大部分可见" /
            f"{Path(spec.source_image).stem}__{Path(corrected['result_path']).stem}.jpg").is_file()
    assert (store.root / "results" / f"{spec.task_id}.json").is_file()
    assert old_id not in [item["id"] for item in registry.review_queue()]
    Path(corrected["result_path"]).write_text("{}", encoding="utf-8")
    assert registry.lookup(spec, profile).action == "review"


def test_reviewer_can_correct_a_prior_manual_correction(tmp_path: Path):
    photos, _, spec, profile = prepared(tmp_path)
    store, state = completed(tmp_path, spec)
    registry = LabelRegistry(photos)
    original = registry.record(state, store.root, profile)
    first = registry.correct(original, [[2, 3, 40, 30]], "staff-a", "first adjustment")
    with pytest.raises(ValueError, match="superseded"):
        registry.review(original, "approve", "staff-a", "use old result")
    second = registry.correct(first, [[5, 7, 42, 32]], "staff-b", "second adjustment")
    assert second != first
    assert registry.lookup(spec, profile).review_status == "approved"
    assert registry.review_history(first)[-1]["decision"] == "reject"
    output = json.loads(Path(registry.case(second)["result_path"]).read_text(encoding="utf-8"))
    assert output["detections"][0]["bbox_pixel"] == [5, 7, 42, 32]


def test_second_cli_run_reuses_result_without_creating_another_run(tmp_path: Path, capsys):
    from agentized_workflow.cli import main

    photos, metadata, spec, _ = prepared(tmp_path)
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps({spec.source_image: {
        "visibility": {"route": "whole_or_mostly_visible"},
        "localizations": [{"boxes": [{"bbox": [100, 100, 800, 800]}]}] * 2,
    }}), encoding="utf-8")
    args = ["--input-dir", str(photos), "--metadata-csv", str(metadata),
            "--fixture", str(fixture), "--label-registry", str(photos)]
    assert main([*args, "--run-dir", str(tmp_path / "runs" / "first")]) == 0
    capsys.readouterr()
    assert main([*args, "--run-dir", str(tmp_path / "runs" / "second")]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["reused"] == 1 and report["new"] == 0
    assert not (tmp_path / "runs" / "second").exists()
    with pytest.raises(ValueError, match="profile"):
        main([*args, "--model", "changed-model", "--run-dir", str(tmp_path / "runs" / "third")])
    assert not (tmp_path / "runs" / "third").exists()


def test_resume_uses_stored_specs_when_metadata_csv_grows(tmp_path: Path):
    from agentized_workflow.cli import main

    photos, metadata, spec, _ = prepared(tmp_path)
    run = tmp_path / "runs" / "first"
    store = Store(run)
    engine = Engine(store, Vision())
    engine.add(spec)
    engine.step(spec.task_id)
    with metadata.open("a", encoding="utf-8", newline="") as handle:
        csv.writer(handle).writerow(["extra.jpg", "石磺"])
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps({spec.source_image: {
        "visibility": {"route": "whole_or_mostly_visible"},
        "localizations": [{"boxes": [{"bbox": [100, 100, 800, 800]}]}] * 2,
    }}), encoding="utf-8")
    assert main(["--resume-existing", "--fixture", str(fixture), "--run-dir", str(run)]) == 0
    assert Store(run).get(spec.task_id).phase == "done"


def test_explicit_historical_import_and_operator_review_commands(tmp_path: Path, capsys):
    from agentized_workflow.cli import main

    photos, metadata, spec, profile = prepared(tmp_path)
    store, state = completed(tmp_path, spec)
    common = ["--photos-root", str(photos)]
    assert main(["photos", "labels", "import-run", *common, "--run-dir", str(store.root),
                 "--model", "qwen3-vl-plus", "--base-url", "https://example.invalid/v1"]) == 0
    imported = json.loads(capsys.readouterr().out)
    assert imported["done"] == 1
    registry = LabelRegistry(photos)
    match = registry.lookup(spec, profile)
    assert match.action == "reuse" and match.review_status == "unreviewed"
    env_file = tmp_path / ".env"
    env_file.write_text("DASHSCOPE_BASE_URL=https://example.invalid/v1\n", encoding="utf-8")
    assert main(["photos", "labels", "plan", *common,
                 "--metadata-csv", str(metadata), "--env-file", str(env_file)]) == 0
    assert json.loads(capsys.readouterr().out)["reused"] == 1
    assert main(["photos", "labels", "list", *common]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed[0]["id"] == match.case_id
    assert listed[0]["review_status"] == "unreviewed"

    assert main(["photos", "labels", "review", *common, "--case-id", str(match.case_id),
                 "--decision", "reject", "--reviewer", "staff-a", "--reason", "bad bbox"]) == 0
    capsys.readouterr()
    assert registry.lookup(spec, profile).action == "review"
    assert main(["photos", "labels", "queue", *common]) == 0
    queue = json.loads(capsys.readouterr().out)
    assert queue[0]["id"] == match.case_id
    boxes_file = tmp_path / "boxes.json"
    boxes_file.write_text(json.dumps({"boxes": [[2, 3, 40, 30]]}), encoding="utf-8")
    assert main(["photos", "labels", "correct", *common, "--case-id", str(match.case_id),
                 "--boxes-file", str(boxes_file), "--reviewer", "staff-a",
                 "--reason", "fixed bbox"]) == 0
    capsys.readouterr()
    assert registry.lookup(spec, profile).review_status == "approved"


def test_incomplete_case_is_reported_for_resume_not_repeated_in_new_run(tmp_path: Path):
    photos, _, spec, profile = prepared(tmp_path)
    run = tmp_path / "runs" / "first"
    store = Store(run)
    engine = Engine(store, Vision())
    engine.add(spec)
    registry = LabelRegistry(photos)
    registry.record(store.get(spec.task_id), run, profile)
    lookup = registry.lookup(spec, profile)
    assert lookup.action == "resume"
    assert lookup.run_dir == str(run)


def test_plan_requires_historical_import_before_changing_library_schema(tmp_path: Path):
    from agentized_workflow.cli import main

    photos, metadata, spec, _ = prepared(tmp_path)
    store = Store(tmp_path / "runs" / "agent-old")
    engine = Engine(store, Vision())
    engine.add(spec)
    engine.run(spec.task_id)
    with pytest.raises(ValueError, match="historical runs require"):
        main(["photos", "labels", "plan", "--photos-root", str(photos),
              "--metadata-csv", str(metadata), "--base-url", "https://example.invalid/v1"])
    with sqlite3.connect(photos / "photo_library.sqlite3") as db:
        assert db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='label_results'").fetchone() is None


def test_removed_inactive_photo_does_not_block_rendering_current_library(tmp_path: Path):
    photos, _, spec, profile = prepared(tmp_path)
    store, state = completed(tmp_path, spec)
    registry = LabelRegistry(photos)
    registry.record(state, store.root, profile)
    with sqlite3.connect(photos / "photo_library.sqlite3") as db:
        db.execute("UPDATE assets SET status='inactive'")
    Path(spec.image_path).unlink()
    assert registry.render_missing()["failed"] == 0


def test_registry_backfill_moves_legacy_image_and_preserves_reuse(tmp_path: Path):
    from agentized_workflow.render_labels import _render_one
    photos, _, spec, profile = prepared(tmp_path)
    store, state = completed(tmp_path, spec)
    registry = LabelRegistry(photos); registry.record(state, store.root, profile)
    result = store.root / 'results' / f'{spec.task_id}.json'
    flat = store.root / 'annotated' / f'{spec.task_id}.jpg'
    _render_one(photos, result, flat)
    before = flat.read_bytes()
    report = registry.render_missing()
    grouped = store.root / 'annotated' / '石磺' / '01_完整或大部分可见' / f'{Path(spec.source_image).stem}__{spec.task_id}.jpg'
    assert grouped.is_file() and not flat.exists()
    assert grouped.read_bytes() == before and report['moved'] == 1
    assert (store.root / 'annotated' / 'index.csv').is_file()
    assert registry.lookup(spec, profile).action == 'reuse'
    assert registry.render_missing()['skipped'] == 1


def test_manual_correction_and_backfill_share_grouped_layout(tmp_path: Path):
    photos, _, spec, profile = prepared(tmp_path)
    store, state = completed(tmp_path, spec)
    registry = LabelRegistry(photos)
    original = registry.record(state, store.root, profile)
    case_id = registry.correct(original, [[2, 3, 40, 30]], 'reviewer', 'checked')
    case = registry.case(case_id)
    manual = Path(case['run_dir']); result_id = Path(case['result_path']).stem
    grouped = manual / 'annotated' / '石磺' / '01_完整或大部分可见' / f'{Path(spec.source_image).stem}__{result_id}.jpg'
    assert grouped.is_file() and (manual / 'annotated' / 'index.csv').is_file()
    grouped.unlink()
    assert registry.render_missing()['rendered'] == 1
    assert grouped.is_file() and not (manual / 'annotated' / f'{result_id}.jpg').exists()
    assert registry.lookup(spec, profile).review_status == 'approved'
