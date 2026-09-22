from pathlib import Path

from PIL import Image

from agentized_workflow.acquisition_ledger import AcquisitionLedger
from agentized_workflow.collector_flow import RemoteCandidate, process_remote_candidate
from agentized_workflow.photo_library import PhotoLibrary
from agentized_workflow.remote_identity import inaturalist_identity
import pytest


def test_ledger_flow_downloads_once_then_skips_known_remote_version(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog"
    catalog.mkdir()
    (catalog / "metadata.csv").write_text(
        "species,scientific_names,status\n甲蟹,Aaa alpha,active\n", encoding="utf-8-sig"
    )
    photos = tmp_path / "photos"
    ledger = AcquisitionLedger(photos / "photo_library.sqlite3")
    library = PhotoLibrary(catalog, photos)
    image = tmp_path / "download.jpg"
    Image.new("RGB", (64, 64), "green").save(image)
    candidate = RemoteCandidate(
        identity=inaturalist_identity(91, 101, "https://static.inaturalist.org/photos/91/original.jpg"),
        species="甲蟹",
    )
    calls: list[Path] = []

    def download() -> Path:
        calls.append(image)
        return image

    first = process_remote_candidate(candidate, ledger, library, "policy-a", "run-a", download)
    second = process_remote_candidate(candidate, ledger, library, "policy-a", "run-b", download)

    assert first.outcome == "accepted"
    assert second.outcome == "skip_known_version"
    assert calls == [image]
    assert len(library.workflow_rows()) == 1


def test_failed_remote_download_releases_the_ledger_claim(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog"
    catalog.mkdir()
    (catalog / "metadata.csv").write_text("species,scientific_names,status\n甲蟹,Aaa alpha,active\n", encoding="utf-8-sig")
    photos = tmp_path / "photos"
    ledger = AcquisitionLedger(photos / "photo_library.sqlite3")
    library = PhotoLibrary(catalog, photos)
    candidate = RemoteCandidate(inaturalist_identity(92, 101, "https://static.inaturalist.org/photos/92/original.jpg"), "甲蟹")

    with pytest.raises(ValueError, match="broken image"):
        process_remote_candidate(candidate, ledger, library, "policy-a", "run-a", lambda: (_ for _ in ()).throw(ValueError("broken image")))

    version = ledger.lookup(candidate.identity, policy_fingerprint="policy-a")
    assert version.action == "download"
    assert ledger.claim_download(version.remote_version_id, run_id="run-b") is not None
