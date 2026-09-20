import json
import pytest
from PIL import Image
from agentized_workflow.metadata import load_tasks
from agentized_workflow.models import Localization
from agentized_workflow.storage import Store
from agentized_workflow.workflow import Engine
from agentized_workflow.planner import JsonPlanner

A={'boxes':[{'bbox':[10,10,200,200]}]}
B={'boxes':[{'bbox':[600,600,900,900]}]}
C={'boxes':[{'bbox':[300,300,400,400]}]}

class Vision:
    def __init__(self, results): self.results=iter(results); self.requests=[]
    def visibility(self, request): return {'route':'whole_or_mostly_visible'}
    def localize(self, request):
        self.requests.append(request)
        value=next(self.results)
        if isinstance(value, BaseException): raise value
        return value

@pytest.fixture
def setup(tmp_path):
    Image.new('RGB',(100,80)).save(tmp_path/'a.png')
    csv=tmp_path/'metadata.csv'
    csv.write_text('source_image,species\na.png,可信名称\n',encoding='utf-8')
    return Store(tmp_path/'run'),load_tasks(tmp_path,csv)[0]


def test_two_independent_passes_and_resume_do_not_recall(setup):
    store,spec=setup
    vision=Vision([A,A]); engine=Engine(store,vision)
    engine.add(spec); state=engine.run(spec.task_id)
    assert state.phase=='done' and len(state.attempts)==2
    assert len(vision.requests)==2
    assert all('boxes' not in r.model_dump() for r in vision.requests)
    assert vision.requests[0].request_id != vision.requests[1].request_id
    resumed=Engine(Store(store.root),Vision([])).run(spec.task_id)
    assert resumed.phase=='done'
    output=json.loads((store.root/'results'/f'{spec.task_id}.json').read_text(encoding='utf-8'))
    assert output['detections'][0]['species']=='可信名称'
    assert output['detections'][0]['bbox_pixel']==[1,0.8,20,16]
    assert store.audit(spec.task_id)[-1]['event']=='done'


def test_single_third_attempt_can_resolve_disagreement(setup):
    store,spec=setup
    engine=Engine(store,Vision([A,B,A])); engine.add(spec)
    state=engine.run(spec.task_id)
    assert state.phase=='done' and len(state.attempts)==3
    assert state.selected_attempt==3


def test_exhaustion_and_review_are_terminal(setup):
    store,spec=setup
    engine=Engine(store,Vision([A,B,C])); engine.add(spec)
    state=engine.run(spec.task_id)
    assert state.phase=='needs_review' and len(state.attempts)==3
    assert len(store.review_queue())==1
    assert Engine(store,Vision([])).run(spec.task_id).phase=='needs_review'
    assert len(store.review_queue())==1


@pytest.mark.parametrize('results', [[{'boxes':[]},{'boxes':[]},{'boxes':[]}], [ValueError('bad'),A,A]])
def test_empty_or_bad_result_never_bypasses_consistency(setup,results):
    store,spec=setup; engine=Engine(store,Vision(results)); engine.add(spec)
    state=engine.run(spec.task_id)
    assert len(state.attempts)==3
    assert state.phase==('done' if results[0].__class__ is ValueError else 'needs_review')


def test_resume_after_first_pass_keeps_its_result(setup):
    store,spec=setup; engine=Engine(store,Vision([A])); engine.add(spec)
    engine.step(spec.task_id); engine.step(spec.task_id)
    assert len(store.get(spec.task_id).attempts)==1
    resumed=Engine(Store(store.root),Vision([A])).run(spec.task_id)
    assert resumed.phase=='done' and len(resumed.attempts)==2


def test_crash_during_call_is_not_replayed(setup):
    store,spec=setup; engine=Engine(store,Vision([KeyboardInterrupt()])); engine.add(spec)
    engine.step(spec.task_id)
    with pytest.raises(KeyboardInterrupt): engine.step(spec.task_id)
    state=Engine(Store(store.root),Vision([])).run(spec.task_id)
    assert state.phase=='needs_review'
    assert len(state.attempts)==1
    assert state.reason=='interrupted_call'


def test_changed_input_enters_review_without_call(setup):
    store,spec=setup; engine=Engine(store,Vision([])); engine.add(spec)
    Image.new('RGB',(200,80)).save(spec.image_path)
    assert engine.run(spec.task_id).reason=='input_changed'


