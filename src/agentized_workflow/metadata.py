from __future__ import annotations
import csv
import hashlib
import json
from pathlib import Path, PurePosixPath
from PIL import Image, ImageOps
from .models import TaskSpec


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024*1024), b''):
            value.update(chunk)
    return value.hexdigest()


def safe_relative(value: str) -> str:
    value = value.replace('\\', '/')
    path = PurePosixPath(value)
    if not value or path.is_absolute() or '..' in path.parts or ':' in value:
        raise ValueError('expected a safe relative image path')
    return path.as_posix()


def load_tasks(input_dir: Path, metadata_csv: Path | None = None, *, directory_map: dict[str,str] | None = None) -> list[TaskSpec]:
    root = Path(input_dir).resolve(strict=True)
    if (metadata_csv is None) == (directory_map is None):
        raise ValueError('supply exactly one trusted metadata source')
    entries = {}
    if metadata_csv is not None:
        with Path(metadata_csv).open(encoding='utf-8-sig', newline='') as handle:
            reader = csv.DictReader(handle)
            if not {'source_image','species'} <= set(reader.fieldnames or []):
                raise ValueError('CSV needs source_image,species')
            for row in reader:
                source = safe_relative(row.get('source_image') or '')
                name = row.get('species') or ''
                if not name or name != name.strip():
                    raise ValueError('species must be nonempty and exact; fix whitespace in trusted source')
                key = source.casefold()
                if key in entries and entries[key] != (source,name):
                    raise ValueError('duplicate or conflicting metadata')
                entries[key] = (source,name)
        provenance = digest(metadata_csv)
        origin = 'metadata_csv'
    else:
        normalized = {safe_relative(k):v for k,v in directory_map.items()}
        if any(not isinstance(v,str) or not v or v != v.strip() for v in normalized.values()):
            raise ValueError('invalid trusted directory name')
        for image in sorted(root.rglob('*')):
            if image.is_file() and image.suffix.lower() in {'.jpg','.jpeg','.png','.webp','.bmp'}:
                source = image.relative_to(root).as_posix()
                matches = [name for folder,name in normalized.items() if PurePosixPath(folder) in PurePosixPath(source).parents]
                if len(matches) != 1:
                    raise ValueError('image must have exactly one trusted directory mapping: '+source)
                entries[source.casefold()] = (source,matches[0])
        provenance = hashlib.sha256(json.dumps(normalized,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        origin = 'trusted_directory'
    if not entries:
        raise ValueError('no images in trusted source')
    tasks = []
    for source,name in entries.values():
        image = (root/source).resolve()
        if not image.is_relative_to(root):
            raise ValueError('image escapes input directory')
        with Image.open(image) as raw:
            shown = ImageOps.exif_transpose(raw)
            width,height = shown.size
        task_id = hashlib.sha256(str(image).casefold().encode()).hexdigest()[:24]
        tasks.append(TaskSpec(task_id=task_id,image_path=str(image),source_image=source,
            image_sha256=digest(image),species=name,species_source=origin,
            provenance_sha256=provenance,width=width,height=height))
    return tasks
