from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

from agentized_workflow.pipeline import prepare_photo_library


def test_prepare_collects_new_species_before_ingesting_user_photos(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "species.txt").write_text("甲蟹\n", encoding="utf-8")
    (config / "species_taxonomy.json").write_text(
        json.dumps({"甲蟹": {"scientific_names": ["Aaa alpha"]}}, ensure_ascii=False),
        encoding="utf-8",
    )
    user_photo = tmp_path / "input" / "inbox" / "batch" / "甲蟹" / "user.jpg"
    user_photo.parent.mkdir(parents=True)
    Image.new("RGB", (100, 80), (30, 40, 50)).save(user_photo)
    calls: list[tuple[Path, Path, Path, str]] = []

    def collect(project_root, catalog_root, photos_root, contact):
        calls.append((project_root, catalog_root, photos_root, contact))
        assert not (photos_root / "workflow_metadata.csv").exists()
        return 0

    result = prepare_photo_library(tmp_path, contact="test@example.org", collector=collect)

    assert len(calls) == 1
    assert result.ingested.accepted == 1
    assert (tmp_path / "photos" / "workflow_metadata.csv").is_file()


def test_prepare_offline_skips_network_collection_but_imports_user_photos(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "species.txt").write_text("甲蟹\n", encoding="utf-8")
    (config / "species_taxonomy.json").write_text(
        json.dumps({"甲蟹": {"scientific_names": ["Aaa alpha"]}}, ensure_ascii=False),
        encoding="utf-8",
    )
    user_photo = tmp_path / "input" / "inbox" / "batch" / "甲蟹" / "user.jpg"
    user_photo.parent.mkdir(parents=True)
    Image.new("RGB", (100, 80), (30, 40, 50)).save(user_photo)

    result = prepare_photo_library(tmp_path)

    assert result.pending_collection == ("甲蟹",)
    assert result.ingested.accepted == 1