def test_task_identity_cannot_be_relabeled(setup):
    store,spec=setup; engine=Engine(store,Vision([])); engine.add(spec)
    with pytest.raises(ValueError): engine.add(spec.model_copy(update={'species':'伪造'}))


@pytest.mark.parametrize('reply', ['{"action":"accept"}', '{"action":"retry_once","species":"伪造"}', 'not json', '{"action":"needs_review"}'])
def test_invalid_or_review_planner_terminates_without_third_call(setup,reply):
    store,spec=setup
    engine=Engine(store,Vision([A,B]),planner=JsonPlanner(lambda context:reply)); engine.add(spec)
    state=engine.run(spec.task_id)
    assert state.phase=='needs_review' and len(state.attempts)==2
    assert state.planner_calls==1


def test_planner_only_called_for_exception_and_cannot_loop(setup):
    store,spec=setup; contexts=[]
    def choose(context): contexts.append(context); return '{"action":"retry_once"}'
    engine=Engine(store,Vision([A,B,C]),planner=JsonPlanner(choose)); engine.add(spec)
    state=engine.run(spec.task_id)
    assert state.phase=='needs_review' and state.planner_calls==1
    assert len(contexts)==1 and contexts[0]['remaining_localizations']==1


def test_planner_not_called_for_success(setup):
    store,spec=setup
    def forbidden(context): raise AssertionError('planner called on normal path')
    engine=Engine(store,Vision([A,A]),planner=JsonPlanner(forbidden)); engine.add(spec)
    assert engine.run(spec.task_id).phase=='done'
    assert store.get(spec.task_id).planner_calls==0


def test_policy_cannot_change_on_resume(setup):
    store,spec=setup; Engine(store,Vision([]),threshold=.8).add(spec)
    with pytest.raises(ValueError): Engine(store,Vision([]),threshold=.2).run(spec.task_id)


def test_terminal_export_failure_recovers_without_model_calls(setup,monkeypatch):
    store,spec=setup; engine=Engine(store,Vision([A,A])); engine.add(spec)
    export=store.export
    def fail(state): raise OSError('simulated disk failure')
    monkeypatch.setattr(store,'export',fail)
    with pytest.raises(OSError): engine.run(spec.task_id)
    assert store.get(spec.task_id).phase=='done'
    monkeypatch.setattr(store,'export',export)
    assert Engine(store,Vision([])).run(spec.task_id).phase=='done'
    assert (store.root/'results'/f'{spec.task_id}.json').is_file()


def test_audit_failure_rolls_back_state(setup):
    store,spec=setup; engine=Engine(store,Vision([])); engine.add(spec)
    before=store.get(spec.task_id)
    with store.connection() as db:
        db.execute("CREATE TRIGGER fail_audit BEFORE INSERT ON events BEGIN SELECT RAISE(ABORT,'disk failure'); END")
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        store.save(before.model_copy(update={'phase':'needs_review'}),'failure')
    assert store.get(spec.task_id)==before


def test_second_runner_cannot_take_lock(setup):
    store,_=setup
    with store.runner_lock():
        with pytest.raises((RuntimeError,BlockingIOError)):
            with Store(store.root).runner_lock(): pass


def test_visibility_error_is_terminal(setup):
    store,spec=setup
    class BadVisibility(Vision):
        def visibility(self, request): return {'route':'invented'}
    engine=Engine(store,BadVisibility([])); engine.add(spec)
    state=engine.run(spec.task_id)
    assert state.phase=='needs_review' and not state.attempts


def test_planner_exception_is_terminal(setup):
    store,spec=setup
    def fail(context): raise TimeoutError('provider offline')
    engine=Engine(store,Vision([A,B]),planner=JsonPlanner(fail)); engine.add(spec)
    state=engine.run(spec.task_id)
    assert state.phase=='needs_review' and len(state.attempts)==2


def test_target_limit_failure_consumes_attempt(setup):
    store,spec=setup
    oversized={'boxes':A['boxes']*2}
    engine=Engine(store,Vision([oversized,oversized,oversized]),max_targets=1); engine.add(spec)
    state=engine.run(spec.task_id)
    assert state.phase=='needs_review' and len(state.attempts)==3
    assert all(a.error=='ValueError' for a in state.attempts)
