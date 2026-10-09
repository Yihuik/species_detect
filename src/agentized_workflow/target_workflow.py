"""Version 2: two global passes and one contextual review per unresolved target."""
from pathlib import Path
from uuid import uuid4
from .metadata import digest
from .models import Attempt, Localization, Policy, TargetReview, TargetReviewRequest, Visibility, VisionRequest
from .target_matching import apply_carapace_flags, crop_region, map_crop_result, pair_targets, resolve_review
from .workflow import Engine

TERMINAL = {'done', 'needs_review', 'partial_review'}

class TargetEngine(Engine):
    def __init__(self, store, vision, *, threshold=.75, max_targets=10):
        super().__init__(store, vision, threshold=threshold, max_targets=max_targets)
        self.policy = Policy(threshold=threshold, max_targets=max_targets, version=2)

    def _review(self, state, reason):
        targets = tuple(target.model_copy(update={'status': 'needs_review', 'reason': reason})
                        if target.status == 'pending' else target for target in state.targets)
        phase = 'partial_review' if any(target.status == 'accepted' for target in targets) else 'needs_review'
        return self._save(state, 'needs_review', phase=phase, reason=reason, targets=targets, in_flight=None)

    def step(self, task_id):
        with self.store.runner_lock():
            state = self.store.get(task_id)
            if state.policy != self.policy:
                raise ValueError('resume must use original policy')
            if state.phase in TERMINAL:
                self.store.export(state); return state
            if state.in_flight:
                state = self._review(state, 'interrupted_call')
            else:
                try:
                    unchanged = digest(Path(state.spec.image_path)) == state.spec.image_sha256
                except OSError:
                    unchanged = False
                state = self._advance(state) if unchanged else self._review(state, 'input_changed')
            if state.phase in TERMINAL:
                self.store.export(state)
            return state

    def _advance(self, state):
        if state.phase == 'visibility':
            request = VisionRequest(task=state.spec, request_id=uuid4().hex, pass_number=0,
                                    max_targets=self.policy.max_targets)
            state = self._save(state, 'visibility_intent', in_flight=request.request_id)
            try:
                route = Visibility.model_validate(self.vision.visibility(request)).route
            except Exception as exc:
                return self._review(state, 'visibility_error:'+type(exc).__name__)
            return self._save(state, 'visibility_result', route=route, phase='locate', in_flight=None)
        if state.phase == 'locate':
            number = len(state.attempts)+1
            if number > 2:
                return self._review(state, 'global_budget_exhausted')
            request = VisionRequest(task=state.spec, request_id=uuid4().hex, pass_number=number,
                                    route=state.route, max_targets=self.policy.max_targets)
            attempt = Attempt(number=number, request_id=request.request_id)
            state = self._save(state, 'localization_intent', attempts=(*state.attempts, attempt), in_flight=request.request_id)
            try:
                result = Localization.model_validate(self.vision.localize(request))
                if len(result.boxes) > self.policy.max_targets:
                    raise ValueError('too many targets')
                attempt = attempt.model_copy(update={'result': result})
            except Exception as exc:
                attempt = attempt.model_copy(update={'error': type(exc).__name__})
            return self._save(state, 'localization_result', attempts=(*state.attempts[:-1], attempt),
                              phase='locate' if number == 1 else 'compare', in_flight=None)
        if state.phase == 'compare':
            if any(attempt.result is None for attempt in state.attempts):
                return self._review(state, 'localization_error')
            left, right = (attempt.result.boxes for attempt in state.attempts)
            targets = pair_targets(left, right, self.policy.threshold)
            state = self._save(state, 'targets_compared', targets=targets, phase='target_review')
            if not targets:
                return self._review(state, 'empty_localizations')
            # Reaching the cap does not establish that all visible targets were covered.
            if len(left) == self.policy.max_targets or len(right) == self.policy.max_targets:
                return self._review(state, 'target_budget_reached')
            return state
        if state.phase == 'target_review':
            target = next((target for target in state.targets if target.status == 'pending'), None)
            if target is None:
                if all(target.status == 'accepted' for target in state.targets):
                    return self._save(state, 'done', phase='done', reason='all_targets_consistent', in_flight=None)
                reason = ('carapace_asymmetry' if any(t.reason == 'carapace_asymmetry' for t in state.targets)
                          else 'unresolved_targets')
                return self._review(state, reason)
            region = crop_region(target, state.spec)
            request = TargetReviewRequest(task=state.spec, request_id=uuid4().hex,
                target_id=target.target_id, region=region, max_targets=self.policy.max_targets)
            review = TargetReview(request_id=request.request_id, region=region)
            targets = tuple(t.model_copy(update={'review': review}) if t.target_id == target.target_id else t for t in state.targets)
            state = self._save(state, 'target_review_intent', targets=targets, in_flight=request.request_id)
            box = pair = score = None
            result = None
            try:
                local = Localization.model_validate(self.vision.review_target(request))
                if len(local.boxes) > self.policy.max_targets:
                    raise ValueError('too many crop targets')
                result = map_crop_result(local, region, state.spec)
                review = review.model_copy(update={'result': result})
                box, pair, score, reason = resolve_review(target, result, state.targets, self.policy.threshold)
            except Exception as exc:
                reason = 'target_review_error:'+type(exc).__name__
                review = review.model_copy(update={'error': type(exc).__name__})
            updated = target.model_copy(update={'review': review, 'selected_box': box,
                'accepted_pair': pair, 'accepted_iou': score,
                'status': 'accepted' if box is not None else 'needs_review', 'reason': reason})
            targets = tuple(updated if t.target_id == target.target_id else t for t in state.targets)
            if result is not None:
                targets = apply_carapace_flags(targets, result.boxes)
            return self._save(state, 'target_review_result', targets=targets, in_flight=None)
        return self._review(state, 'invalid_state')

    def run(self, task_id):
        for _ in range(10 + self.policy.max_targets):
            state = self.step(task_id)
            if state.phase in TERMINAL:
                return state
        raise RuntimeError('target state machine step bound exceeded')
