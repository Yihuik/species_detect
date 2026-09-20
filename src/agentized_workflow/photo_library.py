"""A durable, deduplicated image library fed by user-provided batches."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
from uuid import uuid4
import zipfile

from PIL import Image, ImageOps


IMAGE_EXTENSIONS = {".bmp", ".jpeg", ".jpg", ".png", ".webp"}


@dataclass(frozen=True)
class IngestReport:
    accepted: int = 0
    exact_duplicates: int = 0
    near_duplicates: int = 0
    unknown_species: int = 0
    invalid_images: int = 0


@dataclass(frozen=True)
class RemoteProvenance:
    remote_version_id: int


@dataclass(frozen=True)
class IngestionDecision:
    outcome: str
    asset_id: str | None = None
    matched_asset_id: str | None = None
    pending_item_id: str | None = None


class PhotoLibrary:
    def __init__(self, catalog_root: Path, photos_root: Path, *, near_distance: int = 4):
        if not 0 <= near_distance <= 64:
            raise ValueError("near_distance must be between 0 and 64")
        self.catalog_root = Path(catalog_root).resolve()
        self.photos_root = Path(photos_root).resolve()
        self.near_distance = near_distance
        self.photos_root.mkdir(parents=True, exist_ok=True)
        self._create_schema()

    def ingest_batch(self, batch_root: Path) -> IngestReport:
        batch = Path(batch_root).resolve(strict=True)
        report = IngestReport()
        direct = [(path, path.relative_to(batch).as_posix()) for path in batch.rglob("*")
                  if path.is_file() and path.suffix.casefold() in IMAGE_EXTENSIONS]
        with tempfile.TemporaryDirectory(prefix="photo-import-", dir=self.photos_root) as temporary:
            extracted = Path(temporary)
            archive_images = self._archive_images(batch, extracted)
            for path, relative in [*direct, *archive_images]:
                report = self._merge_report(report, self._ingest_one(path, relative, batch))
        return report

    def ingest_remote(
        self, image_path: Path, species: str, provenance: RemoteProvenance
    ) -> IngestionDecision:
        """Apply the same duplicate rules to a staged remote download as to user input."""

        path = Path(image_path).resolve(strict=True)
        relative = f"remote:{provenance.remote_version_id}/{path.name}"
        return self._ingest_image(
            path,
            species,
            relative,
            batch_id=f"remote:{provenance.remote_version_id}",
            remote_version_id=provenance.remote_version_id,
        )

    def pending_item_count(self) -> int:
        with self._connection() as database:
            row = database.execute("SELECT COUNT(*) AS count FROM pending_items").fetchone()
        return int(row["count"])

    def workflow_rows(self) -> list[dict[str, str]]:
        with self._connection() as database:
            rows = database.execute(
                "SELECT local_path, species FROM assets WHERE status='active' "
                "ORDER BY species COLLATE NOCASE, local_path COLLATE NOCASE"
            ).fetchall()
        return [{"source_image": row["local_path"], "species": row["species"]} for row in rows]

    def write_workflow_metadata(self) -> Path:
        path = self.photos_root / "workflow_metadata.csv"
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["source_image", "species"])
            writer.writeheader()
            writer.writerows(self.workflow_rows())
        return path

    def _ingest_one(self, image_path: Path, relative: str, batch: Path) -> IngestReport:
        species = Path(relative).parts[0] if Path(relative).parts else ""
        decision = self._ingest_image(
            image_path, species, relative, batch_id=self._batch_id(batch), remote_version_id=None
        )
        reports = {
            "accepted": IngestReport(accepted=1),
            "exact_duplicate": IngestReport(exact_duplicates=1),
            "near_duplicate": IngestReport(near_duplicates=1),
            "unknown_species": IngestReport(unknown_species=1),
            "invalid_image": IngestReport(invalid_images=1),
        }
        return reports[decision.outcome]

    def _ingest_image(
        self,
        image_path: Path,
        species: str,
        relative: str,
        *,
        batch_id: str,
        remote_version_id: int | None,
    ) -> IngestionDecision:
        if species not in self._active_species():
            pending = self._pending("unknown_species", image_path, relative, {"species": species})
            return IngestionDecision("unknown_species", pending_item_id=pending)
        try:
            identity = self._identity(image_path)
        except Exception as error:
            pending = self._pending("invalid_images", image_path, relative, {"error": type(error).__name__})
            return IngestionDecision("invalid_image", pending_item_id=pending)

        with self._connection() as database:
            exact = database.execute(
                "SELECT asset_id, species FROM assets WHERE sha256=? AND status='active'",
                (identity["sha256"],),
            ).fetchone()
            pixels = database.execute(
                "SELECT asset_id, species FROM assets WHERE pixel_sha256=? AND status='active'",
                (identity["pixel_sha256"],),
            ).fetchone()
            duplicate = exact or pixels
            if duplicate is not None:
                kind = "cross_species_duplicates" if duplicate["species"] != species else "exact_duplicates"
                pending = self._pending(kind, image_path, relative, {
                    "duplicate_of": duplicate["asset_id"], "duplicate_species": duplicate["species"],
                    "sha256": identity["sha256"],
                }, database=database, remote_version_id=remote_version_id, identity=identity,
                   matched_asset_id=str(duplicate["asset_id"]))
                return IngestionDecision("exact_duplicate", matched_asset_id=str(duplicate["asset_id"]), pending_item_id=pending)

            near = self._near_match(database, species, identity["dhash"])
            if near is not None:
                pending = self._pending("near_duplicates", image_path, relative, {
                    "candidate_of": near["asset_id"], "distance": near["distance"],
                    "sha256": identity["sha256"],
                }, database=database, remote_version_id=remote_version_id, identity=identity,
                   matched_asset_id=str(near["asset_id"]))
                return IngestionDecision("near_duplicate", matched_asset_id=str(near["asset_id"]), pending_item_id=pending)

            asset_id = identity["sha256"][:24]
            target = self.photos_root / species / "images" / f"photo_{asset_id}{image_path.suffix.casefold()}"
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                shutil.copy2(image_path, target)
            database.execute(
                "INSERT INTO assets(asset_id,species,sha256,pixel_sha256,dhash,local_path,source_path,batch_id,status,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (asset_id, species, identity["sha256"], identity["pixel_sha256"], identity["dhash"],
                 target.relative_to(self.photos_root).as_posix(), relative, batch_id, "active", self._now()),
            )
            return IngestionDecision("accepted", asset_id=asset_id)

    def _archive_images(self, batch: Path, temporary: Path) -> list[tuple[Path, str]]:
        images: list[tuple[Path, str]] = []
        for archive in batch.rglob("*.zip"):
            archive_root = temporary / hashlib.sha256(str(archive).encode()).hexdigest()[:16]
            with zipfile.ZipFile(archive) as package:
                members = [member for member in package.infolist() if not member.is_dir()]
                if len(members) > 10_000 or sum(member.file_size for member in members) > 2_000_000_000:
                    self._pending("invalid_archives", archive, archive.name, {"error": "archive_limit"})
                    continue
                for member in members:
                    member_path = Path(member.filename)
                    if member_path.is_absolute() or ".." in member_path.parts:
                        self._pending("invalid_archives", archive, archive.name, {"error": "unsafe_archive_path"})
                        break
                    destination = archive_root / member_path
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with package.open(member) as source, destination.open("wb") as target:
                        shutil.copyfileobj(source, target)
                    if destination.suffix.casefold() in IMAGE_EXTENSIONS:
                        images.append((destination, member_path.as_posix()))
        return images

    def _active_species(self) -> set[str]:
        with (self.catalog_root / "metadata.csv").open(encoding="utf-8-sig", newline="") as handle:
            return {
                str(row["species"])
                for row in csv.DictReader(handle)
                if row.get("status") == "active"
            }

    def _identity(self, path: Path) -> dict[str, str]:
        source_hash = self._sha256(path)
        with Image.open(path) as raw:
            image = ImageOps.exif_transpose(raw).convert("RGB")
            image.load()
        pixel_hash = hashlib.sha256(
            f"{image.width}x{image.height}:".encode() + image.tobytes()
        ).hexdigest()
        grayscale = image.convert("L").resize((9, 8))
        values = list(grayscale.get_flattened_data())
        bits = "".join("1" if values[row * 9 + column] > values[row * 9 + column + 1] else "0"
                       for row in range(8) for column in range(8))
        return {"sha256": source_hash, "pixel_sha256": pixel_hash, "dhash": f"{int(bits, 2):016x}"}

    def _near_match(self, database: sqlite3.Connection, species: str, dhash: str):
        rows = database.execute(
            "SELECT asset_id,dhash FROM assets WHERE species=? AND status='active'", (species,)
        ).fetchall()
        matches = []
        value = int(dhash, 16)
        for row in rows:
            distance = (value ^ int(row["dhash"], 16)).bit_count()
            if distance <= self.near_distance:
                matches.append((distance, row["asset_id"]))
        if not matches:
            return None
        distance, asset_id = min(matches)
        return {"asset_id": asset_id, "distance": distance}

    def _pending(
        self, reason: str, source: Path, relative: str, detail: dict[str, object], *,
        database: sqlite3.Connection | None = None, remote_version_id: int | None = None,
        identity: dict[str, str] | None = None, matched_asset_id: str | None = None,
    ) -> str:
        directory = self.photos_root / "pending" / reason
        directory.mkdir(parents=True, exist_ok=True)
        identifier = hashlib.sha256((str(source) + str(uuid4())).encode()).hexdigest()[:24]
        copy = directory / f"photo_{identifier}{source.suffix.casefold()}"
        if source.suffix.casefold() in IMAGE_EXTENSIONS and source.is_file():
            shutil.copy2(source, copy)
        (directory / f"photo_{identifier}.json").write_text(
            json.dumps({"source_image": relative, "reason": reason, **detail}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        if database is not None:
            database.execute(
                "INSERT INTO pending_items(pending_item_id,reason,local_path,sha256,pixel_sha256,dhash,matched_asset_id,remote_version_id,status,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (identifier, reason, copy.relative_to(self.photos_root).as_posix() if copy.exists() else None,
                 identity.get("sha256") if identity else None,
                 identity.get("pixel_sha256") if identity else None,
                 identity.get("dhash") if identity else None, matched_asset_id, remote_version_id, "pending", self._now()),
            )
        return identifier

    def _create_schema(self) -> None:
        with self._connection() as database:
            database.execute(
                "CREATE TABLE IF NOT EXISTS assets("
                "asset_id TEXT PRIMARY KEY, species TEXT NOT NULL, sha256 TEXT NOT NULL, "
                "pixel_sha256 TEXT NOT NULL, dhash TEXT NOT NULL, local_path TEXT NOT NULL UNIQUE, "
                "source_path TEXT NOT NULL, batch_id TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL)"
            )
            database.execute("CREATE INDEX IF NOT EXISTS assets_sha ON assets(sha256)")
            database.execute("CREATE INDEX IF NOT EXISTS assets_pixel_sha ON assets(pixel_sha256)")
            database.execute("CREATE INDEX IF NOT EXISTS assets_species ON assets(species, status)")
            database.execute(
                "CREATE TABLE IF NOT EXISTS pending_items("
                "pending_item_id TEXT PRIMARY KEY,reason TEXT NOT NULL,local_path TEXT,sha256 TEXT,pixel_sha256 TEXT,"
                "dhash TEXT,matched_asset_id TEXT,remote_version_id INTEGER,status TEXT NOT NULL,created_at TEXT NOT NULL)"
            )

    def _connection(self) -> sqlite3.Connection:
        database = sqlite3.connect(self.photos_root / "photo_library.sqlite3")
        database.row_factory = sqlite3.Row
        return database

    @staticmethod
    def _merge_report(left: IngestReport, right: IngestReport) -> IngestReport:
        return IngestReport(**{field: getattr(left, field) + getattr(right, field)
                               for field in IngestReport.__dataclass_fields__})

    @staticmethod
    def _sha256(path: Path) -> str:
        hasher = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                hasher.update(chunk)
        return hasher.hexdigest()

    @staticmethod
    def _batch_id(path: Path) -> str:
        return hashlib.sha256(str(path).encode()).hexdigest()[:24]

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()
