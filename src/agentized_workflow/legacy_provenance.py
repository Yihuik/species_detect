"""Read historical collector metadata into the current photo-library ledger."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re
import sqlite3
from typing import Iterator

from .acquisition_ledger import AcquisitionLedger
from .remote_identity import RemoteAlias, RemoteIdentity, commons_identity, gbif_identity, inaturalist_identity


@dataclass(frozen=True)
class MigrationReport:
    scanned_records: int = 0
    registered_versions: int = 0
    linked_assets: int = 0
    unmatched_content: int = 0
    invalid_records: int = 0


def migrate_legacy_provenance(legacy_root: Path, photos_root: Path) -> MigrationReport:
    """Import legacy source metadata by hash without reading, copying, or moving images."""

    legacy = Path(legacy_root).resolve(strict=True)
    photos = Path(photos_root).resolve()
    ledger = AcquisitionLedger(photos / "photo_library.sqlite3")
    report = MigrationReport()
    seen: set[tuple[str, str, str]] = set()
    for payload in _legacy_payloads(legacy):
        report = _add(report, scanned_records=1)
        try:
            source = str(payload.get("source") or "").strip()
            asset_key = str(payload.get("asset_key") or "").strip()
            sha256 = str(payload.get("sha256") or "").strip().casefold()
            if not source or not asset_key or not re.fullmatch(r"[0-9a-f]{64}", sha256):
                raise ValueError("missing source, asset_key, or SHA-256")
            key = (source, asset_key, sha256)
            if key in seen:
                continue
            seen.add(key)
            identity = _identity_from_payload(payload)
            asset_id = _asset_id_for_hash(photos / "photo_library.sqlite3", sha256)
            ledger.import_content(identity, content_sha256=sha256, local_asset_id=asset_id, metadata=payload)
            report = _add(
                report,
                registered_versions=1,
                linked_assets=1 if asset_id is not None else 0,
                unmatched_content=1 if asset_id is None else 0,
            )
        except (TypeError, ValueError, sqlite3.Error, json.JSONDecodeError):
            report = _add(report, invalid_records=1)
    (photos / "provenance_migration_report.json").write_text(
        json.dumps(asdict(report), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report


def _legacy_payloads(root: Path) -> Iterator[dict[str, object]]:
    state_path = root / "collector_state.sqlite3"
    if state_path.is_file():
        with sqlite3.connect(state_path) as database:
            database.row_factory = sqlite3.Row
            try:
                rows = database.execute(
                    "SELECT source,asset_key,sha256,metadata_json FROM records WHERE sha256 IS NOT NULL"
                )
            except sqlite3.Error:
                rows = ()
            for row in rows:
                try:
                    payload = json.loads(str(row["metadata_json"]))
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if isinstance(payload, dict):
                    payload.setdefault("source", row["source"])
                    payload.setdefault("asset_key", row["asset_key"])
                    payload.setdefault("sha256", row["sha256"])
                    yield payload
    for path in root.rglob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            yield payload


def _identity_from_payload(payload: dict[str, object]) -> RemoteIdentity:
    source = str(payload["source"])
    metadata = payload.get("source_metadata")
    source_metadata = metadata if isinstance(metadata, dict) else {}
    image_url = str(payload.get("image_url") or source_metadata.get("original_image_url") or "")
    if source == "inaturalist":
        photo_id = source_metadata.get("photo_id")
        observation_id = source_metadata.get("observation_id")
        if photo_id is not None and observation_id is not None and image_url:
            return inaturalist_identity(photo_id, observation_id, image_url)
    if source == "gbif":
        dataset_key = source_metadata.get("dataset_key")
        occurrence_key = source_metadata.get("gbif_key")
        if dataset_key is not None and occurrence_key is not None and image_url:
            return gbif_identity(str(dataset_key), occurrence_key, image_url)
    if source == "commons":
        page_id = source_metadata.get("commons_page_id")
        match = re.search(r"(?:^|:)sha1:([^:]+)$", str(payload.get("asset_key") or ""))
        original_url = str(source_metadata.get("original_image_url") or image_url)
        if page_id is not None and match is not None and original_url:
            return commons_identity(page_id, match.group(1), original_url)
    asset_key = str(payload["asset_key"])
    sha256 = str(payload["sha256"])
    aliases = (RemoteAlias("canonical_url", image_url, immutable=False),) if image_url else ()
    return RemoteIdentity(source, asset_key, f"legacy-sha256:{sha256}", aliases)


def _asset_id_for_hash(database_path: Path, sha256: str) -> str | None:
    if not database_path.is_file():
        return None
    with sqlite3.connect(database_path) as database:
        try:
            row = database.execute("SELECT asset_id FROM assets WHERE sha256=?", (sha256,)).fetchone()
        except sqlite3.Error:
            return None
    return str(row[0]) if row is not None else None


def _add(report: MigrationReport, **increments: int) -> MigrationReport:
    values = asdict(report)
    for field, increment in increments.items():
        values[field] += increment
    return MigrationReport(**values)
