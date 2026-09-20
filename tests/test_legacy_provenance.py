import hashlib
import json

from PIL import Image

from agentized_workflow.acquisition_ledger import AcquisitionLedger
from agentized_workflow.legacy_provenance import migrate_legacy_provenance
from agentized_workflow.photo_library import PhotoLibrary
from agentized_workflow.remote_identity import inaturalist_identity


def test_migration_links_legacy_sidecar_to_existing_photo_asset(tmp_path):
    catalog = tmp_path / "catalog"
    catalog.mkdir()
    (catalog / "metadata.csv").write_text(
        "species,scientific_names,status\n秋茄,Kandelia obovata,active\n",
        encoding="utf-8-sig",
    )
    batch_image = tmp_path / "batch" / "秋茄" / "legacy.jpg"
    batch_image.parent.mkdir(parents=True)
    Image.new("RGB", (64, 64), "green").save(batch_image)
    photos = tmp_path / "photos"
    library = PhotoLibrary(catalog, photos)
    assert library.ingest_batch(batch_image.parents[1]).accepted == 1
    sha256 = _sha256(batch_image)

    legacy_root = tmp_path / "legacy"
    sidecar = legacy_root / "秋茄" / "accepted" / "images" / "inaturalist_record.json"
    sidecar.parent.mkdir(parents=True)
    sidecar.write_text(
        json.dumps(
            {
                "source": "inaturalist",
                "asset_key": "observation:101:photo:91",
                "image_url": "https://static.inaturalist.org/photos/91/original.jpg",
                "sha256": sha256,
                "source_metadata": {"observation_id": 101, "photo_id": 91},
            }
        ),
        encoding="utf-8",
    )

    report = migrate_legacy_provenance(legacy_root, photos)
    ledger = AcquisitionLedger(photos / "photo_library.sqlite3")
    identity = inaturalist_identity(91, 999, "https://static.inaturalist.org/photos/91/original.jpg")

    assert report.linked_assets == 1
    assert report.unmatched_content == 0
    assert ledger.lookup(identity, policy_fingerprint="current").action == "skip_known_version"


def test_migration_reports_unmatched_legacy_content_without_copying_images(tmp_path):
    legacy_root = tmp_path / "legacy"
    sidecar = legacy_root / "秋茄" / "accepted" / "images" / "unmatched.json"
    sidecar.parent.mkdir(parents=True)
    sidecar.write_text(
        json.dumps(
            {
                "source": "inaturalist",
                "asset_key": "observation:101:photo:92",
                "image_url": "https://static.inaturalist.org/photos/92/original.jpg",
                "sha256": "f" * 64,
                "source_metadata": {"observation_id": 101, "photo_id": 92},
            }
        ),
        encoding="utf-8",
    )

    report = migrate_legacy_provenance(legacy_root, tmp_path / "photos")

    assert report.linked_assets == 0
    assert report.unmatched_content == 1
    assert not list((tmp_path / "photos").rglob("*.jpg"))


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()
