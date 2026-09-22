from __future__ import annotations

from io import BytesIO
import importlib.util
from pathlib import Path
import sys

from PIL import Image
import pytest


def _collector_module():
    path = Path(__file__).resolve().parents[1] / "tools" / "collect_species_images.py"
    spec = importlib.util.spec_from_file_location("collect_species_images_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _Response:
    def __init__(self, payload: bytes) -> None:
        self.headers = {"Content-Length": str(len(payload))}
        self.url = "https://images.example.org/photo.jpg"
        self._payload = payload

    def iter_content(self, _chunk_size: int):
        yield self._payload


class _Client:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def get(self, _url: str, **_kwargs):
        return _Response(self.payload)


def test_small_image_is_a_candidate_rejection(tmp_path: Path) -> None:
    collector = _collector_module()
    buffer = BytesIO()
    Image.new("RGB", (120, 400), "green").save(buffer, format="JPEG")

    with pytest.raises(collector.CandidateRejected, match="图片过小"):
        collector.download_and_validate(
            _Client(buffer.getvalue()), "https://images.example.org/photo.jpg", tmp_path, 1024 * 1024, 300
        )
