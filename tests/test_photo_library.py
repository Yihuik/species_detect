from __future__ import annotations

import csv
import json
from pathlib import Path
import zipfile

import pytest
from PIL import Image

from agentized_workflow.photo_library import PhotoLibrary, RemoteProvenance
from agentized_workflow.species_catalog import (
    mark_collection_complete,
    pending_collection_species,
    sync_catalog,
)


def image(path: Path, color: tuple[int, int, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (96, 64), color).save(path)


def catalog(tmp_path: Path) -> Path:
    species_file = tmp_path / "species.txt"
    species_file.write_text("# 可信清单\n甲蟹\n乙螺\n", encoding="utf-8")
    taxonomy = tmp_path / "taxonomy.json"
    taxonomy.write_text(
        json.dumps(
            {
                "甲蟹": {"scientific_names": ["Aaa alpha"]},
                "乙螺": {"scientific_names": ["Bbb beta"]},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return sync_catalog(species_file, taxonomy, tmp_path / "catalog")


def test_sync_catalog_creates_metadata_from_species_file(tmp_path: Path) -> None:
    catalog_root = catalog(tmp_path)

    with (catalog_root / "metadata.csv").open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))

    assert rows == [
        {"species": "甲蟹", "scientific_names": "Aaa alpha", "status": "active"},
        {"species": "乙螺", "scientific_names": "Bbb beta", "status": "active"},
    ]


def test_sync_catalog_rejects_species_without_taxonomy(tmp_path: Path) -> None:
    species_file = tmp_path / "species.txt"
    species_file.write_text("未知物种\n", encoding="utf-8")
    taxonomy = tmp_path / "taxonomy.json"
    taxonomy.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="missing taxonomy"):
        sync_catalog(species_file, taxonomy, tmp_path / "catalog")


def test_catalog_tracks_only_new_species_for_collection(tmp_path: Path) -> None:
    catalog_root = catalog(tmp_path)
    assert pending_collection_species(catalog_root) == ["乙螺", "甲蟹"]

    mark_collection_complete(catalog_root, ["甲蟹"])
    sync_catalog(tmp_path / "species.txt", tmp_path / "taxonomy.json", catalog_root)
    assert pending_collection_species(catalog_root) == ["乙螺"]

    (tmp_path / "species.txt").write_text("甲蟹\n乙螺\n丙鱼\n", encoding="utf-8")
    (tmp_path / "taxonomy.json").write_text(
        json.dumps(
            {
                "甲蟹": {"scientific_names": ["Aaa alpha"]},
                "乙螺": {"scientific_names": ["Bbb beta"]},
                "丙鱼": {"scientific_names": ["Ccc gamma"]},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    sync_catalog(tmp_path / "species.txt", tmp_path / "taxonomy.json", catalog_root)
    assert pending_collection_species(catalog_root) == ["丙鱼", "乙螺"]


def test_import_uses_top_level_species_directory_and_keeps_original_input(tmp_path: Path) -> None:
    catalog_root = catalog(tmp_path)
    inbox = tmp_path / "input" / "inbox" / "batch-a"
    original = inbox / "甲蟹" / "field.jpg"
    image(original, (20, 40, 60))
    library = PhotoLibrary(catalog_root, tmp_path / "photos")

    report = library.ingest_batch(inbox)

    assert report.accepted == 1
    assert original.is_file()
    workflow_rows = library.workflow_rows()
    assert len(workflow_rows) == 1
    assert workflow_rows[0]["species"] == "甲蟹"
    assert (tmp_path / "photos" / workflow_rows[0]["source_image"]).is_file()


def test_exact_duplicate_does_not_enter_photos_twice(tmp_path: Path) -> None:
    catalog_root = catalog(tmp_path)
    first = tmp_path / "input" / "inbox" / "one" / "甲蟹" / "first.jpg"
    second = tmp_path / "input" / "inbox" / "two" / "甲蟹" / "same.jpg"
    image(first, (20, 40, 60))
    second.parent.mkdir(parents=True, exist_ok=True)
    second.write_bytes(first.read_bytes())
    library = PhotoLibrary(catalog_root, tmp_path / "photos")

    assert library.ingest_batch(first.parents[1]).accepted == 1
    report = library.ingest_batch(second.parents[1])

    assert report.exact_duplicates == 1
    assert len(library.workflow_rows()) == 1
    assert list((tmp_path / "photos" / "pending" / "exact_duplicates").glob("*.json"))


def test_near_duplicate_is_held_in_pending_not_workflow(tmp_path: Path) -> None:
    catalog_root = catalog(tmp_path)
    first = tmp_path / "input" / "inbox" / "one" / "甲蟹" / "first.png"
    near = tmp_path / "input" / "inbox" / "two" / "甲蟹" / "near.png"
    image(first, (20, 40, 60))
    image(near, (21, 41, 61))
    library = PhotoLibrary(catalog_root, tmp_path / "photos", near_distance=64)

    assert library.ingest_batch(first.parents[1]).accepted == 1
    report = library.ingest_batch(near.parents[1])

    assert report.near_duplicates == 1
    assert len(library.workflow_rows()) == 1
    assert list((tmp_path / "photos" / "pending" / "near_duplicates").glob("*.json"))


def test_unknown_top_level_directory_is_quarantined(tmp_path: Path) -> None:
    catalog_root = catalog(tmp_path)
    batch = tmp_path / "input" / "inbox" / "batch" / "未知" / "x.jpg"
    image(batch, (20, 40, 60))
    library = PhotoLibrary(catalog_root, tmp_path / "photos")

    report = library.ingest_batch(batch.parents[1])

    assert report.unknown_species == 1
    assert library.workflow_rows() == []
    assert list((tmp_path / "photos" / "pending" / "unknown_species").glob("*.json"))


def test_zip_uses_its_top_level_species_directory(tmp_path: Path) -> None:
    catalog_root = catalog(tmp_path)
    source = tmp_path / "source.png"
    image(source, (20, 40, 60))
    batch = tmp_path / "input" / "inbox" / "batch"
    batch.mkdir(parents=True)
    with zipfile.ZipFile(batch / "upload.zip", "w") as archive:
        archive.write(source, "甲蟹/from_zip.png")
    library = PhotoLibrary(catalog_root, tmp_path / "photos")

    report = library.ingest_batch(batch)

    assert report.accepted == 1
    assert len(library.workflow_rows()) == 1
    assert (batch / "upload.zip").is_file()


def test_zip_path_escape_is_held_out_of_photos(tmp_path: Path) -> None:
    catalog_root = catalog(tmp_path)
    source = tmp_path / "source.png"
    image(source, (20, 40, 60))
    batch = tmp_path / "input" / "inbox" / "batch"
    batch.mkdir(parents=True)
    with zipfile.ZipFile(batch / "unsafe.zip", "w") as archive:
        archive.write(source, "../甲蟹/escape.png")
    library = PhotoLibrary(catalog_root, tmp_path / "photos")

    report = library.ingest_batch(batch)

    assert report.accepted == 0
    assert library.workflow_rows() == []
    assert list((tmp_path / "photos" / "pending" / "invalid_archives").glob("*.json"))


def test_workflow_metadata_contains_only_active_unique_photos(tmp_path: Path) -> None:
    catalog_root = catalog(tmp_path)
    batch = tmp_path / "input" / "inbox" / "batch"
    image(batch / "甲蟹" / "a.jpg", (20, 40, 60))
    image(batch / "乙螺" / "b.jpg", (70, 80, 90))
    library = PhotoLibrary(catalog_root, tmp_path / "photos")
    library.ingest_batch(batch)

    metadata_path = library.write_workflow_metadata()
    with metadata_path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))

    assert [row["species"] for row in rows] == ["乙螺", "甲蟹"]
    assert all((tmp_path / "photos" / row["source_image"]).is_file() for row in rows)


def test_remote_ingest_returns_duplicate_decision_and_indexes_pending_item(tmp_path: Path) -> None:
    catalog_root = catalog(tmp_path)
    first = tmp_path / "first.jpg"
    second = tmp_path / "second.jpg"
    image(first, (20, 40, 60))
    second.write_bytes(first.read_bytes())
    library = PhotoLibrary(catalog_root, tmp_path / "photos")

    accepted = library.ingest_remote(first, "甲蟹", RemoteProvenance(remote_version_id=11))
    duplicate = library.ingest_remote(second, "甲蟹", RemoteProvenance(remote_version_id=12))

    assert accepted.outcome == "accepted"
    assert accepted.asset_id is not None
    assert duplicate.outcome == "exact_duplicate"
    assert duplicate.matched_asset_id == accepted.asset_id
    assert duplicate.pending_item_id is not None
    assert library.pending_item_count() == 1
