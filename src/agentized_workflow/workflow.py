from __future__ import annotations
from pathlib import Path
import uuid
from .metadata import digest
from .models import Attempt, Decision, Localization, Policy, TaskSpec, TaskState, Visibility, VisionRequest
from .planner import DeterministicPlanner, Planner
from .storage import Store
from .tools import ToolRegistry, Vision

class Engine:
    def __init__(self, store: Store, vision: Vision, planner: Planner | None = None, *, threshold=.7, max_targets=10):
        self.store=store
        self.vision=vision
        self.planner=planner or DeterministicPlanner()
        self.policy=Policy(threshold=threshold,max_targets=max_targets)
        self.tools=ToolRegistry()

    def add(self, spec: TaskSpec):
        with self.store.runner_lock():
            try:
                existing=self.store.get(spec.task_id)
            except KeyError:
                self.store.save(TaskState(spec=spec,policy=self.policy),'created',{'spec':spec.model_dump()})
            else:
                if existing.spec != spec or existing.policy != self.policy:
                    raise ValueError('task provenance/policy changed; use a new run directory')

    def _save(self,state,event,**updates):
        state=state.model_copy(update=updates)
        self.store.save(state,event,{'state':state.model_dump(mode='json')})
        return state

    def _review(self,state,reason):
        return self._save(state,'needs_review',phase='needs_review',reason=reason,in_flight=None)

    def step(self, task_id: str) -> TaskState:
        with self.store.runner_lock():
            state=self.store.get(task_id)
            if state.policy != self.policy:
                raise ValueError('resume must use original policy')
            if state.phase in {'done','needs_review'}:
                self.store.export(state)
                return state
            if state.in_flight:
                state=self._review(state,'interrupted_call')
            else:
                try:
                    unchanged=digest(Path(state.spec.image_path))==state.spec.image_sha256
                except OSError:
                    unchanged=False
                if not unchanged:
                    state=self._review(state,'input_changed')
                else:
                    state=self._advance(state)
            if state.phase in {'done','needs_review'}:
                self.store.export(state)
            return state

    def _advance(self,state):
        if state.phase=='visibility':
            request=VisionRequest(task=state.spec,request_id=uuid.uuid4().hex,pass_number=0,max_targets=self.policy.max_targets)
            state=self._save(state,'visibility_intent',in_flight=request.request_id)
            try:
                route=Visibility.model_validate(self.vision.visibility(request)).route
            except Exception as exc:
                return self._review(state,'visibility_error:'+type(exc).__name__)
            return self._save(state,'visibility_result',route=route,phase='locate',in_flight=None)
        if state.phase=='locate':
            number=len(state.attempts)+1
            if number>3:
                return self._review(state,'budget_exhausted')
            request=VisionRequest(task=state.spec,request_id=uuid.uuid4().hex,pass_number=number,
                                 route=state.route,max_targets=self.policy.max_targets)
            attempt=Attempt(number=number,request_id=request.request_id)
            state=self._save(state,'localization_intent',attempts=(*state.attempts,attempt),in_flight=request.request_id)
            try:
                result=Localization.model_validate(self.vision.localize(request))
                if len(result.boxes)>self.policy.max_targets:
                    raise ValueError('too many targets')
                attempt=attempt.model_copy(update={'result':result})
            except Exception as exc:
                attempt=attempt.model_copy(update={'error':type(exc).__name__})
            return self._save(state,'localization_result',attempts=(*state.attempts[:-1],attempt),
                              phase='locate' if number<2 else 'compare',in_flight=None)
        if state.phase=='compare':
            latest=state.attempts[-1]
            if latest.result:
                for earlier in state.attempts[:-1]:
                    if earlier.result and self.tools.call('consistent',left=earlier.result.boxes,
                            right=latest.result.boxes,threshold=self.policy.threshold):
                        return self._save(state,'done',phase='done',selected_attempt=latest.number,
                                          reason=f'consistent_with_attempt_{earlier.number}')
            if len(state.attempts)>=3:
                return self._review(state,'no_consistent_pair')
            return self._save(state,'consistency_failed',phase='plan')
        if state.phase=='plan':
            if state.planner_calls or len(state.attempts)!=2:
                return self._review(state,'planner_budget_exhausted')
            context={'allowed_actions':['retry_once','needs_review'],'remaining_localizations':1,
                     'attempts':[{'number':a.number,'error':a.error,
                        'box_count':len(a.result.boxes) if a.result else None} for a in state.attempts],
                     'reason':'no_consistent_pair','iou_threshold':self.policy.threshold}
            state=self._save(state,'planner_intent',planner_calls=1,in_flight='planner:'+uuid.uuid4().hex)
            try:
                decision=Decision.model_validate(self.planner.decide(context))
            except Exception as exc:
                return self._review(state,'planner_error:'+type(exc).__name__)
            if decision.action=='needs_review':
                return self._review(state,'planner_requested_review')
            return self._save(state,'retry_once',phase='locate',in_flight=None)
        return self._review(state,'invalid_state')

    def run(self,task_id: str) -> TaskState:
        for _ in range(16):
            state=self.step(task_id)
            if state.phase in {'done','needs_review'}:
                return state
        raise RuntimeError('state machine step bound exceeded')
