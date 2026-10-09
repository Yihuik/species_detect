"""Human review must override box agreement when carapace morphology is suspect."""
import json

import pytest
from PIL import Image
from pydantic import ValidationError

from agentized_workflow.metadata import load_tasks
from agentized_workflow.models import Box, Localization, TargetReviewRequest, VisionRequest
from agentized_workflow.storage import Store
from agentized_workflow.target_providers import TargetHttpVision
from agentized_workflow.target_workflow import TargetEngine

A = [50, 50, 150, 150]
C = [600, 600, 700, 700]
D = [615, 600, 715, 700]
EVIDENCE = 'Visible carapace extends much farther on the right; possible merged boundary.'


def checked(box, status='symmetric', evidence=''):
    return {'bbox': box, 'carapace_check': {'status': status, 'evidence': evidence}}


def reply(*boxes):
    return {'coordinate_system': 'qwen_0_999', 'boxes': list(boxes)}


class OfflineVision:
    """Fake the external response boundary, keeping storage and engine real."""
    def __init__(self, passes, crop=None):
        self.passes = iter(passes)
        self.crop = crop or []
        self.full_requests = []
        self.crop_requests = []

    def visibility(self, request):
        return {'route': 'mixed'}

    def localize(self, request):
        self.full_requests.append(request)
        return next(self.passes)

    def review_target(self, request):
        self.crop_requests.append(request)
        x, y, right, bottom = request.region
        boxes = []
        for entry in self.crop:
            a, b, c, d = entry['bbox']
            local = [(a-x)*1000/(right-x), (b-y)*1000/(bottom-y),
                     (c-x)*1000/(right-x), (d-y)*1000/(bottom-y)]
            boxes.append({**entry, 'bbox': local})
        return reply(*boxes)


@pytest.fixture
def prepared(tmp_path):
    Image.new('RGB', (1000, 1000), 'white').save(tmp_path / 'one.jpg')
    metadata = tmp_path / 'metadata.csv'
    metadata.write_text('source_image,species\none.jpg,crab\n', encoding='utf-8')
    return Store(tmp_path / 'run'), load_tasks(tmp_path, metadata)[0]


@pytest.mark.parametrize('flagged_pass', [0, 1])
def test_one_suspicious_pass_blocks_iou_one_without_extra_model_call(prepared, flagged_pass):
    store, spec = prepared
    passes = [reply(checked(A), checked(C)), reply(checked(A), checked(C))]
    passes[flagged_pass]['boxes'][1] = checked(C, 'suspected_asymmetry', EVIDENCE)
    vision = OfflineVision(passes)
    runner = TargetEngine(store, vision); runner.add(spec)
    state = runner.run(spec.task_id)
    assert state.phase == 'partial_review'
    assert state.reason == 'carapace_asymmetry'
    assert state.targets[0].status == 'accepted'
    assert state.targets[1].status == 'needs_review'
    assert state.targets[1].reason == 'carapace_asymmetry'
    assert state.targets[1].pair_iou == 1
    assert state.targets[1].selected_box is None
    assert len(vision.full_requests) == 2 and not vision.crop_requests
    assert not (store.root / 'results' / f'{spec.task_id}.json').exists()
    data = json.loads((store.root / 'partial_results' / f'{spec.task_id}.json').read_text())
    assert data['complete'] is False
    assert [entry['bbox'] for entry in data['detections']] == [A]
    flagged_key = 'first_box' if flagged_pass == 0 else 'second_box'
    assert data['targets'][1][flagged_key]['carapace_check']['evidence'] == EVIDENCE
    resumed = TargetEngine(Store(store.root), OfflineVision([])).run(spec.task_id)
    assert resumed == state
    assert store.review_queue()[0]['reason'] == 'carapace_asymmetry'


def test_all_suspicious_targets_remain_manual_with_candidate_boxes(prepared):
    store, spec = prepared
    suspicious = checked(C, 'suspected_asymmetry', EVIDENCE)
    vision = OfflineVision([reply(suspicious), reply(checked(C))])
    runner = TargetEngine(store, vision); runner.add(spec)
    state = runner.run(spec.task_id)
    assert state.phase == 'needs_review' and state.reason == 'carapace_asymmetry'
    assert state.targets[0].first_box.bbox == tuple(C)
    data = json.loads((store.root / 'partial_results' / f'{spec.task_id}.json').read_text())
    assert data['detections'] == [] and not data['complete']
    assert not vision.crop_requests


@pytest.mark.parametrize('status', ['not_applicable', 'symmetric', 'unassessable'])
def test_safe_or_unassessable_check_does_not_force_symmetry_or_review(prepared, status):
    store, spec = prepared
    evidence = 'Perspective or occlusion prevents comparing sides.' if status == 'unassessable' else ''
    vision = OfflineVision([reply(checked(C, status, evidence))]*2)
    runner = TargetEngine(store, vision); runner.add(spec)
    state = runner.run(spec.task_id)
    assert state.phase == 'done' and not vision.crop_requests
    assert state.targets[0].selected_box.carapace_check.status == status


