from typing import Callable, Protocol
from .models import Decision

class Planner(Protocol):
    def decide(self, context: dict) -> Decision: ...

class JsonPlanner:
    """The transport receives only bounded diagnostic data, never executable tools."""
    def __init__(self, transport: Callable[[dict], str]):
        self.transport = transport

    def decide(self, context: dict) -> Decision:
        return Decision.model_validate_json(self.transport(context))

class DeterministicPlanner:
    def decide(self, context: dict) -> Decision:
        return Decision(action='retry_once' if context['remaining_localizations']==1 else 'needs_review')
