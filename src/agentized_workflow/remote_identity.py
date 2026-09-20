"""Stable, source-native identities for publicly hosted image candidates."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from typing import Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


@dataclass(frozen=True)
class RemoteAlias:
    """A URL or provider key that can help identify one remote image version."""

    alias_type: str
    value: str
    immutable: bool = False


@dataclass(frozen=True)
class RemoteIdentity:
    """A provider asset plus the version returned by one source API."""

    source: str
    provider_asset_key: str
    version_key: str
    aliases: tuple[RemoteAlias, ...] = ()
    observation: Mapping[str, str] = field(default_factory=dict)


def canonicalize_media_url(url: str) -> str:
    """Normalize only URL presentation details, never media-specific query fields."""

    parsed = urlsplit(str(url).strip())
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"expected a public HTTP(S) media URL, got {url!r}")
    query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)), doseq=True)
    return urlunsplit((parsed.scheme.casefold(), parsed.netloc.casefold(), parsed.path, query, ""))


def inaturalist_identity(photo_id: int | str, observation_id: int | str, original_url: str) -> RemoteIdentity:
    photo = str(photo_id).strip()
    observation = str(observation_id).strip()
    if not photo or not observation:
        raise ValueError("iNaturalist candidates require both photo_id and observation_id")
    url = canonicalize_media_url(original_url)
    return RemoteIdentity(
        source="inaturalist",
        provider_asset_key=f"photo:{photo}",
        version_key=f"photo:{photo}",
        aliases=(RemoteAlias("canonical_url", url, immutable=True),),
        observation={"observation_id": observation},
    )


def gbif_identity(dataset_key: str, occurrence_key: int | str, media_identifier: str) -> RemoteIdentity:
    dataset = str(dataset_key).strip()
    occurrence = str(occurrence_key).strip()
    url = canonicalize_media_url(media_identifier)
    if not dataset or not occurrence:
        raise ValueError("GBIF candidates require dataset_key and occurrence_key")
    media_key = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
    return RemoteIdentity(
        source="gbif",
        provider_asset_key=f"dataset:{dataset}:occurrence:{occurrence}:media:{media_key}",
        version_key=f"identifier:{media_key}",
        aliases=(RemoteAlias("canonical_url", url, immutable=False),),
        observation={"dataset_key": dataset, "occurrence_key": occurrence},
    )


def commons_identity(page_id: int | str, file_sha1: str, original_url: str) -> RemoteIdentity:
    page = str(page_id).strip()
    sha1 = str(file_sha1).strip().casefold()
    if not page or not sha1 or sha1 == "unknown":
        raise ValueError("Commons candidates require page_id and file_sha1")
    return RemoteIdentity(
        source="commons",
        provider_asset_key=f"page:{page}",
        version_key=f"sha1:{sha1}",
        aliases=(RemoteAlias("canonical_url", canonicalize_media_url(original_url), immutable=True),),
        observation={"page_id": page},
    )
