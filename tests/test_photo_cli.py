from __future__ import annotations

import csv
import json
import os
from pathlib import Path
import subprocess
import sys

from PIL import Image

from agentized_workflow.cli import main


def write_project_files(root: Path) -> None:
    config = root / "config"
    config.mkdir(parents=True)
    (config / "species.txt").write_text("甲蟹\n", encoding="utf-8")
    (config / "species_taxonomy.json").write_text(
        json.dumps({"甲蟹": {"scientific_names": ["Aaa alpha"]}}, ensure_ascii=False),
        encoding="utf-8",
    )


def test_photos_sync_and_ingest_commands_build_workflow_metadata(tmp_path: Path, capsys) -> None:
    write_project_files(tmp_path)
    image_path = tmp_path / "input" / "inbox" / "batch-a" / "甲蟹" / "source.jpg"
    image_path.parent.mkdir(parents=True)
    Image.new("RGB", (100, 80), (30, 40, 50)).save(image_path)

    assert main(["photos", "sync", "--project-root", str(tmp_path)]) == 0
    assert main(["photos", "ingest", "--project-root", str(tmp_path)]) == 0

    with (tmp_path / "photos" / "workflow_metadata.csv").open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1
    assert rows[0]["species"] == "甲蟹"
    assert (tmp_path / "photos" / rows[0]["source_image"]).is_file()
    assert "accepted" in capsys.readouterr().out


def test_photos_ingest_reorganizes_existing_collector_tree(tmp_path: Path) -> None:
    write_project_files(tmp_path)
    legacy_image = tmp_path / "legacy" / "甲蟹" / "accepted" / "inaturalist" / "images" / "old.jpg"
    legacy_image.parent.mkdir(parents=True)
    Image.new("RGB", (100, 80), (30, 40, 50)).save(legacy_image)

    assert main(["photos", "sync", "--project-root", str(tmp_path)]) == 0
    assert main([
        "photos", "ingest", "--project-root", str(tmp_path), "--batch", str(tmp_path / "legacy")
    ]) == 0

    assert list((tmp_path / "photos" / "甲蟹" / "images").glob("*.jpg"))


def test_module_entrypoint_forwards_process_arguments(tmp_path: Path) -> None:
    write_project_files(tmp_path)
    project = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "agentized_workflow.cli",
            "photos",
            "sync",
            "--project-root",
            str(tmp_path),
        ],
        cwd=project,
        env={**os.environ, "PYTHONPATH": str(project / "src"), "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert result.returncode == 0, result.stderr


def test_photos_collect_uses_the_catalog_pending_set(tmp_path: Path, monkeypatch) -> None:
    write_project_files(tmp_path)
    captured: dict[str, object] = {}

    def collect(project_root, catalog_root, photos_root, contact):
        captured.update(
            project_root=project_root,
            catalog_root=catalog_root,
            photos_root=photos_root,
            contact=contact,
        )
        return 0

    monkeypatch.setattr("agentized_workflow.cli.collect_pending", collect)
    assert main(["photos", "collect", "--project-root", str(tmp_path), "--contact", "test@example.org"]) == 0
    assert captured["contact"] == "test@example.org"
    assert Path(captured["catalog_root"]) == tmp_path / "catalog"


def test_migrate_provenance_command_uses_explicit_legacy_root(tmp_path: Path, monkeypatch, capsys) -> None:
    write_project_files(tmp_path)
    captured: dict[str, Path] = {}

    def migrate(legacy_root: Path, photos_root: Path):
        captured["legacy_root"] = legacy_root
        captured["photos_root"] = photos_root
        return type("Report", (), {"__dict__": {"linked_assets": 3}})()

    monkeypatch.setattr("agentized_workflow.cli.migrate_legacy_provenance", migrate)

    assert main([
        "photos", "migrate-provenance", "--project-root", str(tmp_path),
        "--legacy-root", str(tmp_path / "legacy"),
    ]) == 0
    assert captured == {"legacy_root": tmp_path / "legacy", "photos_root": tmp_path / "photos"}
    assert '"linked_assets": 3' in capsys.readouterr().out
