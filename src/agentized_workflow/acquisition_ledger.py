"""Durable acquisition state for public-source image collection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from .remote_identity import RemoteIdentity


SCHEMA_VERSION = 2


@dataclass(frozen=True)
class LookupDecision:
    action: str
    remote_version_id: int | None = None


class AcquisitionLedger:
    """Record remote identities separately from policy decisions and local files."""

    def __init__(self, database_path: Path):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()

    def register(self, identity: RemoteIdentity, metadata: dict[str, object] | None = None) -> int:
        """Create or refresh a remote version and all aliases without downloading it."""

        with self._connection() as database:
            database.execute("BEGIN IMMEDIATE")
            now = _iso(_utcnow())
            database.execute(
                "INSERT INTO remote_assets(source,provider_asset_key,first_seen_at,last_seen_at) "
                "VALUES (?,?,?,?) ON CONFLICT(source,provider_asset_key) DO UPDATE SET last_seen_at=excluded.last_seen_at",
                (identity.source, identity.provider_asset_key, now, now),
            )
            asset = database.execute(
                "SELECT remote_asset_id FROM remote_assets WHERE source=? AND provider_asset_key=?",
                (identity.source, identity.provider_asset_key),
            ).fetchone()
            assert asset is not None
            database.execute(
                "INSERT INTO remote_versions(remote_asset_id,version_key,metadata_json,first_seen_at,last_seen_at) "
                "VALUES (?,?,?,?,?) ON CONFLICT(remote_asset_id,version_key) DO UPDATE SET "
                "metadata_json=excluded.metadata_json,last_seen_at=excluded.last_seen_at",
                (asset["remote_asset_id"], identity.version_key, json.dumps(metadata or {}, ensure_ascii=False), now, now),
            )
            version = database.execute(
                "SELECT remote_version_id FROM remote_versions WHERE remote_asset_id=? AND version_key=?",
                (asset["remote_asset_id"], identity.version_key),
            ).fetchone()
            assert version is not None
            for alias in identity.aliases:
                database.execute(
                    "INSERT INTO remote_aliases(alias_type,alias_value,immutable,first_seen_at,last_seen_at) VALUES (?,?,?,?,?)",
                    (alias.alias_type, alias.value, int(alias.immutable), now, now),
                )
                alias_id = database.execute("SELECT last_insert_rowid()").fetchone()[0]
                database.execute(
                    "INSERT OR IGNORE INTO remote_version_aliases(remote_version_id,alias_id) VALUES (?,?)",
                    (version["remote_version_id"], alias_id),
                )
            database.commit()
            return int(version["remote_version_id"])

    def lookup(self, identity: RemoteIdentity, *, policy_fingerprint: str, now: datetime | None = None) -> LookupDecision:
        """Return the safe next action before an image response body is requested."""

        instant = _iso(now or _utcnow())
        with self._connection() as database:
            version = self._version_for_identity(database, identity)
            if version is not None:
                version_id = int(version["remote_version_id"])
                if version["content_sha256"]:
                    return LookupDecision("skip_known_version", version_id)
                claim = database.execute(
                    "SELECT lease_expires_at FROM download_claims WHERE remote_version_id=?", (version_id,)
                ).fetchone()
                if claim is not None and str(claim["lease_expires_at"]) > instant:
                    return LookupDecision("skip_active_claim", version_id)
                decision = database.execute(
                    "SELECT outcome FROM collection_decisions WHERE remote_version_id=? AND policy_fingerprint=?",
                    (version_id, policy_fingerprint),
                ).fetchone()
                if decision is not None:
                    return LookupDecision("skip_policy_decision", version_id)
                return LookupDecision("download", version_id)

            for alias in identity.aliases:
                if not alias.immutable:
                    continue
                matched = database.execute(
                    "SELECT rv.remote_version_id FROM remote_aliases AS ra "
                    "JOIN remote_version_aliases AS rva ON rva.alias_id=ra.alias_id "
                    "JOIN remote_versions AS rv ON rv.remote_version_id=rva.remote_version_id "
                    "WHERE ra.alias_type=? AND ra.alias_value=? AND ra.immutable=1 "
                    "AND rv.content_sha256 IS NOT NULL ORDER BY rv.remote_version_id LIMIT 1",
                    (alias.alias_type, alias.value),
                ).fetchone()
                if matched is not None:
                    return LookupDecision("skip_immutable_alias", int(matched["remote_version_id"]))
            return LookupDecision("download")

    def claim_download(
        self,
        remote_version_id: int,
        *,
        run_id: str,
        now: datetime | None = None,
        lease_seconds: int = 300,
    ) -> str | None:
        """Claim a download with a renewable lease; no transaction spans the transfer."""

        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        instant = now or _utcnow()
        token = uuid4().hex
        with self._connection() as database:
            database.execute("BEGIN IMMEDIATE")
            exists = database.execute(
                "SELECT 1 FROM remote_versions WHERE remote_version_id=?", (remote_version_id,)
            ).fetchone()
            if exists is None:
                raise ValueError(f"unknown remote version: {remote_version_id}")
            result = database.execute(
                "INSERT INTO download_claims(remote_version_id,claim_token,run_id,claimed_at,lease_expires_at,attempt_count) "
                "VALUES (?,?,?,?,?,1) ON CONFLICT(remote_version_id) DO UPDATE SET "
                "claim_token=excluded.claim_token,run_id=excluded.run_id,claimed_at=excluded.claimed_at,"
                "lease_expires_at=excluded.lease_expires_at,attempt_count=download_claims.attempt_count+1 "
                "WHERE download_claims.lease_expires_at<=excluded.claimed_at",
                (remote_version_id, token, run_id, _iso(instant), _iso(instant + timedelta(seconds=lease_seconds))),
            )
            database.commit()
            return token if result.rowcount == 1 else None

    def record_download(
        self,
        remote_version_id: int,
        claim_token: str,
        *,
        content_sha256: str,
        outcome: str,
        now: datetime | None = None,
    ) -> None:
        """Finish a claimed transfer and make its content identity durable."""

        with self._connection() as database:
            database.execute("BEGIN IMMEDIATE")
            claim = database.execute(
                "SELECT claim_token FROM download_claims WHERE remote_version_id=?", (remote_version_id,)
            ).fetchone()
            if claim is None or claim["claim_token"] != claim_token:
                raise ValueError("download claim is missing or owned by another run")
            database.execute(
                "UPDATE remote_versions SET content_sha256=?,download_outcome=?,last_seen_at=? WHERE remote_version_id=?",
                (content_sha256, outcome, _iso(now or _utcnow()), remote_version_id),
            )
            database.execute("DELETE FROM download_claims WHERE remote_version_id=?", (remote_version_id,))
            database.commit()

    def record_decision(
        self,
        remote_version_id: int,
        *,
        policy_fingerprint: str,
        outcome: str,
        now: datetime | None = None,
    ) -> None:
        with self._connection() as database:
            database.execute(
                "INSERT INTO collection_decisions(remote_version_id,policy_fingerprint,outcome,created_at) VALUES (?,?,?,?) "
                "ON CONFLICT(remote_version_id,policy_fingerprint) DO UPDATE SET outcome=excluded.outcome,created_at=excluded.created_at",
                (remote_version_id, policy_fingerprint, outcome, _iso(now or _utcnow())),
            )

    def import_content(
        self,
        identity: RemoteIdentity,
        *,
        content_sha256: str,
        local_asset_id: str | None,
        metadata: dict[str, object] | None = None,
    ) -> int:
        """Register previously downloaded content without creating a download claim."""

        version_id = self.register(identity, metadata)
        with self._connection() as database:
            database.execute("BEGIN IMMEDIATE")
            current = database.execute(
                "SELECT content_sha256 FROM remote_versions WHERE remote_version_id=?", (version_id,)
            ).fetchone()
            if current is None:
                raise ValueError(f"unknown remote version: {version_id}")
            known = current["content_sha256"]
            if known is not None and known != content_sha256:
                raise ValueError("remote version has conflicting content hashes")
            database.execute(
                "UPDATE remote_versions SET content_sha256=?,download_outcome='imported',last_seen_at=? "
                "WHERE remote_version_id=?",
                (content_sha256, _iso(_utcnow()), version_id),
            )
            if local_asset_id is not None:
                database.execute(
                    "INSERT OR IGNORE INTO remote_version_assets(remote_version_id,asset_id) VALUES (?,?)",
                    (version_id, local_asset_id),
                )
            database.commit()
        return version_id

    def link_local_asset(self, remote_version_id: int, asset_id: str) -> None:
        with self._connection() as database:
            database.execute(
                "INSERT OR IGNORE INTO remote_version_assets(remote_version_id,asset_id) VALUES (?,?)",
                (remote_version_id, asset_id),
            )

    def _version_for_identity(self, database: sqlite3.Connection, identity: RemoteIdentity) -> sqlite3.Row | None:
        return database.execute(
            "SELECT rv.* FROM remote_versions AS rv JOIN remote_assets AS ra ON ra.remote_asset_id=rv.remote_asset_id "
            "WHERE ra.source=? AND ra.provider_asset_key=? AND rv.version_key=?",
            (identity.source, identity.provider_asset_key, identity.version_key),
        ).fetchone()

    def _migrate(self) -> None:
        with self._connection() as database:
            version = int(database.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise ValueError("photo library schema is newer than this program supports")
            if version < 1:
                database.executescript(
                    """
                CREATE TABLE IF NOT EXISTS remote_assets(
                    remote_asset_id INTEGER PRIMARY KEY,
                    source TEXT NOT NULL,
                    provider_asset_key TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    UNIQUE(source, provider_asset_key)
                );
                CREATE TABLE IF NOT EXISTS remote_versions(
                    remote_version_id INTEGER PRIMARY KEY,
                    remote_asset_id INTEGER NOT NULL REFERENCES remote_assets(remote_asset_id),
                    version_key TEXT NOT NULL,
                    content_sha256 TEXT,
                    download_outcome TEXT,
                    metadata_json TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    UNIQUE(remote_asset_id, version_key)
                );
                CREATE TABLE IF NOT EXISTS remote_aliases(
                    alias_id INTEGER PRIMARY KEY,
                    alias_type TEXT NOT NULL,
                    alias_value TEXT NOT NULL,
                    immutable INTEGER NOT NULL CHECK(immutable IN (0,1)),
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS remote_alias_lookup ON remote_aliases(alias_type, alias_value, immutable);
                CREATE TABLE IF NOT EXISTS remote_version_aliases(
                    remote_version_id INTEGER NOT NULL REFERENCES remote_versions(remote_version_id),
                    alias_id INTEGER NOT NULL REFERENCES remote_aliases(alias_id),
                    PRIMARY KEY(remote_version_id, alias_id)
                );
                CREATE TABLE IF NOT EXISTS collection_decisions(
                    remote_version_id INTEGER NOT NULL REFERENCES remote_versions(remote_version_id),
                    policy_fingerprint TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(remote_version_id, policy_fingerprint)
                );
                CREATE TABLE IF NOT EXISTS download_claims(
                    remote_version_id INTEGER PRIMARY KEY REFERENCES remote_versions(remote_version_id),
                    claim_token TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    claimed_at TEXT NOT NULL,
                    lease_expires_at TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS query_scopes(
                    query_scope_id INTEGER PRIMARY KEY,
                    source TEXT NOT NULL,
                    species TEXT NOT NULL,
                    query_fingerprint TEXT NOT NULL,
                    cursor_json TEXT,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    exhausted_at TEXT,
                    last_error TEXT,
                    UNIQUE(source, species, query_fingerprint)
                );
                    """
                )
                database.execute("PRAGMA user_version=1")
                version = 1
            if version < 2:
                database.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS remote_version_assets(
                        remote_version_id INTEGER NOT NULL REFERENCES remote_versions(remote_version_id),
                        asset_id TEXT NOT NULL,
                        PRIMARY KEY(remote_version_id, asset_id)
                    );
                    """
                )
                database.execute("PRAGMA user_version=2")

    def _connection(self) -> sqlite3.Connection:
        database = sqlite3.connect(self.database_path)
        database.row_factory = sqlite3.Row
        database.execute("PRAGMA foreign_keys=ON")
        database.execute("PRAGMA busy_timeout=5000")
        return database


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()
