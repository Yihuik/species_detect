from __future__ import annotations
import math
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator

Route = Literal['whole_or_mostly_visible', 'partially_visible']

class Contract(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, allow_inf_nan=False)

class Box(Contract):
    bbox: tuple[float, float, float, float]

    @field_validator('bbox', mode='before')
    @classmethod
    def validate_box(cls, value):
        if not isinstance(value, (list, tuple)) or len(value) != 4:
            raise ValueError('bbox must have four numbers')
        if any(isinstance(x, bool) or not isinstance(x, (float, int)) for x in value):
            raise ValueError('bbox must contain numbers')
        if any(not math.isfinite(x) or not 0 <= x <= 999 for x in value):
            raise ValueError('bbox outside qwen_0_999')
        if value[2] <= value[0] or value[3] <= value[1]:
            raise ValueError('bbox must have positive area')
        return value

class Localization(Contract):
    coordinate_system: Literal['qwen_0_999'] = 'qwen_0_999'
    boxes: tuple[Box, ...] = Field(max_length=100)

class Visibility(Contract):
    route: Route

class TaskSpec(Contract):
    task_id: str = Field(pattern=r'^[a-f0-9]{24}$')
    image_path: str
    source_image: str
    image_sha256: str
    species: str = Field(min_length=1)
    species_source: Literal['metadata_csv', 'trusted_directory']
    provenance_sha256: str
    width: int = Field(gt=0)
    height: int = Field(gt=0)

class VisionRequest(Contract):
    task: TaskSpec
    request_id: str
    pass_number: int = Field(ge=0, le=3)
    route: Route | None = None
    max_targets: int = Field(ge=1, le=100)

class Decision(Contract):
    action: Literal['retry_once', 'needs_review']

class Policy(Contract):
    threshold: float = Field(default=.7, gt=0, le=1)
    max_targets: int = Field(default=10, ge=1, le=100)
    version: Literal[1] = 1

class Attempt(Contract):
    number: int = Field(ge=1, le=3)
    request_id: str
    result: Localization | None = None
    error: str | None = None

class TaskState(Contract):
    spec: TaskSpec
    policy: Policy
    phase: Literal['visibility','locate','compare','plan','done','needs_review'] = 'visibility'
    route: Route | None = None
    attempts: tuple[Attempt, ...] = Field(default=(), max_length=3)
    in_flight: str | None = None
    planner_calls: int = Field(default=0, ge=0, le=1)
    selected_attempt: int | None = None
    reason: str | None = None
