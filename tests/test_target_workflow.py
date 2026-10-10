import json
from pathlib import Path
import pytest
from PIL import Image
from agentized_workflow.metadata import load_tasks
from agentized_workflow.storage import Store

A = [50, 50, 150, 150]
B = [300, 300, 400, 400]
C = [600, 600, 700, 700]
D = [615, 600, 715, 700]  # IoU 0.73913 with C

def reply(*boxes):
    return {"coordinate_system": "qwen_0_999", "boxes": [{"bbox": b} for b in boxes]}

class Vision:
    def __init__(self, passes, review=None):
        self.passes = iter(passes)
        self.review = review
        self.full_requests = []
        self.crop_requests = []

    def visibility(self, request):
        return {"route": "mixed"}

    def localize(self, request):
        self.full_requests.append(request)
        return next(self.passes)

    def review_target(self, request):
        self.crop_requests.append(request)
        if isinstance(self.review, BaseException):
            raise self.review
        if self.review is None:
            return reply()
        x, y, right, bottom = request.region
        def local(box):
            return [(box[0]*request.task.width/1000-x)*1000/(right-x),
                    (box[1]*request.task.height/1000-y)*1000/(bottom-y),
                    (box[2]*request.task.width/1000-x)*1000/(right-x),
                    (box[3]*request.task.height/1000-y)*1000/(bottom-y)]
        return reply(*(local(box) for box in self.review))

@pytest.fixture
def prepared(tmp_path):
    Image.new("RGB", (1000, 1000), "white").save(tmp_path / "one.jpg")
    metadata = tmp_path / "metadata.csv"
    metadata.write_text("source_image,species\none.jpg,target\n", encoding="utf-8")
    return Store(tmp_path / "run"), load_tasks(tmp_path, metadata)[0]

def engine(store, vision):
    from agentized_workflow.target_workflow import TargetEngine
    return TargetEngine(store, vision)

def test_mixed_targets_preserved_and_only_failed_target_reviewed(prepared):
    store, spec = prepared
    vision = Vision([reply(A, B, C), reply(B, D, A)], [C])
    runner = engine(store, vision); runner.add(spec)
    state = runner.run(spec.task_id)
    assert state.route == "mixed" and state.phase == "done"
    assert state.policy.version == 2 and state.policy.threshold == .75
    assert len(vision.full_requests) == 2 and len(vision.crop_requests) == 1
    assert len(state.attempts) == 2
    assert [t.selected_box.bbox for t in state.targets] == [tuple(A), tuple(B), tuple(C)]
    assert [t.accepted_pair for t in state.targets] == ["first_second", "first_second", "first_review"]
    request = vision.crop_requests[0]
    assert request.region[0] < 600 and request.region[2] > 715
    assert "boxes" not in request.model_dump() and "iou" not in request.model_dump()
    data = json.loads((store.root / "results" / f"{spec.task_id}.json").read_text())
    assert len(data["detections"]) == 3 and data["complete"] is True
    resumed = engine(Store(store.root), Vision([])).run(spec.task_id)
    assert resumed == state

def test_failed_crop_keeps_partial_outputs_out_of_complete_results(prepared):
    store, spec = prepared
    vision = Vision([reply(A, C), reply(A, D)])
    runner = engine(store, vision); runner.add(spec)
    state = runner.run(spec.task_id)
    assert state.phase == "partial_review"
    assert state.targets[0].status == "accepted"
    assert state.targets[1].status == "needs_review"
    assert not (store.root / "results" / f"{spec.task_id}.json").exists()
    partial = json.loads((store.root / "partial_results" / f"{spec.task_id}.json").read_text())
    assert partial["complete"] is False and len(partial["detections"]) == 1
    assert len(store.review_queue()) == 1
    assert engine(store, Vision([])).run(spec.task_id).phase == "partial_review"

def test_iou_equal_to_point75_does_not_pass(prepared):
    store, spec = prepared
    first = [500, 500, 900, 900]
    second = [500, 500, 800, 900]
    vision = Vision([reply(A, first), reply(A, second)])
    runner = engine(store, vision); runner.add(spec)
    state = runner.run(spec.task_id)
    assert state.phase == "partial_review" and len(vision.crop_requests) == 1
    assert state.targets[1].pair_iou == .75

