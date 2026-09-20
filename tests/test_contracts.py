from pathlib import Path
import pytest
from pydantic import ValidationError
from PIL import Image
from agentized_workflow.models import Box, Localization
from agentized_workflow.tools import iou, consistent, pixels, ToolRegistry
from agentized_workflow.metadata import load_tasks


def box(values):
    return Box(bbox=values)


def test_geometry_and_unordered_matching():
    a, b = box([0, 0, 10, 10]), box([5, 0, 15, 10])
    assert iou(a, b) == pytest.approx(1/3)
    far = box([100, 100, 200, 200])
    assert consistent([a, far], [far, a], .7)
    assert not consistent([a, far], [a], .7)
    assert not consistent([], [], .7)
    assert not consistent([a, far], [a, a], .7)
    assert pixels(box([0, 0, 999, 999]), 200, 100) == [0, 0, 199.8, 99.9]


@pytest.mark.parametrize('coords', [[0,0,0,2],[-1,0,2,2],[0,0,1000,2],[0,0,float('nan'),2]])
def test_invalid_boxes_rejected(coords):
    with pytest.raises(ValidationError): box(coords)


def test_model_cannot_supply_species():
    with pytest.raises(ValidationError):
        Localization.model_validate({'boxes': [{'bbox': [0,0,10,10], 'species':'invented'}]})


def test_registry_rejects_arbitrary_tools():
    registry = ToolRegistry()
    with pytest.raises(ValueError): registry.call('shell', command='echo bad')
    assert registry.call('iou', a=box([0,0,10,10]), b=box([0,0,10,10])) == 1


def test_csv_preserves_name_and_rejects_conflicts(tmp_path):
    Image.new('RGB',(80,40)).save(tmp_path/'a.png')
    csv = tmp_path/'metadata.csv'
    csv.write_text('source_image,species\na.png,可信物种\n', encoding='utf-8')
    tasks = load_tasks(tmp_path, csv)
    assert tasks[0].species == '可信物种'
    assert (tasks[0].width,tasks[0].height) == (80,40)
    csv.write_text('source_image,species\na.png,甲\na.png,乙\n', encoding='utf-8')
    with pytest.raises(ValueError): load_tasks(tmp_path, csv)


def test_path_traversal_rejected(tmp_path):
    csv=tmp_path/'metadata.csv'
    csv.write_text('source_image,species\n../outside.png,甲\n',encoding='utf-8')
    with pytest.raises(ValueError): load_tasks(tmp_path,csv)


def test_trusted_directory_map_exact_label(tmp_path):
    (tmp_path/'1.crab').mkdir()
    Image.new('RGB',(20,20)).save(tmp_path/'1.crab/a.png')
    tasks=load_tasks(tmp_path, directory_map={'1.crab':'原始名称'})
    assert tasks[0].species == '原始名称'
    assert tasks[0].species_source == 'trusted_directory'


def test_perfect_matching_avoids_greedy_false_negative():
    left=[box([0,0,10,10]),box([2,0,12,10])]
    right=[box([1,0,11,10]),box([0,0,9,10])]
    assert consistent(left,right,.7)
