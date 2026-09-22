from datetime import datetime, timedelta, timezone

from agentized_workflow.acquisition_ledger import AcquisitionLedger
from agentized_workflow.remote_identity import (
    RemoteAlias,
    RemoteIdentity,
    inaturalist_identity,
)


def test_completed_remote_version_is_skipped_before_download(tmp_path):
    ledger = AcquisitionLedger(tmp_path / "photo_library.sqlite3")
    identity = inaturalist_identity(91, 101, "https://static.inaturalist.org/photos/91/original.jpg")
    version_id = ledger.register(identity)
    token = ledger.claim_download(version_id, run_id="run-a", now=_now())
    ledger.record_download(version_id, token, content_sha256="a" * 64, outcome="accepted", now=_now())

    decision = ledger.lookup(identity, policy_fingerprint="policy-a", now=_now())

    assert decision.action == "skip_known_version"
    assert decision.remote_version_id == version_id


def test_immutable_url_alias_skips_same_media_seen_from_another_source(tmp_path):
    ledger = AcquisitionLedger(tmp_path / "photo_library.sqlite3")
    original = RemoteIdentity(
        source="commons",
        provider_asset_key="page:1",
        version_key="sha1:one",
        aliases=(RemoteAlias("canonical_url", "https://images.example.org/one.jpg", immutable=True),),
    )
    original_id = ledger.register(original)
    token = ledger.claim_download(original_id, run_id="run-a", now=_now())
    ledger.record_download(original_id, token, content_sha256="b" * 64, outcome="accepted", now=_now())
    later = RemoteIdentity(
        source="gbif",
        provider_asset_key="dataset:one:occurrence:2:media:two",
        version_key="v1",
        aliases=(RemoteAlias("canonical_url", "https://images.example.org/one.jpg", immutable=True),),
    )

    decision = ledger.lookup(later, policy_fingerprint="policy-a", now=_now())

    assert decision.action == "skip_immutable_alias"
    assert decision.remote_version_id == original_id


def test_policy_change_can_reconsider_an_undownloaded_license_rejection(tmp_path):
    ledger = AcquisitionLedger(tmp_path / "photo_library.sqlite3")
    identity = inaturalist_identity(92, 101, "https://static.inaturalist.org/photos/92/original.jpg")
    version_id = ledger.register(identity)
    ledger.record_decision(version_id, policy_fingerprint="strict", outcome="rejected_license", now=_now())

    assert ledger.lookup(identity, policy_fingerprint="strict", now=_now()).action == "skip_policy_decision"
    assert ledger.lookup(identity, policy_fingerprint="broader", now=_now()).action == "download"


def test_expired_download_claim_can_be_recovered(tmp_path):
    ledger = AcquisitionLedger(tmp_path / "photo_library.sqlite3")
    identity = inaturalist_identity(93, 101, "https://static.inaturalist.org/photos/93/original.jpg")
    version_id = ledger.register(identity)
    first = ledger.claim_download(version_id, run_id="run-a", now=_now(), lease_seconds=10)

    assert ledger.claim_download(version_id, run_id="run-b", now=_now() + timedelta(seconds=5), lease_seconds=10) is None
    second = ledger.claim_download(version_id, run_id="run-b", now=_now() + timedelta(seconds=11), lease_seconds=10)

    assert first is not None
    assert second is not None
    assert second != first


def test_failed_transfer_releases_its_claim_for_a_later_retry(tmp_path):
    ledger = AcquisitionLedger(tmp_path / "photo_library.sqlite3")
    identity = inaturalist_identity(94, 101, "https://static.inaturalist.org/photos/94/original.jpg")
    version_id = ledger.register(identity)
    token = ledger.claim_download(version_id, run_id="run-a", now=_now())

    ledger.record_download_failure(version_id, token, outcome="download_failed", now=_now())

    assert ledger.lookup(identity, policy_fingerprint="policy-a", now=_now()).action == "download"
    assert ledger.claim_download(version_id, run_id="run-b", now=_now()) is not None


def test_query_scope_resumes_cursor_and_keeps_it_after_failure(tmp_path):
    ledger = AcquisitionLedger(tmp_path / "photo_library.sqlite3")

    ledger.checkpoint_query("gbif", "甲蟹", "query-a", {"offset": 300}, now=_now())
    ledger.fail_query("gbif", "甲蟹", "query-a", "503 upstream", now=_now())

    scope = ledger.query_scope("gbif", "甲蟹", "query-a")
    assert scope.cursor == {"offset": 300}
    assert scope.status == "failed"
    assert scope.last_error == "503 upstream"

    ledger.complete_query("gbif", "甲蟹", "query-a", now=_now())

    completed = ledger.query_scope("gbif", "甲蟹", "query-a")
    assert completed.cursor is None
    assert completed.status == "completed"


def _now() -> datetime:
    return datetime(2026, 9, 20, tzinfo=timezone.utc)
