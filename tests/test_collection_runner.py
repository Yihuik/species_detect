from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from agentized_workflow.collection_runner import build_collection_command, collect_pending
from agentized_workflow.photo_library import PhotoLibrary
from agentized_workflow.species_catalog import pending_collection_species, sync_catalog


def test_collection_command_targets_only_pending_species_without_a_photo_cap(tmp_path: Path) -> None:
    species = tmp_path / "species.txt"
    species.write_text("甲蟹\n", encoding="utf-8")
    taxonomy = tmp_path / "taxonomy.json"
    taxonomy.write_text(
        json.dumps({"甲蟹": {"scientific_names": ["Aaa alpha"]}}, ensure_ascii=False),
        encoding="utf-8",
    )
    catalog = sync_catalog(species, taxonomy, tmp_path / "catalog")

    command = build_collection_command(tmp_path, catalog, "https://example.org/contact")

    assert "--include-species" in command
    assert command[command.index("--include-species") + 1] == "甲蟹"
    assert "--target-total-per-species" not in command
    assert command[command.index("--max-per-source") + 1] == "2000000000"
    assert command[command.index("--sources") + 1:command.index("--max-per-source")] == [
        "inaturalist", "gbif", "commons"
    ]
    assert command[command.index("--catalog-root") + 1] == str(catalog)
    assert command[command.index("--photos-root") + 1] == str(tmp_path / "photos")
    assert command[command.index("--output-root") + 1].endswith("photos\\.staging\\collector_logs")


def test_partial_collection_refreshes_metadata_but_keeps_species_pending(tmp_path: Path, monkeypatch) -> None:
    species = tmp_path / "species.txt"
    species.write_text("甲蟹\n", encoding="utf-8")
    taxonomy = tmp_path / "taxonomy.json"
    taxonomy.write_text('{"甲蟹":{"scientific_names":["Aaa alpha"]}}', encoding="utf-8")
    catalog = sync_catalog(species, taxonomy, tmp_path / "catalog")
    batch = tmp_path / "batch" / "甲蟹"
    batch.mkdir(parents=True)
    Image.new("RGB", (80, 80), "green").save(batch / "one.jpg")
    library = PhotoLibrary(catalog, tmp_path / "photos")
    library.ingest_batch(batch.parent)
    monkeypatch.setattr("agentized_workflow.collection_runner.subprocess.run", lambda *_args, **_kwargs: SimpleNamespace(returncode=1))

    assert collect_pending(tmp_path, catalog, tmp_path / "photos", "test@example.org", allow_partial=True) == 0
    assert (tmp_path / "photos" / "workflow_metadata.csv").is_file()
    assert pending_collection_species(catalog) == ["甲蟹"]


def test_strict_collection_returns_source_failure_after_refreshing_metadata(tmp_path: Path, monkeypatch) -> None:
    species = tmp_path / "species.txt"
    species.write_text("甲蟹\n", encoding="utf-8")
    taxonomy = tmp_path / "taxonomy.json"
    taxonomy.write_text('{"甲蟹":{"scientific_names":["Aaa alpha"]}}', encoding="utf-8")
    catalog = sync_catalog(species, taxonomy, tmp_path / "catalog")
    monkeypatch.setattr("agentized_workflow.collection_runner.subprocess.run", lambda *_args, **_kwargs: SimpleNamespace(returncode=1))

    assert collect_pending(tmp_path, catalog, tmp_path / "photos", "test@example.org") == 1
    assert (tmp_path / "photos" / "workflow_metadata.csv").is_file()
