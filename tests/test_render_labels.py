from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

def test_render_completed_draws_box_and_skips_current_photo(tmp_path: Path) -> None:
    from agentized_workflow.render_labels import render_completed
    photos = tmp_path / "photos"
    source = photos / "甲蟹" / "source.download"
    source.parent.mkdir(parents=True)
    Image.new("RGB", (100, 80), "white").save(source, format="JPEG")
    run = tmp_path / "run"
    results = run / "results"
    results.mkdir(parents=True)
    (results / "task-one.json").write_text(
        json.dumps({
            "source_image": "甲蟹/source.download",
            "image_width": 100,
            "image_height": 80,
            "metadata": {"species": "甲蟹"},
            "visibility_route": "whole_or_mostly_visible",
            "detections": [{"bbox_pixel": [10, 20, 60, 50], "species": "甲蟹"}],
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    assert render_completed(photos, run) == {"rendered": 1, "moved": 0, "skipped": 0, "failed": 0}
    output = run / "annotated" / "甲蟹" / "01_完整或大部分可见" / "source__task-one.jpg"
    with Image.open(output) as image:
        assert image.size == (100, 80)
        red = image.getpixel((10, 30))
        white = image.getpixel((90, 70))
    assert red[0] > 180 and red[1] < 100 and red[2] < 100
    assert all(channel > 220 for channel in white)
    first_mtime = output.stat().st_mtime_ns

    assert render_completed(photos, run) == {"rendered": 0, "moved": 0, "skipped": 1, "failed": 0}
    assert output.stat().st_mtime_ns == first_mtime


def test_render_completed_uses_model_exif_orientation(tmp_path: Path) -> None:
    from agentized_workflow.render_labels import render_completed
    photos = tmp_path / "photos"
    photos.mkdir()
    source = photos / "rotated.jpg"
    exif = Image.Exif()
    exif[274] = 6
    Image.new("RGB", (80, 100), "white").save(source, exif=exif)
    run = tmp_path / "run"
    results = run / "results"
    results.mkdir(parents=True)
    (results / "task-rotated.json").write_text(
        json.dumps({
            "source_image": "rotated.jpg",
            "image_width": 100,
            "image_height": 80,
            "metadata": {"species": "Crab"},
            "visibility_route": "whole_or_mostly_visible",
            "detections": [{"bbox_pixel": [10, 20, 60, 50], "species": "Crab"}],
        }),
        encoding="utf-8",
    )

    assert render_completed(photos, run)["rendered"] == 1
    with Image.open(run / "annotated" / "Crab" / "01_完整或大部分可见" / "rotated__task-rotated.jpg") as image:
        assert image.size == (100, 80)
