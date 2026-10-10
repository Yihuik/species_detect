"""Staff-facing grouping and lossless legacy migration at real file boundaries."""
import csv
import json
import os
from pathlib import Path

import pytest
from PIL import Image
from agentized_workflow.render_labels import render_completed


def prepared(tmp_path, route='whole_or_mostly_visible', *, partial=False, species='甲蟹'):
    photos = tmp_path / 'photos'; photos.mkdir(exist_ok=True)
    source = photos / 'photo_one.jpg'
    Image.new('RGB', (100, 80), 'white').save(source)
    run = tmp_path / 'run'
    result_root = run / ('partial_results' if partial else 'results')
    result_root.mkdir(parents=True)
    result = result_root / 't1.json'
    result.write_text(json.dumps({'source_image': 'photo_one.jpg', 'image_width': 100,
        'image_height': 80, 'metadata': {'species': species}, 'visibility_route': route,
        'complete': not partial, 'detections': [{'bbox_pixel': [10, 20, 60, 50], 'species': species}]},
        ensure_ascii=False), encoding='utf-8')
    return photos, run, result


def current_image(path, result, color='blue'):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new('RGB', (100, 80), color).save(path)
    stamp = result.stat().st_mtime_ns + 1_000_000_000
    os.utime(path, ns=(stamp, stamp))


@pytest.mark.parametrize('route,folder', [
    ('whole_or_mostly_visible', '01_完整或大部分可见'),
    ('partially_visible', '02_局部可见'),
    ('mixed', '03_混合可见')])
def test_species_visibility_grouping_has_traceable_name_and_index(tmp_path, route, folder):
    photos, run, result = prepared(tmp_path, route)
    assert render_completed(photos, run)['rendered'] == 1
    expected = run / 'annotated' / '甲蟹' / folder / 'photo_one__t1.jpg'
    assert expected.is_file()
    with Image.open(expected) as image:
        red = image.getpixel((10, 30))
        assert image.size == (100, 80) and red[0] > 180 and red[1] < 100
    for name in ['01_完整或大部分可见', '02_局部可见', '03_混合可见']:
        assert (run / 'annotated' / '甲蟹' / name).is_dir()
    with (run / 'annotated' / 'index.csv').open(encoding='utf-8-sig', newline='') as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1
    assert rows[0]['species'] == '甲蟹' and rows[0]['visibility_route'] == route
    assert rows[0]['source_image'] == 'photo_one.jpg'
    assert rows[0]['annotation_path'] == f'annotated/甲蟹/{folder}/photo_one__t1.jpg'
    assert rows[0]['result_path'] == 'results/t1.json'
    stamp = expected.stat().st_mtime_ns
    report = render_completed(photos, run)
    assert report['skipped'] == 1 and not report['rendered'] and not report['moved']
    assert expected.stat().st_mtime_ns == stamp


def test_current_flat_photo_moves_losslessly_with_json_untouched(tmp_path):
    photos, run, result = prepared(tmp_path)
    legacy = run / 'annotated' / 't1.jpg'
    current_image(legacy, result)
    before, stamp, json_before = legacy.read_bytes(), legacy.stat().st_mtime_ns, result.read_bytes()
    report = render_completed(photos, run)
    expected = run / 'annotated' / '甲蟹' / '01_完整或大部分可见' / 'photo_one__t1.jpg'
    assert expected.is_file() and not legacy.exists()
    assert report == {'rendered': 0, 'moved': 1, 'skipped': 0, 'failed': 0}
    assert expected.read_bytes() == before and expected.stat().st_mtime_ns == stamp
    assert result.read_bytes() == json_before


def test_partial_outputs_stay_out_of_complete_species_tree(tmp_path):
    photos, run, _ = prepared(tmp_path, 'mixed', partial=True)
    report = render_completed(photos, run)
    assert report['failed'] == 0
    assert (run / 'partial_annotated' / '甲蟹' / '03_混合可见' / 'photo_one__t1.jpg').is_file()
    assert (run / 'partial_annotated' / 'index.csv').is_file()
    assert not (run / 'annotated').exists()


def test_conflicting_flat_and_grouped_photos_are_not_overwritten(tmp_path):
    photos, run, result = prepared(tmp_path)
    legacy = run / 'annotated' / 't1.jpg'
    target = run / 'annotated' / '甲蟹' / '01_完整或大部分可见' / 'photo_one__t1.jpg'
    current_image(legacy, result, 'blue'); current_image(target, result, 'red')
    before = legacy.read_bytes(), target.read_bytes()
    assert render_completed(photos, run)['failed'] == 1
    assert legacy.read_bytes() == before[0] and target.read_bytes() == before[1]
    with (run / 'annotated' / 'index.csv').open(encoding='utf-8-sig', newline='') as handle:
        assert list(csv.DictReader(handle)) == []


def test_identical_legacy_duplicate_is_collapsed_only_after_equality(tmp_path):
    photos, run, result = prepared(tmp_path)
    legacy = run / 'annotated' / 't1.jpg'
    target = run / 'annotated' / '甲蟹' / '01_完整或大部分可见' / 'photo_one__t1.jpg'
    current_image(legacy, result)
    target.parent.mkdir(parents=True); target.write_bytes(legacy.read_bytes())
    stamp = legacy.stat().st_mtime_ns; os.utime(target, ns=(stamp, stamp))
    before = target.read_bytes()
    assert render_completed(photos, run)['failed'] == 0
    assert not legacy.exists() and target.read_bytes() == before


def test_unsafe_species_name_cannot_move_outputs_outside_run(tmp_path):
    photos, run, _ = prepared(tmp_path, species='../outside')
    assert render_completed(photos, run)['failed'] == 0
    files = list((run / 'annotated').rglob('*.jpg'))
    assert len(files) == 1
    assert len(files[0].relative_to(run / 'annotated').parts) == 3
    assert not (tmp_path / 'outside').exists()


def test_unknown_visibility_is_not_silently_assigned_to_a_category(tmp_path):
    photos, run, _ = prepared(tmp_path, route='unexpected')
    assert render_completed(photos, run)['failed'] == 1
    assert not list(run.rglob('*.jpg'))


def test_same_source_basename_keeps_distinct_task_outputs(tmp_path):
    photos, run, first = prepared(tmp_path)
    data = json.loads(first.read_text(encoding='utf-8'))
    for number in [1, 2]:
        source = photos / str(number) / 'same.jpg'
        source.parent.mkdir(); Image.new('RGB', (100, 80), 'white').save(source)
        data['source_image'] = f'{number}/same.jpg'
        (run / 'results' / f't{number}.json').write_text(json.dumps(data), encoding='utf-8')
    assert render_completed(photos, run)['rendered'] == 2
    folder = run / 'annotated' / '甲蟹' / '01_完整或大部分可见'
    assert {p.name for p in folder.glob('*.jpg')} == {'same__t1.jpg', 'same__t2.jpg'}


def test_malformed_json_object_is_reported_as_failed_without_crashing(tmp_path):
    photos, run, result = prepared(tmp_path)
    result.write_text('[]', encoding='utf-8')
    assert render_completed(photos, run)['failed'] == 1
    assert not list(run.rglob('*.jpg'))
