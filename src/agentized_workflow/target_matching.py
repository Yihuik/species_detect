"""Conservative target correspondence and explicit crop coordinate transforms."""
import math
from .models import Box, Localization, TargetRecord, TaskSpec
from .tools import iou

ASSOCIATION_IOU = .25
CROP_PADDING = .25

def pair_targets(left, right, threshold):
    """Only isolated one-to-one overlap components establish target identity."""
    edges = {i: {j for j, b in enumerate(right) if iou(a, b) >= ASSOCIATION_IOU}
             for i, a in enumerate(left)}
    records = []
    visited_left, visited_right = set(), set()
    for start in range(len(left)):
        if start in visited_left:
            continue
        ls, rs = {start}, set()
        while True:
            expanded_right = rs | {j for i in ls for j in edges[i]}
            expanded_left = ls | {i for i in edges if edges[i] & expanded_right}
            if expanded_left == ls and expanded_right == rs:
                break
            ls, rs = expanded_left, expanded_right
        visited_left |= ls; visited_right |= rs
        if len(ls) == len(rs) == 1:
            i, j = next(iter(ls)), next(iter(rs))
            score = iou(left[i], right[j])
            accepted = score > threshold
            records.append((i, TargetRecord(target_id=f"target-{i+1:03d}",
                first_box=left[i], second_box=right[j], pair_iou=score,
                status='accepted' if accepted else 'pending',
                selected_box=right[j] if accepted else None,
                accepted_iou=score if accepted else None,
                accepted_pair='first_second' if accepted else None)))
        else:
            reason = 'correspondence_ambiguous' if rs else 'unmatched_target'
            for i in sorted(ls):
                records.append((i, TargetRecord(target_id=f"target-{i+1:03d}",
                    first_box=left[i], status='needs_review', reason=reason)))
            for j in sorted(rs):
                records.append((len(left)+j, TargetRecord(target_id=f"second-{j+1:03d}",
                    second_box=right[j], status='needs_review', reason=reason)))
    for j in range(len(right)):
        if j not in visited_right:
            records.append((len(left)+j, TargetRecord(target_id=f"second-{j+1:03d}",
                second_box=right[j], status='needs_review', reason='unmatched_target')))
    return tuple(record for _, record in sorted(records, key=lambda item: item[0]))

def crop_region(target: TargetRecord, spec: TaskSpec):
    a, b = target.first_box.bbox, target.second_box.bbox
    x, y, r, bottom = min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])
    pad_x, pad_y = (r-x)*CROP_PADDING, (bottom-y)*CROP_PADDING
    return (max(0, math.floor((x-pad_x)*spec.width/1000)),
            max(0, math.floor((y-pad_y)*spec.height/1000)),
            min(spec.width, math.ceil((r+pad_x)*spec.width/1000)),
            min(spec.height, math.ceil((bottom+pad_y)*spec.height/1000)))

def map_crop_result(result: Localization, region, spec: TaskSpec):
    x, y, r, bottom = region
    boxes = []
    for box in result.boxes:
        a, b, c, d = box.bbox
        boxes.append(Box(bbox=[min(999, value) for value in [(x+a*(r-x)/1000)*1000/spec.width,
                              (y+b*(bottom-y)/1000)*1000/spec.height,
                              (x+c*(r-x)/1000)*1000/spec.width,
                              (y+d*(bottom-y)/1000)*1000/spec.height]]))
    return Localization(boxes=tuple(boxes))

def resolve_review(target, result, records, threshold):
    """Require a unique target match and account for every returned crop box."""
    candidates = []
    other_boxes = [box for record in records if record.target_id != target.target_id
                   for box in (record.first_box, record.second_box) if box is not None]
    for box in result.boxes:
        first, second = iou(target.first_box, box), iou(target.second_box, box)
        if max(first, second) > threshold:
            if any(iou(box, other) >= ASSOCIATION_IOU for other in other_boxes):
                return None, None, None, 'review_identity_ambiguous'
            candidates.append((box, first, second))
        elif not any(iou(box, other) > threshold for other in other_boxes):
            return None, None, None, 'review_unmatched_box'
    if len(candidates) != 1:
        return None, None, None, 'review_empty_or_inconsistent' if not candidates else 'review_identity_ambiguous'
    box, first, second = candidates[0]
    pair, score = ('first_review', first) if first > threshold else ('second_review', second)
    return box, pair, score, None