@pytest.mark.parametrize("passes", [(reply(A, B), reply(A)), (reply(A, A), reply(A, A))])
def test_missing_or_ambiguous_target_never_triggers_guessed_crop(prepared, passes):
    store, spec = prepared
    vision = Vision(passes)
    runner = engine(store, vision); runner.add(spec)
    state = runner.run(spec.task_id)
    assert state.phase in {"needs_review", "partial_review"}
    assert not vision.crop_requests

def test_interrupted_crop_is_not_replayed_and_keeps_accepted_targets(prepared):
    store, spec = prepared
    vision = Vision([reply(A, C), reply(A, D)], KeyboardInterrupt())
    runner = engine(store, vision); runner.add(spec)
    with pytest.raises(KeyboardInterrupt): runner.run(spec.task_id)
    assert store.get(spec.task_id).in_flight
    state = engine(store, Vision([])).run(spec.task_id)
    assert state.phase == "partial_review"
    assert state.targets[0].status == "accepted"
    assert state.targets[1].reason == "interrupted_call"

def test_two_empty_passes_do_not_trigger_full_image_third_call(prepared):
    store, spec = prepared
    vision = Vision([reply(), reply()])
    runner = engine(store, vision); runner.add(spec)
    state = runner.run(spec.task_id)
    assert state.phase == "needs_review" and len(vision.full_requests) == 2
    assert not vision.crop_requests

def test_crop_provider_sends_context_without_old_predictions(prepared):
    from agentized_workflow.target_providers import TargetHttpVision
    from agentized_workflow.models import TargetReviewRequest
    import base64, io
    _, spec = prepared
    payloads = []
    def transport(payload):
        payloads.append(payload)
        return json.dumps({'coordinate_system': 'qwen_0_999', 'boxes': [{
            'bbox': [100, 100, 500, 500],
            'carapace_check': {'status': 'not_applicable', 'evidence': ''}}]})
    vision = TargetHttpVision(transport, model='offline', structured=True)
    vision.visibility(__import__('agentized_workflow.models', fromlist=['VisionRequest']).VisionRequest(
        task=spec, request_id='visibility', pass_number=0, max_targets=10))
    assert 'mixed' in payloads[-1]['response_format']['json_schema']['schema']['properties']['route']['enum']
    request = TargetReviewRequest(task=spec, request_id='crop', target_id='target-003',
                                  region=(500, 550, 800, 750), max_targets=10)
    vision.review_target(request)
    payload = payloads[-1]
    assert 'mixed' in payloads[0]['messages'][0]['content']
    message = payload['messages'][1]['content']
    text = json.loads(message[0]['text'])
    assert set(text) == {'trusted_species', 'max_targets'}
    image = Image.open(io.BytesIO(base64.b64decode(message[1]['image_url']['url'].split(',')[1])))
    assert image.size == (300, 200)
    assert len(payload['messages']) == 2

def test_partial_render_and_registry_never_reuse_incomplete_photo(tmp_path):
    from agentized_workflow.photo_library import PhotoLibrary
    from agentized_workflow.label_registry import LabelRegistry, profile_for
    from agentized_workflow.render_labels import render_completed
    catalog = tmp_path / 'catalog'; catalog.mkdir()
    (catalog / 'metadata.csv').write_text('species,status\ntarget,active\n')
    batch = tmp_path / 'batch' / 'target'; batch.mkdir(parents=True)
    Image.new('RGB', (1000, 1000)).save(batch / 'one.jpg')
    photos = tmp_path / 'photos'; library = PhotoLibrary(catalog, photos)
    library.ingest_batch(batch.parent)
    spec = load_tasks(photos, library.write_workflow_metadata())[0]
    store = Store(tmp_path / 'runs' / 'agent-test')
    runner = engine(store, Vision([reply(A, C), reply(A, D)])); runner.add(spec)
    state = runner.run(spec.task_id)
    registry = LabelRegistry(photos)
    profile = profile_for('offline', None, .75, 10, False, None, version=2)
    case_id = registry.record(state, store.root, profile)
    assert registry.lookup(spec, profile).action == 'review'
    assert registry.case(case_id)['result_path'].endswith(f'partial_results/{spec.task_id}.json') or 'partial_results' in registry.case(case_id)['result_path']
    report = render_completed(photos, store.root)
    assert report['rendered'] == 1
    assert not (store.root / 'annotated').exists()
    assert (store.root / 'partial_annotated' / 'target' / '03_混合可见' / f'{Path(spec.source_image).stem}__{spec.task_id}.jpg').is_file()
    corrected = registry.correct(case_id, [A, C], 'reviewer', 'checked complete image')
    assert registry.lookup(spec, profile).action == 'reuse'
    assert registry.case(corrected)['result_kind'] == 'human_correction'
    Image.new('RGB', (1000, 1000), 'red').save(spec.image_path)
    with pytest.raises(ValueError, match='source image'):
        registry.correct(corrected, [A, C], 'reviewer', 'must not adopt changed source')

