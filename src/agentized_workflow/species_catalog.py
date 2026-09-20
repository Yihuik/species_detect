"""Build the trusted species catalog from the user-maintained species list."""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path


def _read_species(path: Path) -> list[str]:
    species = [
        line.strip()
        for line in path.read_text(encoding="utf-8-sig").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not species:
        raise ValueError("species file has no species")
    if len(species) != len(set(species)):
        raise ValueError("species file contains duplicates")
    return species


def sync_catalog(species_file: Path, taxonomy_file: Path, catalog_root: Path) -> Path:
    """Create the only trusted name catalog used by the photo library.

    The taxonomy document maps exact Chinese names to one or more confirmed
    scientific names.  It is deliberately not a fuzzy name resolver.
    """

    species = _read_species(Path(species_file))
    raw_taxonomy = json.loads(Path(taxonomy_file).read_text(encoding="utf-8-sig"))
    if not isinstance(raw_taxonomy, dict):
        raise ValueError("taxonomy must be a JSON object")

    rows: list[dict[str, str]] = []
    for name in species:
        item = raw_taxonomy.get(name)
        names = item.get("scientific_names") if isinstance(item, dict) else None
        if not isinstance(names, list) or not names or not all(
            isinstance(value, str) and value.strip() for value in names
        ):
            raise ValueError(f"missing taxonomy for species: {name}")
        rows.append(
            {
                "species": name,
                "scientific_names": "|".join(dict.fromkeys(names)),
                "status": "active",
            }
        )

    root = Path(catalog_root)
    root.mkdir(parents=True, exist_ok=True)
    state_path = root / "collection_state.json"
    previous = _read_state(state_path)
    state = {
        name: previous.get(name, {"status": "pending"})
        for name in species
    }
    metadata = root / "metadata.csv"
    with metadata.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=["species", "scientific_names", "status"]
        )
        writer.writeheader()
        writer.writerows(rows)
    with (root / "collector_species.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["chinese_name", "scientific_names"])
        writer.writeheader()
        writer.writerows(
            {
                "chinese_name": row["species"],
                "scientific_names": row["scientific_names"],
            }
            for row in rows
        )
    (root / "catalog_manifest.json").write_text(
        json.dumps(
            {
                "species_file_sha256": _digest(species_file),
                "taxonomy_file_sha256": _digest(taxonomy_file),
                "active_species": [row["species"] for row in rows],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    state_path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return root


def pending_collection_species(catalog_root: Path) -> list[str]:
    state = _read_state(Path(catalog_root) / "collection_state.json")
    return sorted(name for name, value in state.items() if value.get("status") == "pending")


def mark_collection_complete(catalog_root: Path, species: list[str]) -> None:
    path = Path(catalog_root) / "collection_state.json"
    state = _read_state(path)
    for name in species:
        if name not in state:
            raise ValueError(f"species is not active in catalog: {name}")
        state[name] = {"status": "completed"}
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _read_state(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("collection state must be a JSON object")
    result: dict[str, dict[str, str]] = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not isinstance(value, dict):
            raise ValueError("collection state is invalid")
        status = value.get("status")
        if status not in {"pending", "completed"}:
            raise ValueError("collection state has invalid status")
        result[name] = {"status": status}
    return result
