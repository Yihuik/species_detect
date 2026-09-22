"""Run the isolated public-source collector only for catalog species needing collection."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys

from .photo_library import PhotoLibrary
from .species_catalog import mark_collection_complete, pending_collection_species


SOURCES = ("inaturalist", "gbif", "commons")
UNBOUNDED_SOURCE_LIMIT = 2_000_000_000


def build_collection_command(project_root: Path, catalog_root: Path, contact: str) -> list[str]:
    if not contact.strip():
        raise ValueError("a public contact URL or email is required for collection")
    pending = pending_collection_species(catalog_root)
    photos_root = Path(catalog_root).parent / "photos"
    command = [
        sys.executable,
        str(Path(project_root) / "tools" / "collect_species_images.py"),
        "crawl",
        "--species-csv",
        str(Path(catalog_root) / "collector_species.csv"),
        "--output-root",
        str(Path(photos_root) / ".staging" / "collector_logs"),
        "--catalog-root",
        str(Path(catalog_root)),
        "--photos-root",
        str(Path(photos_root)),
        "--sources",
        *SOURCES,
        "--max-per-source",
        str(UNBOUNDED_SOURCE_LIMIT),
        "--contact",
        contact,
    ]
    for species in pending:
        command.extend(("--include-species", species))
    return command


def collect_pending(
    project_root: Path, catalog_root: Path, photos_root: Path, contact: str, *, allow_partial: bool = False,
) -> int:
    pending = pending_collection_species(catalog_root)
    if not pending:
        return 0
    command = build_collection_command(project_root, catalog_root, contact)
    completed = subprocess.run(command, check=False)
    library = PhotoLibrary(catalog_root, photos_root)
    library.write_workflow_metadata()
    if completed.returncode == 0:
        mark_collection_complete(catalog_root, pending)
        return 0
    if allow_partial:
        return 0
    return completed.returncode