def test_v2_cli_fixture_exports_composite_and_resumes_original_version(prepared, tmp_path):
    from agentized_workflow.cli import main
    store, spec = prepared
    fixture = tmp_path / 'fixture.json'
    fixture.write_text(json.dumps({'one.jpg': {'visibility': {'route': 'mixed'},
        'localizations': [reply(A, B), reply(B, A)]}}))
    args = ['--input-dir', str(tmp_path), '--metadata-csv', str(tmp_path/'metadata.csv'),
            '--fixture', str(fixture), '--workflow-version', '2', '--run-dir', str(store.root)]
    assert main(args) == 0
    assert store.get(spec.task_id).policy.version == 2
    assert main(['--resume-existing', '--fixture', str(fixture), '--run-dir', str(store.root)]) == 0


def test_multiple_unresolved_targets_each_get_one_crop(prepared):
    store, spec = prepared
    shifted_b = [315, 300, 415, 400]
    class CropVision(Vision):
        def review_target(self, request):
            self.review = [B] if request.target_id == 'target-002' else [C]
            return super().review_target(request)
    vision = CropVision([reply(A, B, C), reply(A, shifted_b, D)])
    runner = engine(store, vision); runner.add(spec)
    state = runner.run(spec.task_id)
    assert state.phase == 'done' and len(vision.crop_requests) == 2
    assert len({r.target_id for r in vision.crop_requests}) == 2


def test_target_limit_reached_never_claims_complete_image(prepared):
    from agentized_workflow.target_workflow import TargetEngine
    store, spec = prepared
    runner = TargetEngine(store, Vision([reply(A), reply(A)]), max_targets=1)
    runner.add(spec)
    state = runner.run(spec.task_id)
    assert state.phase == 'partial_review' and state.reason == 'target_budget_reached'


def test_crop_mapping_at_original_image_edge_stays_in_coordinate_contract(prepared):
    from agentized_workflow.target_matching import map_crop_result
    from agentized_workflow.models import Localization
    _, spec = prepared
    result = map_crop_result(Localization.model_validate(reply([0, 0, 999, 999])), (950, 950, 1000, 1000), spec)
    assert result.boxes[0].bbox == (950, 950, 999, 999)


@pytest.mark.parametrize('crop_reply', [[C, C], [[750, 750, 800, 800]], []])
def test_duplicate_unmatched_or_empty_crop_cannot_accept_target(prepared, crop_reply):
    store, spec = prepared
    runner = engine(store, Vision([reply(A, C), reply(A, D)], crop_reply)); runner.add(spec)
    state = runner.run(spec.task_id)
    assert state.phase == 'partial_review' and state.targets[0].status == 'accepted'
    assert state.targets[1].status == 'needs_review'


@pytest.mark.parametrize('threshold', [.7, .74, 1])
def test_v2_rejects_weaker_or_impossible_threshold(prepared, threshold):
    from agentized_workflow.target_workflow import TargetEngine
    store, _ = prepared
    with pytest.raises(ValueError, match='threshold'):
        TargetEngine(store, Vision([]), threshold=threshold)




def test_third_box_at_exact_threshold_is_not_accepted(prepared):
    store, spec = prepared
    runner = engine(store, Vision([reply(A, C), reply(A, D)], [[600, 600, 675, 700]]))
    runner.add(spec)
    state = runner.run(spec.task_id)
    assert state.phase == 'partial_review' and state.targets[1].status == 'needs_review'


def test_third_box_can_match_only_second_candidate(prepared):
    store, spec = prepared
    runner = engine(store, Vision([reply(A, C), reply(A, D)], [[628, 600, 728, 700]]))
    runner.add(spec)
    state = runner.run(spec.task_id)
    assert state.phase == 'done'
    assert state.targets[1].accepted_pair == 'second_review'
    assert state.targets[1].accepted_iou > .75
