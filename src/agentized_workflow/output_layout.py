"""Staff-facing species/visibility paths; canonical result records stay stable."""
from __future__ import annotations

import csv
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile

from .metadata import safe_relative

ROUTE_FOLDERS = {
    'whole_or_mostly_visible': '01_完整或大部分可见',
    'partially_visible': '02_局部可见',
    'mixed': '03_混合可见',
}


def _component(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError('empty output name')
    clean = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', value).strip().rstrip('. ')
    reserved = clean.split('.')[0].upper() in {
        'CON', 'PRN', 'AUX', 'NUL', *{f'COM{i}' for i in range(1, 10)},
        *{f'LPT{i}' for i in range(1, 10)}}
    if not clean or reserved:
        clean = '_' + clean
    if clean != value or len(clean) > 48:
        clean = clean[:38] + '__' + hashlib.sha256(value.encode('utf-8')).hexdigest()[:8]
    return clean


def annotation_path(run_dir: Path, result_path: Path, result: dict | None = None) -> Path:
    run = Path(run_dir).resolve(strict=True)
    source = Path(result_path).resolve(strict=True)
    if source.parent not in {run / 'results', run / 'partial_results'}:
        raise ValueError('result must remain in the run results directory')
    data = result if result is not None else json.loads(source.read_text(encoding='utf-8'))
    if not isinstance(data, dict):
        raise ValueError('result must be a JSON object')
    route = data.get('visibility_route')
    if route not in ROUTE_FOLDERS:
        raise ValueError('missing or invalid visibility_route')
    species = _component(data['metadata']['species'])
    image_stem = _component(PurePosixPath(safe_relative(data['source_image'])).stem)
    task_stem = _component(source.stem)
    root = (run / ('partial_annotated' if source.parent.name == 'partial_results' else 'annotated')).resolve()
    output = (root / species / ROUTE_FOLDERS[route] / f'{image_stem}__{task_stem}.jpg').resolve()
    if not root.is_relative_to(run) or not output.is_relative_to(root):
        raise ValueError('annotation output escapes run directory')
    return output


def ensure_group_directories(output: Path) -> None:
    species_root = output.parent.parent.resolve()
    for name in ROUTE_FOLDERS.values():
        folder = species_root / name
        if not folder.resolve().is_relative_to(species_root):
            raise ValueError('visibility directory escapes species directory')
        folder.mkdir(parents=True, exist_ok=True)


def write_annotation_indexes(run_dir: Path) -> None:
    run = Path(run_dir).resolve(strict=True)
    grouped = {'annotated': [], 'partial_annotated': []}
    for name in ('results', 'partial_results'):
        for result_path in sorted((run / name).glob('*.json')):
            try:
                data = json.loads(result_path.read_text(encoding='utf-8'))
                output = annotation_path(run, result_path, data)
                if not output.is_file() or output.stat().st_mtime_ns < result_path.stat().st_mtime_ns:
                    continue
                legacy = (output.parent.parent.parent / f'{result_path.stem}.jpg').resolve()
                if legacy.exists():
                    continue  # Unresolved legacy/grouped conflicts must not enter the index.
                kind = 'partial_annotated' if name == 'partial_results' else 'annotated'
                grouped[kind].append({
                    'species': data['metadata']['species'], 'visibility_route': data['visibility_route'],
                    'source_image': data['source_image'], 'task_id': result_path.stem,
                    'annotation_path': output.relative_to(run).as_posix(),
                    'result_path': result_path.relative_to(run).as_posix(),
                    'output_kind': 'partial' if name == 'partial_results' else 'complete',
                    'result_kind': 'human_correction' if 'human_correction' in data else 'model'})
            except (OSError, ValueError, KeyError, TypeError):
                continue  # The renderer reports failed records; an index never certifies them.
    fields = ['species', 'visibility_route', 'source_image', 'task_id',
              'annotation_path', 'result_path', 'output_kind', 'result_kind']
    for kind, rows in grouped.items():
        root = (run / kind).resolve()
        if not root.is_relative_to(run):
            raise ValueError('index output escapes run directory')
        if not root.is_dir():
            continue
        fd, temporary = tempfile.mkstemp(dir=root, suffix='.tmp')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8-sig', newline='') as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerows(sorted(rows, key=lambda row: (row['species'], row['visibility_route'], row['task_id'])))
                handle.flush(); os.fsync(handle.fileno())
            os.replace(temporary, root / 'index.csv')
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
