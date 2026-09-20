from __future__ import annotations
from types import MappingProxyType
from typing import Protocol
from .models import Box, VisionRequest

class Vision(Protocol):
    def visibility(self, request: VisionRequest) -> dict: ...
    def localize(self, request: VisionRequest) -> dict: ...


def iou(a: Box, b: Box) -> float:
    x1,y1,x2,y2 = a.bbox
    u1,v1,u2,v2 = b.bbox
    intersection = max(0,min(x2,u2)-max(x1,u1))*max(0,min(y2,v2)-max(y1,v1))
    return intersection/((x2-x1)*(y2-y1)+(u2-u1)*(v2-v1)-intersection)


def consistent(left, right, threshold: float) -> bool:
    if not 0 < threshold <= 1:
        raise ValueError('threshold must be in (0,1]')
    if not left or len(left) != len(right):
        return False
    # Augmenting-path bipartite matching avoids order-dependent greedy failures.
    edges = [[j for j,b in enumerate(right) if iou(a,b) >= threshold] for a in left]
    matched = {}
    def assign(index, seen):
        for j in edges[index]:
            if j in seen:
                continue
            seen.add(j)
            if j not in matched or assign(matched[j],seen):
                matched[j] = index
                return True
        return False
    return all(assign(i,set()) for i in range(len(left)))


def pixels(box: Box, width: int, height: int) -> list[float]:
    if width <= 0 or height <= 0:
        raise ValueError('positive image dimensions required')
    return [round(v*(width if i%2==0 else height)/1000,4) for i,v in enumerate(box.bbox)]

class ToolRegistry:
    """Fixed Python tool whitelist; there is no dynamic import or shell tool."""
    def __init__(self):
        self._tools = MappingProxyType({'iou':iou, 'consistent':consistent, 'pixels':pixels})

    def call(self, name: str, **arguments):
        if name not in self._tools:
            raise ValueError('tool not allowed')
        return self._tools[name](**arguments)
