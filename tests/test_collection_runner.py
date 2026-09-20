from __future__ import annotations

import json
from pathlib import Path

from agentized_workflow.collection_runner import build_collection_command
from agentized_workflow.species_catalog import sync_catalog


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
