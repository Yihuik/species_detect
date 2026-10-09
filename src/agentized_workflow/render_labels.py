from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import sys
import tempfile

from PIL import Image, ImageDraw, ImageFont, ImageOps

from .metadata import safe_relative


def _label_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for name in (
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    ):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    return ImageFont.load_default()


def _render_one(photos_root: Path, result_path: Path, output_path: Path) -> None:
    result = json.loads(result_path.read_text(encoding="utf-8"))
    source = (photos_root / safe_relative(result["source_image"])).resolve(strict=True)
    if not source.is_relative_to(photos_root):
        raise ValueError("source image escapes photos root")
    with Image.open(source) as raw:
        image = ImageOps.exif_transpose(raw).convert("RGB")
    if image.size != (result["image_width"], result["image_height"]):
        raise ValueError("source image dimensions changed")

    draw = ImageDraw.Draw(image)
    font = _label_font(max(14, min(image.size) // 55))
    species = result["metadata"]["species"]
    line_width = max(2, min(image.size) // 350)
    for detection in result["detections"]:
        box = detection["bbox_pixel"]
        if len(box) != 4 or any(not math.isfinite(value) for value in box):
            raise ValueError("invalid pixel box")
        left, top, right, bottom = (round(value) for value in box)
        if not (0 <= left < right <= image.width and 0 <= top < bottom <= image.height):
            raise ValueError("pixel box outside image")
        draw.rectangle((left, top, right, bottom), outline=(255, 0, 0), width=line_width)
        bounds = draw.textbbox((0, 0), species, font=font)
        text_width, text_height = bounds[2] - bounds[0], bounds[3] - bounds[1]
        label_left = min(left, max(0, image.width - text_width - 6))
        label_top = max(0, top - text_height - 6)
        draw.rectangle(
            (label_left, label_top, label_left + text_width + 5, label_top + text_height + 5),
            fill=(255, 0, 0),
        )
        draw.text((label_left + 2, label_top + 2 - bounds[1]), species, fill="white", font=font)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=output_path.parent, suffix=".tmp")
    os.close(fd)
    try:
        image.save(temporary, format="JPEG", quality=92)
        os.replace(temporary, output_path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def render_completed(photos_root: Path, run_dir: Path) -> dict[str, int]:
    photos_root = Path(photos_root).resolve(strict=True)
    run_dir = Path(run_dir).resolve(strict=True)
    counts = {"rendered": 0, "skipped": 0, "failed": 0}
    result_paths = list((run_dir / 'results').glob('*.json')) + list((run_dir / 'partial_results').glob('*.json'))
    for result_path in sorted(result_paths):
        partial = result_path.parent.name == 'partial_results'
        output_path = run_dir / ('partial_annotated' if partial else 'annotated') / f"{result_path.stem}.jpg"
        if output_path.is_file() and output_path.stat().st_mtime_ns >= result_path.stat().st_mtime_ns:
            counts["skipped"] += 1
            continue
        try:
            _render_one(photos_root, result_path, output_path)
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            counts["failed"] += 1
            print(f"render failed for {result_path.name}: {type(exc).__name__}", file=sys.stderr)
        else:
            counts["rendered"] += 1
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description="Render completed bbox JSON as annotated local photos")
    parser.add_argument("--photos-root", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()
    counts = render_completed(args.photos_root, args.run_dir)
    print(json.dumps(counts))
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