def test_suspicious_third_result_retains_evidence_after_crop_mapping(prepared):
    store, spec = prepared
    vision = OfflineVision([reply(checked(A), checked(C)), reply(checked(A), checked(D))],
                           [checked(C, 'suspected_asymmetry', EVIDENCE)])
    runner = TargetEngine(store, vision); runner.add(spec)
    state = runner.run(spec.task_id)
    assert state.phase == 'partial_review' and state.reason == 'carapace_asymmetry'
    assert state.targets[0].status == 'accepted'
    target = state.targets[1]
    assert target.status == 'needs_review' and target.reason == 'carapace_asymmetry'
    assert target.review.result.boxes[0].bbox == pytest.approx(C)
    assert target.review.result.boxes[0].carapace_check.evidence == EVIDENCE
    assert len(vision.crop_requests) == 1


def test_context_crop_flag_cannot_leave_neighbor_automatically_accepted(prepared):
    store, spec = prepared
    neighbor = [574, 610, 594, 690]  # Inside C's padded crop, but disjoint from C and D.
    vision = OfflineVision([reply(checked(neighbor), checked(C)), reply(checked(neighbor), checked(D))],
                           [checked(neighbor, 'suspected_asymmetry', EVIDENCE), checked(C)])
    runner = TargetEngine(store, vision); runner.add(spec)
    state = runner.run(spec.task_id)
    assert state.phase == 'partial_review'
    assert state.targets[0].status == 'needs_review'
    assert state.targets[0].reason == 'carapace_asymmetry'
    assert state.targets[1].status == 'accepted'
    assert len(vision.crop_requests) == 1
    data = json.loads((store.root / 'partial_results' / f'{spec.task_id}.json').read_text())
    assert len(data['detections']) == 1 and data['detections'][0]['bbox'] == pytest.approx(C)


@pytest.mark.parametrize('stage', ['full', 'crop'])
def test_live_provider_rejects_silently_missing_checks(prepared, stage):
    _, spec = prepared
    vision = TargetHttpVision(lambda payload: json.dumps(reply({'bbox': C})), model='offline')
    with pytest.raises(ValueError, match='carapace_check'):
        if stage == 'full':
            vision.localize(VisionRequest(task=spec, request_id='full', pass_number=1, max_targets=10))
        else:
            vision.review_target(TargetReviewRequest(task=spec, request_id='crop', target_id='t',
                                                    region=(500, 500, 800, 800), max_targets=10))


def test_live_full_and_crop_schemas_carry_same_check_contract(prepared):
    _, spec = prepared
    requests = []
    def transport(payload):
        requests.append(payload)
        return json.dumps(reply(checked(C, 'suspected_asymmetry', EVIDENCE)))
    vision = TargetHttpVision(transport, model='offline')
    result = vision.localize(VisionRequest(task=spec, request_id='full', pass_number=1, max_targets=10))
    assert Localization.model_validate(result).boxes[0].carapace_check.status == 'suspected_asymmetry'
    vision.review_target(TargetReviewRequest(task=spec, request_id='crop', target_id='t',
                                           region=(500, 500, 800, 800), max_targets=10))
    for payload in requests:
        item = payload['response_format']['json_schema']['schema']['properties']['boxes']['items']
        assert 'carapace_check' in item['required']
        check = item['properties']['carapace_check']
        assert check['required'] == ['status', 'evidence']
        assert 'suspected_asymmetry' in check['properties']['status']['enum']


def test_suspicion_requires_observable_evidence():
    with pytest.raises(ValidationError, match='evidence is required for suspected_asymmetry'):
        Localization.model_validate(reply(checked(C, 'suspected_asymmetry', '   ')))


def test_legacy_boxes_serialize_without_new_fields():
    box = Box(bbox=A)
    assert box.model_dump(mode='json') == {'bbox': A}
    result = Localization.model_validate(reply({'bbox': C}))
    assert result.model_dump(mode='json') == reply({'bbox': C})


def test_suspicious_library_photo_is_not_reused_until_human_correction(tmp_path):
    from agentized_workflow.label_registry import LabelRegistry, profile_for
    from agentized_workflow.photo_library import PhotoLibrary
    catalog = tmp_path / 'catalog'; catalog.mkdir()
    (catalog / 'metadata.csv').write_text('species,status\ncrab,active\n')
    batch = tmp_path / 'batch' / 'crab'; batch.mkdir(parents=True)
    Image.new('RGB', (1000, 1000), 'white').save(batch / 'one.jpg')
    photos = tmp_path / 'photos'; library = PhotoLibrary(catalog, photos)
    library.ingest_batch(batch.parent)
    spec = load_tasks(photos, library.write_workflow_metadata())[0]
    store = Store(tmp_path / 'runs' / 'agent-test')
    vision = OfflineVision([reply(checked(C, 'suspected_asymmetry', EVIDENCE)), reply(checked(C))])
    runner = TargetEngine(store, vision); runner.add(spec)
    state = runner.run(spec.task_id)
    registry = LabelRegistry(photos)
    profile = profile_for('offline', None, .75, 10, False, None, version=2)
    case_id = registry.record(state, store.root, profile)
    assert registry.lookup(spec, profile).action == 'review'
    source = store.root / 'partial_results' / f'{spec.task_id}.json'
    before = source.read_bytes()
    corrected = registry.correct(case_id, [C], 'reviewer', 'Verified visible carapace and all connected limbs.')
    assert registry.lookup(spec, profile).action == 'reuse'
    assert registry.lookup(spec, profile).review_status == 'approved'
    assert registry.case(corrected)['result_kind'] == 'human_correction'
    assert source.read_bytes() == before
