"""Composite annotations, with incomplete samples isolated from training outputs."""
from .tools import pixels

def result_payload(state):
    spec = state.spec
    return {
        'workflow': 'agentized_metadata_v2', 'source_image': spec.source_image,
        'image_width': spec.width, 'image_height': spec.height,
        'metadata': {'species': spec.species, 'source': spec.species_source,
                     'provenance_sha256': spec.provenance_sha256},
        'coordinate_system': 'qwen_0_999', 'pixel_scale_denominator': 1000,
        'visibility_route': state.route, 'selected_attempt': None,
        'policy': state.policy.model_dump(), 'complete': state.phase == 'done',
        'targets': [target.model_dump(mode='json') for target in state.targets],
        'detections': [{'target_id': target.target_id, 'species': spec.species,
                        'bbox': list(target.selected_box.bbox),
                        'bbox_pixel': pixels(target.selected_box, spec.width, spec.height)}
                       for target in state.targets if target.status == 'accepted'],
    }
