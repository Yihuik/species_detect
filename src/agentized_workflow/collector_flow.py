"""Ledger-first binary acquisition shared by all public-source adapters."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .acquisition_ledger import AcquisitionLedger
from .photo_library import PhotoLibrary, RemoteProvenance
from .remote_identity import RemoteIdentity


@dataclass(frozen=True)
class RemoteCandidate:
    identity: RemoteIdentity
    species: str


@dataclass(frozen=True)
class CollectionResult:
    outcome: str
    remote_version_id: int | None = None
    asset_id: str | None = None


def process_remote_candidate(
    candidate: RemoteCandidate,
    ledger: AcquisitionLedger,
    library: PhotoLibrary,
    policy_fingerprint: str,
    run_id: str,
    download: Callable[[], Path],
) -> CollectionResult:
    """Download only an unknown remote version, then persist its library result."""

    decision = ledger.lookup(candidate.identity, policy_fingerprint=policy_fingerprint)
    if decision.action != "download":
        return CollectionResult(decision.action, decision.remote_version_id)
    version_id = ledger.register(candidate.identity)
    claim = ledger.claim_download(version_id, run_id=run_id)
    if claim is None:
        return CollectionResult("skip_active_claim", version_id)
    try:
        staged_path = download()
        ingestion = library.ingest_remote(
            staged_path, candidate.species, RemoteProvenance(remote_version_id=version_id)
        )
        content_sha256 = library.content_sha256(staged_path)
        ledger.record_download(
            version_id, claim, content_sha256=content_sha256, outcome=ingestion.outcome
        )
        if ingestion.asset_id is not None:
            ledger.link_local_asset(version_id, ingestion.asset_id)
        return CollectionResult(ingestion.outcome, version_id, ingestion.asset_id)
    except Exception:
        # The lease intentionally expires and can be recovered by a later run.
        raise
