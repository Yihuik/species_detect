#!/usr/bin/env python3
"""Conservative multi-source image collector for mangrove species.

The crawler uses public APIs from iNaturalist, GBIF, and Wikimedia Commons.
Only iNaturalist research-grade records are auto-accepted. GBIF and Commons
assets are quarantined for manual review by default. The SQLite state file is
the source of truth for resume, deduplication, and review decisions.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import ipaddress
import json
import logging
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence
from urllib.parse import urlparse
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

from agentized_workflow.acquisition_ledger import AcquisitionLedger
from agentized_workflow.collector_flow import RemoteCandidate, process_remote_candidate
from agentized_workflow.photo_library import PhotoLibrary
from agentized_workflow.remote_identity import RemoteAlias, RemoteIdentity, commons_identity, gbif_identity, inaturalist_identity

try:
    import requests
    from PIL import Image
except ImportError as exc:  # pragma: no cover - friendly CLI failure
    raise SystemExit(
        "缺少依赖。请运行：python -m pip install requests Pillow"
    ) from exc


INAT_API = "https://api.inaturalist.org/v1"
GBIF_API = "https://api.gbif.org/v1"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
DEFAULT_SOURCES = ("inaturalist", "gbif", "commons")
DEFAULT_LICENSES = ("cc0", "cc-by", "cc-by-sa", "cc-by-nc", "cc-by-nc-sa", "pd")

# Bounding boxes are deliberately split by province to avoid a single broad
# rectangle that would include large areas outside southern China.
SOUTH_CHINA_BBOXES = (
    (20.20, 25.55, 109.55, 117.35),  # Guangdong
    (20.85, 26.40, 104.40, 112.10),  # Guangxi
    (23.45, 28.40, 115.80, 120.75),  # Fujian
    (18.00, 20.25, 108.60, 111.10),  # Hainan
)
SOUTH_CHINA_NAMES = (
    "guangdong", "guangxi", "fujian", "hainan",
    "hong kong", "macau", "macao",
    "广东", "广西", "福建", "海南", "香港", "澳门",
)
SOUTH_CHINA_COUNTRY_CODES = ("CN", "HK", "MO")
ARTWORK_MARKERS = (
    "illustration", "drawing", "diagram", "map", "fossil", "plate from",
    "插图", "绘图", "地图", "化石", "线描",
)
VERIFIED_GBIF_VALUES = {
    "verified", "validated", "expert verified", "expert-verified",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def canonical_name(value: object) -> str:
    """Return a conservative binomial name; reject non-binomial text."""
    text = " ".join(str(value or "").strip().split())
    match = re.match(r"^([A-Z][A-Za-zÀ-ÖØ-öø-ÿ-]+)\s+([a-z][A-Za-zÀ-ÖØ-öø-ÿ-]+)\b", text)
    return f"{match.group(1)} {match.group(2)}" if match else ""


def safe_component(value: str, fallback: str = "item") -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", value).strip(" .")
    return cleaned[:120] or fallback


def validate_remote_url(value: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        raise ValueError(f"不是有效的 HTTP(S) 图片 URL：{value!r}")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        return
    if not address.is_global:
        raise ValueError(f"图片 URL 指向非公网地址：{parsed.hostname}")


def strip_html(value: object) -> str:
    text = re.sub(r"<[^>]+>", " ", str(value or ""))
    return " ".join(html.unescape(text).split())


def extmetadata_value(metadata: dict[str, Any], key: str) -> str:
    raw = metadata.get(key)
    if isinstance(raw, dict):
        raw = raw.get("value")
    return strip_html(raw)


def normalize_license(value: object) -> str | None:
    text = str(value or "").strip().casefold().replace("_", "-")
    text = text.replace("creative commons", "cc")
    if not text:
        return None
    if "public domain" in text or text in {"pd", "pdm"}:
        return "pd"
    if "cc0" in text or "zero/1.0" in text:
        return "cc0"
    if "by-nc-sa" in text or "/by-nc-sa/" in text:
        return "cc-by-nc-sa"
    if "by-nc-nd" in text or "/by-nc-nd/" in text:
        return "cc-by-nc-nd"
    if "by-nc" in text or "/by-nc/" in text:
        return "cc-by-nc"
    if "by-sa" in text or "/by-sa/" in text:
        return "cc-by-sa"
    if "by-nd" in text or "/by-nd/" in text:
        return "cc-by-nd"
    if re.search(r"\bcc[- ]?by\b", text) or "/by/" in text:
        return "cc-by"
    return None


def within_south_china(
    latitude: object, longitude: object, state_province: object = None
) -> bool:
    try:
        lat, lon = float(latitude), float(longitude)
    except (TypeError, ValueError):
        text = str(state_province or "").casefold()
        return any(name in text for name in SOUTH_CHINA_NAMES)
    return any(
        south <= lat <= north and west <= lon <= east
        for south, north, west, east in SOUTH_CHINA_BBOXES
    )


@dataclass(frozen=True)
class SpeciesTarget:
    chinese_name: str
    scientific_names: tuple[str, ...]

    @property
    def allowed_names(self) -> set[str]:
        return {name.casefold() for name in self.scientific_names}


@dataclass
class Candidate:
    source: str
    asset_key: str
    target_chinese_name: str
    requested_scientific_name: str
    returned_scientific_name: str
    image_url: str
    page_url: str
    license_raw: str | None
    license_key: str | None
    attribution: str | None
    observer_or_creator: str | None
    observed_at: str | None
    locality: str | None
    latitude: float | None
    longitude: float | None
    quality_grade: str | None
    auto_accept: bool
    source_metadata: dict[str, Any]
    accepted_target_names: tuple[str, ...]

    @property
    def record_id(self) -> str:
        raw = f"{self.source}\0{self.asset_key}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()[:24]


def load_species_csv(path: Path) -> list[SpeciesTarget]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"chinese_name", "scientific_names"}
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ValueError("species.csv 必须包含 chinese_name,scientific_names 两列")
        targets: list[SpeciesTarget] = []
        seen_chinese: set[str] = set()
        for line_number, row in enumerate(reader, start=2):
            chinese = str(row.get("chinese_name") or "").strip()
            raw_names = str(row.get("scientific_names") or "")
            names = tuple(dict.fromkeys(canonical_name(item) for item in raw_names.split("|") if item.strip()))
            if not chinese or not names or any(not name for name in names):
                raise ValueError(f"species.csv 第 {line_number} 行名称为空或学名不是有效二名法")
            if chinese in seen_chinese:
                raise ValueError(f"species.csv 中文名重复：{chinese}")
            seen_chinese.add(chinese)
            targets.append(SpeciesTarget(chinese, names))
    if not targets:
        raise ValueError("species.csv 没有有效物种")
    return targets


class StateStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS records (
                record_id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                asset_key TEXT NOT NULL,
                chinese_name TEXT NOT NULL,
                status TEXT NOT NULL,
                scientific_name TEXT,
                page_url TEXT,
                image_url TEXT,
                local_path TEXT,
                sha256 TEXT,
                duplicate_of TEXT,
                metadata_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(source, asset_key)
            )
            """
        )
        self.connection.execute("CREATE INDEX IF NOT EXISTS idx_records_sha ON records(sha256)")
        self.connection.execute("CREATE INDEX IF NOT EXISTS idx_records_status ON records(status)")
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def already_seen(
        self, source: str, asset_key: str, retry_license_rejections: bool = False
    ) -> bool:
        row = self.connection.execute(
            "SELECT status,metadata_json FROM records WHERE source=? AND asset_key=?",
            (source, asset_key),
        ).fetchone()
        if row is None or row["status"] == "download_failed":
            return False
        if retry_license_rejections and row["status"] == "rejected":
            try:
                reason = str(json.loads(row["metadata_json"]).get("rejection_reason") or "")
            except (TypeError, ValueError, json.JSONDecodeError):
                reason = ""
            if reason.startswith("license_not_allowed:"):
                return False
        return True

    def duplicate_for_hash(self, sha256: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT record_id,local_path FROM records "
            "WHERE sha256=? AND status NOT IN ('rejected','download_failed','duplicate') "
            "ORDER BY updated_at LIMIT 1",
            (sha256,),
        ).fetchone()

    def save(
        self,
        candidate: Candidate,
        status: str,
        metadata: dict[str, Any],
        local_path: str | None = None,
        sha256: str | None = None,
        duplicate_of: str | None = None,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO records (
                record_id,source,asset_key,chinese_name,status,scientific_name,
                page_url,image_url,local_path,sha256,duplicate_of,metadata_json,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(record_id) DO UPDATE SET
                status=excluded.status, local_path=excluded.local_path,
                sha256=excluded.sha256, duplicate_of=excluded.duplicate_of,
                metadata_json=excluded.metadata_json, updated_at=excluded.updated_at
            """,
            (
                candidate.record_id, candidate.source, candidate.asset_key,
                candidate.target_chinese_name, status,
                candidate.returned_scientific_name, candidate.page_url,
                candidate.image_url, local_path, sha256, duplicate_of,
                json.dumps(metadata, ensure_ascii=False), now_iso(),
            ),
        )
        self.connection.commit()

    def pending(self) -> list[sqlite3.Row]:
        return list(self.connection.execute(
            "SELECT * FROM records WHERE status='pending_review' ORDER BY chinese_name,source,record_id"
        ))

    def get(self, record_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM records WHERE record_id=?", (record_id,)
        ).fetchone()

    def update_review(
        self, record_id: str, status: str, local_path: str, metadata: dict[str, Any]
    ) -> None:
        self.connection.execute(
            "UPDATE records SET status=?,local_path=?,metadata_json=?,updated_at=? WHERE record_id=?",
            (status, local_path, json.dumps(metadata, ensure_ascii=False), now_iso(), record_id),
        )
        self.connection.commit()

    def counts(self) -> list[sqlite3.Row]:
        return list(self.connection.execute(
            "SELECT chinese_name,source,status,COUNT(*) AS count FROM records "
            "GROUP BY chinese_name,source,status ORDER BY chinese_name,source,status"
        ))

    def usable_count(self, chinese_name: str) -> int:
        row = self.connection.execute(
            "SELECT COUNT(*) AS count FROM records "
            "WHERE chinese_name=? AND status IN ('accepted','pending_review')",
            (chinese_name,),
        ).fetchone()
        return int(row["count"] if row is not None else 0)


class ApiClient:
    def __init__(
        self, delay_seconds: float, timeout_seconds: float, retries: int, contact: str
    ) -> None:
        self.delay_seconds = delay_seconds
        self.timeout_seconds = timeout_seconds
        self.retries = retries
        self.last_request = 0.0
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": f"mangrove-species-research-collector/1.0 ({contact})",
            "Accept": "application/json,image/*;q=0.9,*/*;q=0.1",
        })

    def _wait(self) -> None:
        remaining = self.delay_seconds - (time.monotonic() - self.last_request)
        if remaining > 0:
            time.sleep(remaining)

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            self._wait()
            try:
                response = self.session.get(url, timeout=self.timeout_seconds, **kwargs)
                self.last_request = time.monotonic()
                if response.status_code == 429:
                    retry_after = min(120.0, float(response.headers.get("Retry-After", 5)))
                    time.sleep(retry_after)
                    response.raise_for_status()
                response.raise_for_status()
                return response
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                if attempt < self.retries:
                    time.sleep(min(30.0, 2.0 ** attempt))
        assert last_error is not None
        raise last_error

    def json(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        response = self.get(url, params=params)
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError(f"API 返回顶层不是 JSON 对象：{response.url}")
        return payload


def float_or_none(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def inat_photo_original(url: str) -> str:
    return re.sub(r"/(square|small|medium|large)\.", "/original.", url, count=1)


def fetch_inaturalist(
    client: ApiClient,
    target: SpeciesTarget,
    maximum: int,
    rejected_log: Path,
    geography: str = "south-china",
) -> Iterator[Candidate]:
    yielded = 0
    seen_assets: set[str] = set()
    for scientific_name in target.scientific_names:
        regions: Sequence[tuple[float, float, float, float] | None] = (
            SOUTH_CHINA_BBOXES if geography == "south-china" else (None,)
        )
        for region in regions:
            page = 1
            while yielded < maximum:
                params: dict[str, Any] = {
                    "taxon_name": scientific_name,
                    "quality_grade": "research",
                    "photos": "true",
                    "geo": "true",
                    "verifiable": "true",
                    "captive": "false",
                    "identifications": "most_agree",
                    "per_page": 200, "page": page,
                    "order_by": "id", "order": "desc",
                }
                if region is not None:
                    south, north, west, east = region
                    params.update({
                        "nelat": north, "nelng": east,
                        "swlat": south, "swlng": west,
                    })
                payload = client.json(
                    f"{INAT_API}/observations",
                    params,
                )
                results = payload.get("results")
                if not isinstance(results, list) or not results:
                    break
                for observation in results:
                    if not isinstance(observation, dict):
                        continue
                    taxon = observation.get("taxon") or {}
                    returned = canonical_name(taxon.get("name")) if isinstance(taxon, dict) else ""
                    quality = str(observation.get("quality_grade") or "")
                    geojson = observation.get("geojson") or {}
                    coordinates = geojson.get("coordinates") if isinstance(geojson, dict) else None
                    lon = float_or_none(coordinates[0]) if isinstance(coordinates, list) and len(coordinates) >= 2 else None
                    lat = float_or_none(coordinates[1]) if isinstance(coordinates, list) and len(coordinates) >= 2 else None
                    reason = None
                    if returned.casefold() not in target.allowed_names:
                        reason = f"returned_taxon_mismatch:{returned or 'missing'}"
                    elif quality != "research":
                        reason = f"quality_grade:{quality or 'missing'}"
                    elif geography == "south-china" and not within_south_china(
                        lat, lon, observation.get("place_guess")
                    ):
                        reason = "outside_south_china_or_location_missing"
                    if reason:
                        append_jsonl(rejected_log, {
                            "source": "inaturalist", "reason": reason,
                            "target": target.chinese_name, "observation_id": observation.get("id"),
                        })
                        continue
                    photos = observation.get("photos")
                    if not isinstance(photos, list):
                        continue
                    for photo in photos:
                        if yielded >= maximum or not isinstance(photo, dict):
                            break
                        photo_url = str(photo.get("url") or "")
                        photo_id = str(photo.get("id") or hashlib.sha256(photo_url.encode()).hexdigest()[:12])
                        asset_key = f"observation:{observation.get('id')}:photo:{photo_id}"
                        if not photo_url or asset_key in seen_assets:
                            continue
                        seen_assets.add(asset_key)
                        user = observation.get("user") or {}
                        creator = user.get("login") if isinstance(user, dict) else None
                        license_raw = str(photo.get("license_code") or "") or None
                        yielded += 1
                        yield Candidate(
                            "inaturalist", asset_key, target.chinese_name,
                            scientific_name, returned, inat_photo_original(photo_url),
                            f"https://www.inaturalist.org/observations/{observation.get('id')}",
                            license_raw, normalize_license(license_raw),
                            str(photo.get("attribution") or "") or None,
                            str(creator or "") or None,
                            str(observation.get("observed_on") or "") or None,
                            str(observation.get("place_guess") or "") or None,
                            lat, lon, quality, True,
                            {
                                "observation_id": observation.get("id"),
                                "photo_id": photo.get("id"),
                                "taxon_id": taxon.get("id") if isinstance(taxon, dict) else None,
                                "positional_accuracy": observation.get("positional_accuracy"),
                                "coordinates_obscured": observation.get("obscured"),
                            },
                            target.scientific_names,
                        )
                page += 1
                if page > int(payload.get("total_results", 0) or 0) / 200 + 1:
                    break
            if yielded >= maximum:
                return


def resolve_gbif_key(client: ApiClient, target: SpeciesTarget, name: str) -> tuple[int, str] | None:
    payload = client.json(
        f"{GBIF_API}/species/match",
        {"name": name, "strict": "true", "verbose": "true"},
    )
    returned = canonical_name(payload.get("canonicalName") or payload.get("scientificName"))
    if str(payload.get("matchType") or "").upper() != "EXACT":
        return None
    if returned.casefold() not in target.allowed_names:
        return None
    key = payload.get("acceptedUsageKey") or payload.get("usageKey") or payload.get("speciesKey")
    return (int(key), returned) if isinstance(key, int) else None


def fetch_gbif(
    client: ApiClient,
    target: SpeciesTarget,
    maximum: int,
    rejected_log: Path,
    accept_verified: bool,
    geography: str = "south-china",
) -> Iterator[Candidate]:
    yielded = 0
    seen_assets: set[str] = set()
    for scientific_name in target.scientific_names:
        resolved = resolve_gbif_key(client, target, scientific_name)
        if resolved is None:
            append_jsonl(rejected_log, {
                "source": "gbif", "reason": "name_match_not_exact_or_unapproved_synonym",
                "target": target.chinese_name, "query": scientific_name,
            })
            continue
        taxon_key, resolved_name = resolved
        country_codes: Sequence[str | None] = (
            SOUTH_CHINA_COUNTRY_CODES if geography == "south-china" else (None,)
        )
        for country_code in country_codes:
            offset = 0
            while yielded < maximum:
                params: dict[str, Any] = {
                    "taxon_key": taxon_key,
                    "media_type": "StillImage", "occurrence_status": "PRESENT",
                    "limit": min(300, maximum * 3), "offset": offset,
                }
                if country_code is not None:
                    params["country"] = country_code
                payload = client.json(
                    f"{GBIF_API}/occurrence/search",
                    params,
                )
                results = payload.get("results")
                if not isinstance(results, list) or not results:
                    break
                for occurrence in results:
                    if yielded >= maximum or not isinstance(occurrence, dict):
                        break
                    returned = canonical_name(
                        occurrence.get("species")
                        or occurrence.get("acceptedScientificName")
                        or occurrence.get("scientificName")
                    )
                    lat = float_or_none(occurrence.get("decimalLatitude"))
                    lon = float_or_none(occurrence.get("decimalLongitude"))
                    reason = None
                    if returned.casefold() not in target.allowed_names:
                        reason = f"returned_taxon_mismatch:{returned or 'missing'}"
                    elif geography == "south-china" and str(
                        occurrence.get("countryCode") or ""
                    ).upper() not in SOUTH_CHINA_COUNTRY_CODES:
                        reason = "country_not_south_china"
                    elif geography == "south-china" and not within_south_china(
                        lat, lon, occurrence.get("stateProvince")
                    ):
                        reason = "outside_south_china_or_location_missing"
                    if reason:
                        append_jsonl(rejected_log, {
                            "source": "gbif", "reason": reason,
                            "target": target.chinese_name, "gbif_key": occurrence.get("key"),
                        })
                        continue
                    verification = str(occurrence.get("identificationVerificationStatus") or "").strip()
                    verified = verification.casefold() in VERIFIED_GBIF_VALUES
                    media_items = occurrence.get("media")
                    if not isinstance(media_items, list):
                        continue
                    for media_index, media in enumerate(media_items):
                        if yielded >= maximum or not isinstance(media, dict):
                            break
                        image_url = str(media.get("identifier") or "")
                        if not image_url.startswith(("https://", "http://")):
                            continue
                        media_type = str(media.get("type") or "").strip().casefold()
                        if media_type and media_type not in {"stillimage", "still image"}:
                            continue
                        media_id = str(media.get("identifier") or media_index)
                        asset_key = f"occurrence:{occurrence.get('key')}:media:{hashlib.sha256(media_id.encode()).hexdigest()[:16]}"
                        if asset_key in seen_assets:
                            continue
                        seen_assets.add(asset_key)
                        license_raw = str(media.get("license") or occurrence.get("license") or "") or None
                        yielded += 1
                        yield Candidate(
                            "gbif", asset_key, target.chinese_name,
                            scientific_name, returned or resolved_name, image_url,
                            str(media.get("references") or occurrence.get("references") or f"https://www.gbif.org/occurrence/{occurrence.get('key')}"),
                            license_raw, normalize_license(license_raw),
                            str(media.get("rightsHolder") or media.get("creator") or "") or None,
                            str(occurrence.get("recordedBy") or media.get("creator") or "") or None,
                            str(occurrence.get("eventDate") or "") or None,
                            str(occurrence.get("locality") or occurrence.get("stateProvince") or "") or None,
                            lat, lon, verification or None,
                            bool(accept_verified and verified),
                            {
                                "gbif_key": occurrence.get("key"),
                                "dataset_key": occurrence.get("datasetKey"),
                                "dataset_title": occurrence.get("datasetTitle"),
                                "basis_of_record": occurrence.get("basisOfRecord"),
                                "identification_verification_status": verification or None,
                                "gbif_taxon_key": taxon_key,
                                "gbif_country_query": country_code,
                                "geography_mode": geography,
                            },
                            target.scientific_names,
                        )
                offset += len(results)
                if payload.get("endOfRecords") is True:
                    break
        if yielded >= maximum:
            return


def fetch_commons(
    client: ApiClient, target: SpeciesTarget, maximum: int, rejected_log: Path
) -> Iterator[Candidate]:
    yielded = 0
    seen_pages: set[int] = set()
    for scientific_name in target.scientific_names:
        continuation: dict[str, Any] = {}
        while yielded < maximum:
            params: dict[str, Any] = {
                "action": "query", "format": "json", "formatversion": 2,
                "generator": "categorymembers",
                "gcmtitle": f"Category:{scientific_name}", "gcmtype": "file",
                "gcmlimit": 50, "prop": "imageinfo",
                "iiprop": "url|mime|mediatype|sha1|extmetadata",
                # Keep this below the size of typical Commons originals.  When the
                # requested width is larger than the original, MediaWiki returns an
                # ``thumbnail_unscaled`` original-file URL, which the upload host
                # may reject with HTTP 429 during dataset collection.
                "iiurlwidth": 640,
                "iiextmetadatalanguage": "en",
            }
            params.update(continuation)
            payload = client.json(COMMONS_API, params)
            pages = (payload.get("query") or {}).get("pages")
            if not isinstance(pages, list) or not pages:
                break
            for page in pages:
                if yielded >= maximum or not isinstance(page, dict):
                    break
                page_id = page.get("pageid")
                if not isinstance(page_id, int) or page_id in seen_pages:
                    continue
                seen_pages.add(page_id)
                info_items = page.get("imageinfo")
                if not isinstance(info_items, list) or not info_items:
                    continue
                info = info_items[0]
                if not isinstance(info, dict):
                    continue
                metadata = info.get("extmetadata") or {}
                if not isinstance(metadata, dict):
                    continue
                description = " ".join((
                    str(page.get("title") or ""),
                    extmetadata_value(metadata, "ObjectName"),
                    extmetadata_value(metadata, "ImageDescription"),
                    extmetadata_value(metadata, "Categories"),
                ))
                lowered = description.casefold()
                mime = str(info.get("mime") or "").casefold()
                reason = None
                if scientific_name.casefold() not in lowered:
                    reason = "scientific_name_missing_from_file_metadata"
                elif any(marker in lowered for marker in ARTWORK_MARKERS):
                    reason = "likely_artwork_or_non_photo"
                elif mime not in {"image/jpeg", "image/png", "image/webp", "image/tiff"}:
                    reason = f"unsupported_mime:{mime or 'missing'}"
                if reason:
                    append_jsonl(rejected_log, {
                        "source": "commons", "reason": reason,
                        "target": target.chinese_name, "page_id": page_id,
                    })
                    continue
                image_url = str(info.get("thumburl") or info.get("url") or "")
                if not image_url:
                    continue
                license_raw = extmetadata_value(metadata, "LicenseShortName") or None
                yielded += 1
                yield Candidate(
                    "commons", f"page:{page_id}:sha1:{info.get('sha1') or 'unknown'}",
                    target.chinese_name, scientific_name, scientific_name,
                    image_url, str(info.get("descriptionurl") or ""),
                    license_raw, normalize_license(license_raw),
                    extmetadata_value(metadata, "Credit") or None,
                    extmetadata_value(metadata, "Artist") or None,
                    extmetadata_value(metadata, "DateTimeOriginal") or None,
                    extmetadata_value(metadata, "Location") or None,
                    None, None, None, False,
                    {
                        "commons_page_id": page_id,
                        "commons_title": page.get("title"),
                        "license_url": extmetadata_value(metadata, "LicenseUrl") or None,
                        "attribution_required": extmetadata_value(metadata, "AttributionRequired") or None,
                        "mime": mime,
                        "original_image_url": info.get("url"),
                        "download_variant": "thumbnail_640" if info.get("thumburl") else "original",
                    },
                    target.scientific_names,
                )
            next_values = payload.get("continue")
            if not isinstance(next_values, dict):
                break
            continuation = next_values
        if yielded >= maximum:
            return


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def download_and_validate(
    client: ApiClient, url: str, temp_dir: Path, max_bytes: int, min_dimension: int
) -> tuple[Path, str, str, int, int]:
    validate_remote_url(url)
    response = client.get(url, stream=True, headers={"Accept": "image/*"})
    validate_remote_url(str(response.url))
    content_length = int(response.headers.get("Content-Length", 0) or 0)
    if content_length > max_bytes:
        raise ValueError(f"图片超过大小上限：{content_length} > {max_bytes}")
    digest = hashlib.sha256()
    total = 0
    with tempfile.NamedTemporaryFile(dir=temp_dir, suffix=".download", delete=False) as handle:
        temp_path = Path(handle.name)
        try:
            for chunk in response.iter_content(128 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError(f"图片下载超过大小上限：{total} > {max_bytes}")
                digest.update(chunk)
                handle.write(chunk)
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise
    try:
        with Image.open(temp_path) as image:
            width, height = image.size
            if width * height > 100_000_000:
                raise ValueError(f"图片像素总量过大：{width}x{height}")
            image.load()
            image_format = str(image.format or "").upper()
        if min(width, height) < min_dimension:
            raise ValueError(f"图片过小：{width}x{height}，短边要求 >= {min_dimension}")
        extensions = {
            "JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp",
            "TIFF": ".tif", "BMP": ".bmp",
        }
        extension = extensions.get(image_format)
        if extension is None:
            raise ValueError(f"不支持的图片格式：{image_format or 'unknown'}")
        return temp_path, digest.hexdigest(), extension, width, height
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def candidate_metadata(candidate: Candidate, status: str) -> dict[str, Any]:
    payload = asdict(candidate)
    payload.pop("auto_accept", None)
    payload.update({
        "record_id": candidate.record_id,
        "review_status": status,
        "collected_at": now_iso(),
    })
    return payload


def process_candidate(
    client: ApiClient,
    store: StateStore,
    candidate: Candidate,
    output_root: Path,
    allowed_licenses: set[str],
    max_bytes: int,
    min_dimension: int,
    rejected_log: Path,
    allow_any_license: bool = False,
) -> str:
    if store.already_seen(
        candidate.source, candidate.asset_key,
        retry_license_rejections=allow_any_license,
    ):
        return "skipped_seen"
    if candidate.returned_scientific_name.casefold() not in {
        name.casefold() for name in candidate.accepted_target_names
    }:
        # Fetchers already compare against the complete target allow-list. This
        # final guard prevents a future adapter from bypassing exact matching.
        metadata = candidate_metadata(candidate, "rejected")
        metadata["rejection_reason"] = "final_exact_name_guard_failed"
        store.save(candidate, "rejected", metadata)
        append_jsonl(rejected_log, metadata)
        return "rejected_taxon"
    license_allowed = candidate.license_key in allowed_licenses
    if not license_allowed and not allow_any_license:
        metadata = candidate_metadata(candidate, "rejected")
        metadata["rejection_reason"] = f"license_not_allowed:{candidate.license_raw or 'missing'}"
        store.save(candidate, "rejected", metadata)
        append_jsonl(rejected_log, metadata)
        return "rejected_license"

    temp_dir = output_root / ".tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    try:
        temp_path, sha256, extension, width, height = download_and_validate(
            client, candidate.image_url, temp_dir, max_bytes, min_dimension
        )
    except Exception as exc:
        metadata = candidate_metadata(candidate, "download_failed")
        metadata["failure"] = f"{type(exc).__name__}: {exc}"
        store.save(candidate, "download_failed", metadata)
        logging.warning("下载失败 %s: %s", candidate.page_url, exc)
        return "download_failed"

    duplicate = store.duplicate_for_hash(sha256)
    if duplicate is not None:
        temp_path.unlink(missing_ok=True)
        metadata = candidate_metadata(candidate, "duplicate")
        metadata["duplicate_of"] = duplicate["record_id"]
        store.save(
            candidate, "duplicate", metadata, sha256=sha256,
            duplicate_of=str(duplicate["record_id"]),
        )
        append_jsonl(output_root / "logs" / "duplicates.jsonl", metadata)
        return "duplicate"

    # A relaxed-license run may retain the file for research review, but an
    # unknown or restrictive license must never inherit an automatic accept.
    status = "accepted" if candidate.auto_accept and license_allowed else "pending_review"
    species_dir = output_root / safe_component(candidate.target_chinese_name)
    if status == "accepted":
        destination_dir = species_dir / "accepted" / "images"
    else:
        destination_dir = species_dir / "pending_review" / candidate.source / "images"
    destination_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{candidate.source}_{candidate.record_id}"
    image_path = destination_dir / f"{stem}{extension}"
    sidecar_path = destination_dir / f"{stem}.json"
    os.replace(temp_path, image_path)
    metadata = candidate_metadata(candidate, status)
    metadata.update({
        "sha256": sha256, "width": width, "height": height,
        "local_image": image_path.relative_to(output_root).as_posix(),
        "license_requires_review": not license_allowed,
    })
    sidecar_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    store.save(
        candidate, status, metadata,
        local_path=image_path.relative_to(output_root).as_posix(), sha256=sha256,
    )
    append_jsonl(output_root / "metadata.jsonl", metadata)
    return status


def ledger_identity(candidate: Candidate) -> RemoteIdentity:
    metadata = candidate.source_metadata
    if candidate.source == "inaturalist" and metadata.get("photo_id") is not None and metadata.get("observation_id") is not None:
        return inaturalist_identity(metadata["photo_id"], metadata["observation_id"], candidate.image_url)
    if candidate.source == "gbif" and metadata.get("dataset_key") and metadata.get("gbif_key") is not None:
        return gbif_identity(str(metadata["dataset_key"]), metadata["gbif_key"], candidate.image_url)
    if candidate.source == "commons":
        match = re.search(r"page:(\d+):sha1:([^:]+)$", candidate.asset_key)
        original = str(metadata.get("original_image_url") or candidate.image_url)
        if match and original:
            return commons_identity(match.group(1), match.group(2), original)
    return RemoteIdentity(candidate.source, candidate.asset_key, f"legacy:{candidate.record_id}", (RemoteAlias("canonical_url", candidate.image_url),))


def process_candidate_with_ledger(
    client: ApiClient, candidate: Candidate, library: PhotoLibrary, ledger: AcquisitionLedger,
    policy_fingerprint: str, run_id: str, max_bytes: int, min_dimension: int,
    allowed_licenses: set[str], allow_any_license: bool,
) -> str:
    identity = ledger_identity(candidate)
    version_id = ledger.register(identity, candidate_metadata(candidate, "discovered"))
    names = {name.casefold() for name in candidate.accepted_target_names}
    if candidate.returned_scientific_name.casefold() not in names:
        ledger.record_decision(version_id, policy_fingerprint=policy_fingerprint, outcome="rejected_taxon")
        return "rejected_taxon"
    if candidate.license_key not in allowed_licenses and not allow_any_license:
        ledger.record_decision(version_id, policy_fingerprint=policy_fingerprint, outcome="rejected_license")
        return "rejected_license"
    staged: list[Path] = []
    def download() -> Path:
        (library.photos_root / ".staging").mkdir(parents=True, exist_ok=True)
        temp, _sha, _ext, _width, _height = download_and_validate(
            client, candidate.image_url, library.photos_root / ".staging", max_bytes, min_dimension
        )
        staged.append(temp)
        return temp
    try:
        return process_remote_candidate(
            RemoteCandidate(identity, candidate.target_chinese_name), ledger, library,
            policy_fingerprint, run_id, download,
        ).outcome
    finally:
        for path in staged:
            path.unlink(missing_ok=True)


def crawl(args: argparse.Namespace) -> int:
    targets = load_species_csv(args.species_csv)
    if args.include_species:
        requested = set(args.include_species)
        targets = [target for target in targets if target.chinese_name in requested]
        missing = requested - {target.chinese_name for target in targets}
        if missing:
            raise ValueError(
                f"--include-species 不在 species.csv 中：{', '.join(sorted(missing))}"
            )
    selected_sources = tuple(dict.fromkeys(args.sources))
    invalid = set(selected_sources) - set(DEFAULT_SOURCES)
    if invalid:
        raise ValueError(f"未知数据源：{', '.join(sorted(invalid))}")
    allowed_licenses = {item.casefold() for item in args.allow_license}
    output_root: Path = args.output_root
    output_root.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(output_root / "collector.log", encoding="utf-8"),
        ],
    )
    client = ApiClient(args.delay, args.timeout, args.retries, args.contact)
    ledger_mode = args.photos_root is not None
    if ledger_mode and args.catalog_root is None:
        raise ValueError("--photos-root requires --catalog-root")
    library = PhotoLibrary(args.catalog_root, args.photos_root) if ledger_mode else None
    ledger = AcquisitionLedger(args.photos_root / "photo_library.sqlite3") if ledger_mode else None
    store = None if ledger_mode else StateStore(output_root / "collector_state.sqlite3")
    policy_fingerprint = hashlib.sha256(
        json.dumps({"licenses": sorted(allowed_licenses), "allow_any": args.allow_any_license}, sort_keys=True).encode()
    ).hexdigest()
    rejected_log = output_root / "logs" / "rejected.jsonl"
    counters: dict[str, int] = {}
    try:
        for target in targets:
            logging.info("物种：%s (%s)", target.chinese_name, " | ".join(target.scientific_names))
            usable_total = store.usable_count(target.chinese_name) if store is not None else 0
            target_reached = (
                args.target_total_per_species is not None
                and usable_total >= args.target_total_per_species
            )
            if target_reached:
                logging.info(
                    "%s 已有 %d 张，达到目标 %d 张，跳过",
                    target.chinese_name, usable_total, args.target_total_per_species,
                )
                continue
            for source in selected_sources:
                logging.info("数据源：%s", source)
                if source == "inaturalist":
                    candidates = fetch_inaturalist(
                        client, target, args.max_per_source, rejected_log,
                        args.geography,
                    )
                elif source == "gbif":
                    candidates = fetch_gbif(
                        client, target, args.max_per_source, rejected_log,
                        args.accept_gbif_verified, args.geography,
                    )
                else:
                    candidates = fetch_commons(client, target, args.max_per_source, rejected_log)
                try:
                    for candidate in candidates:
                        outcome = (
                            process_candidate_with_ledger(
                                client, candidate, library, ledger, policy_fingerprint, uuid4().hex,
                                args.max_image_mb * 1024 * 1024, args.min_dimension, allowed_licenses, args.allow_any_license,
                            ) if ledger_mode else process_candidate(
                                client, store, candidate, output_root, allowed_licenses,
                                args.max_image_mb * 1024 * 1024, args.min_dimension,
                                rejected_log, args.allow_any_license,
                            )
                        )
                        counters[outcome] = counters.get(outcome, 0) + 1
                        if outcome in {"accepted", "pending_review"}:
                            usable_total += 1
                        if (
                            args.target_total_per_species is not None
                            and usable_total >= args.target_total_per_species
                        ):
                            logging.info(
                                "%s 已补齐至 %d 张，停止该物种后续抓取",
                                target.chinese_name, usable_total,
                            )
                            target_reached = True
                            break
                except Exception as exc:
                    logging.exception("%s/%s 抓取失败：%s", target.chinese_name, source, exc)
                    counters["source_failed"] = counters.get("source_failed", 0) + 1
                if target_reached:
                    break
        logging.info("本次结果：%s", json.dumps(counters, ensure_ascii=False, sort_keys=True))
        return 1 if counters.get("source_failed") else 0
    finally:
        if store is not None:
            store.close()


def export_review(args: argparse.Namespace) -> int:
    root: Path = args.output_root
    store = StateStore(root / "collector_state.sqlite3")
    try:
        rows = store.pending()
        args.review_csv.parent.mkdir(parents=True, exist_ok=True)
        with args.review_csv.open("w", encoding="utf-8-sig", newline="") as handle:
            fields = [
                "record_id", "chinese_name", "scientific_name", "source",
                "local_path", "page_url", "license", "decision", "reviewer", "notes",
            ]
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for row in rows:
                metadata = json.loads(row["metadata_json"])
                writer.writerow({
                    "record_id": row["record_id"], "chinese_name": row["chinese_name"],
                    "scientific_name": row["scientific_name"], "source": row["source"],
                    "local_path": row["local_path"], "page_url": row["page_url"],
                    "license": metadata.get("license_raw"), "decision": "",
                    "reviewer": "", "notes": "",
                })
        print(f"已导出 {len(rows)} 条待审核记录：{args.review_csv}")
        return 0
    finally:
        store.close()


def apply_review(args: argparse.Namespace) -> int:
    root: Path = args.output_root
    store = StateStore(root / "collector_state.sqlite3")
    applied = 0
    try:
        with args.review_csv.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            required = {"record_id", "decision", "reviewer", "notes"}
            if not reader.fieldnames or not required.issubset(reader.fieldnames):
                raise ValueError("审核 CSV 必须包含 record_id,decision,reviewer,notes")
            for line_number, decision_row in enumerate(reader, start=2):
                decision = str(decision_row.get("decision") or "").strip().casefold()
                if not decision:
                    continue
                if decision not in {"accept", "reject"}:
                    raise ValueError(f"审核 CSV 第 {line_number} 行 decision 必须是 accept/reject")
                reviewer = str(decision_row.get("reviewer") or "").strip()
                if not reviewer:
                    raise ValueError(f"审核 CSV 第 {line_number} 行缺少 reviewer")
                record_id = str(decision_row.get("record_id") or "").strip()
                row = store.get(record_id)
                if row is None:
                    raise ValueError(f"第 {line_number} 行记录不存在：{record_id}")
                desired_status = "accepted" if decision == "accept" else "rejected"
                if row["status"] == desired_status:
                    continue
                if row["status"] != "pending_review":
                    raise ValueError(
                        f"第 {line_number} 行当前状态不能审核：{record_id}={row['status']}"
                    )
                source_path = root / str(row["local_path"])
                species_dir = root / safe_component(str(row["chinese_name"]))
                status = desired_status
                destination_dir = (
                    species_dir / "accepted" / "images"
                    if status == "accepted"
                    else species_dir / "rejected" / str(row["source"]) / "images"
                )
                destination_dir.mkdir(parents=True, exist_ok=True)
                destination = destination_dir / source_path.name
                sidecar_source = source_path.with_name(source_path.stem + ".json")
                sidecar_destination = destination_dir / (source_path.stem + ".json")
                if source_path.is_file():
                    shutil.move(str(source_path), str(destination))
                elif not destination.is_file():
                    raise ValueError(
                        f"待审核图片及预期目标文件均不存在：{source_path}；{destination}"
                    )
                metadata = json.loads(row["metadata_json"])
                metadata["review_status"] = status
                metadata["manual_review"] = {
                    "reviewer": reviewer,
                    "reviewed_at": now_iso(),
                    "notes": str(decision_row.get("notes") or "").strip(),
                }
                metadata["local_image"] = destination.relative_to(root).as_posix()
                if sidecar_source.exists():
                    shutil.move(str(sidecar_source), str(sidecar_destination))
                sidecar_destination.write_text(
                    json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                store.update_review(
                    record_id, status, destination.relative_to(root).as_posix(), metadata
                )
                append_jsonl(root / "review_events.jsonl", {
                    "record_id": record_id, "decision": decision,
                    "reviewer": reviewer, "notes": metadata["manual_review"]["notes"],
                    "reviewed_at": metadata["manual_review"]["reviewed_at"],
                })
                applied += 1
        print(f"已应用 {applied} 条审核决定")
        return 0
    finally:
        store.close()


def report(args: argparse.Namespace) -> int:
    store = StateStore(args.output_root / "collector_state.sqlite3")
    try:
        rows = store.counts()
        if not rows:
            print("当前没有采集记录")
            return 0
        print("中文名\t来源\t状态\t数量")
        for row in rows:
            print(f"{row['chinese_name']}\t{row['source']}\t{row['status']}\t{row['count']}")
        return 0
    finally:
        store.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="低并发、多数据源、严格物种核验的红树林物种图片采集器"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    crawl_parser = subparsers.add_parser("crawl", help="调用公开 API 抓取并下载图片")
    crawl_parser.add_argument("--species-csv", type=Path, required=True)
    crawl_parser.add_argument(
        "--include-species",
        action="append",
        help="只采集指定中文名；可重复传入，默认采集 CSV 中全部物种",
    )
    crawl_parser.add_argument("--output-root", type=Path, default=Path("dataset/mangrove_species"))
    crawl_parser.add_argument("--catalog-root", type=Path, help="账本模式所用可信物种目录")
    crawl_parser.add_argument("--photos-root", type=Path, help="启用账本优先入库的照片库目录")
    crawl_parser.add_argument(
        "--sources", nargs="+", default=list(DEFAULT_SOURCES),
        choices=DEFAULT_SOURCES,
    )
    crawl_parser.add_argument("--max-per-source", type=int, default=100)
    crawl_parser.add_argument(
        "--target-total-per-species", type=int,
        help="物种的 accepted+pending_review 总数达到该值后停止",
    )
    crawl_parser.add_argument(
        "--geography", choices=("south-china", "global"), default="south-china",
        help="地理范围；默认仅华南，global 不执行地理过滤",
    )
    crawl_parser.add_argument("--delay", type=float, default=1.2)
    crawl_parser.add_argument("--timeout", type=float, default=45.0)
    crawl_parser.add_argument("--retries", type=int, default=3)
    crawl_parser.add_argument("--contact", required=True, help="User-Agent 中的联系邮箱或项目 URL")
    crawl_parser.add_argument(
        "--allow-license", nargs="+", default=list(DEFAULT_LICENSES),
        help="允许下载的许可证规范名",
    )
    crawl_parser.add_argument(
        "--allow-any-license", action="store_true",
        help="下载许可证未知或受限的图片，但强制放入 pending_review",
    )
    crawl_parser.add_argument("--min-dimension", type=int, default=300)
    crawl_parser.add_argument("--max-image-mb", type=int, default=25)
    crawl_parser.add_argument(
        "--accept-gbif-verified", action="store_true",
        help="仅将带明确 verified/validated 状态的 GBIF 记录自动接收；默认全部待人工审核",
    )
    crawl_parser.set_defaults(func=crawl)

    export_parser = subparsers.add_parser("export-review", help="导出人工审核 CSV")
    export_parser.add_argument("--output-root", type=Path, default=Path("dataset/mangrove_species"))
    export_parser.add_argument("--review-csv", type=Path, default=Path("review_queue.csv"))
    export_parser.set_defaults(func=export_review)

    apply_parser = subparsers.add_parser("apply-review", help="应用人工审核 CSV")
    apply_parser.add_argument("--output-root", type=Path, default=Path("dataset/mangrove_species"))
    apply_parser.add_argument("--review-csv", type=Path, required=True)
    apply_parser.set_defaults(func=apply_review)

    report_parser = subparsers.add_parser("report", help="按物种/来源/状态统计")
    report_parser.add_argument("--output-root", type=Path, default=Path("dataset/mangrove_species"))
    report_parser.set_defaults(func=report)
    return parser


def validate_args(args: argparse.Namespace) -> None:
    if args.command == "crawl":
        if args.max_per_source < 1:
            raise ValueError("--max-per-source 必须大于 0")
        if (
            args.target_total_per_species is not None
            and args.target_total_per_species < 1
        ):
            raise ValueError("--target-total-per-species 必须大于 0")
        if args.delay < 0.5:
            raise ValueError("--delay 不能小于 0.5 秒")
        if args.timeout <= 0 or args.retries < 0:
            raise ValueError("timeout 必须大于 0，retries 不能小于 0")
        if args.min_dimension < 32 or args.max_image_mb < 1:
            raise ValueError("min-dimension 不能小于 32，max-image-mb 必须大于 0")
        if not str(args.contact).strip():
            raise ValueError("--contact 不能为空")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        validate_args(args)
        return int(args.func(args))
    except (OSError, ValueError, sqlite3.Error, requests.RequestException) as exc:
        print(f"错误：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
