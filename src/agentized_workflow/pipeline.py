"""Prepare the stable photo library before any model workflow begins."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .collection_runner import collect_pending
from .photo_library import IngestReport, PhotoLibrary
from .species_catalog import pending_collection_species, sync_catalog


@dataclass(frozen=True)
class PreparedLibrary:
    catalog_root: Path
    photos_root: Path
    metadata_path: Path
    pending_collection: tuple[str, ...]
    ingested: IngestReport


def prepare_photo_library(
    project_root: Path,
    *,
    contact: str | None = None,
    collector: Callable[[Path, Path, Path, str], int] = collect_pending,
) -> PreparedLibrary:
    root = Path(project_root).resolve()
    catalog = sync_catalog(
        root / "config" / "species.txt",
        root / "config" / "species_taxonomy.json",
        root / "catalog",
    )
    photos = root / "photos"
    pending = tuple(pending_collection_species(catalog))
    if pending and contact is not None:
        status = collector(root, catalog, photos, contact)
        if status != 0:
            raise RuntimeError(f"collection failed with exit code {status}")
        pending = tuple(pending_collection_species(catalog))

    library = PhotoLibrary(catalog, photos)
    report = IngestReport()
    inbox = root / "input" / "inbox"
    if inbox.is_dir():
        batches = [item for item in sorted(inbox.iterdir()) if item.is_dir()]
        if any(item.is_file() for item in inbox.iterdir()):
            batches.insert(0, inbox)
        for batch in batches:
            report = PhotoLibrary._merge_report(report, library.ingest_batch(batch))
    return PreparedLibrary(catalog, photos, library.write_workflow_metadata(), pending, report)
