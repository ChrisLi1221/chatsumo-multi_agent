# agents/types_shared.py
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List

Json = Dict[str, Any]

@dataclass
class Proposal:
    """A specialist's *suggested* change set (not yet executed)."""
    agent_id: str
    summary: str
    changes: List[Dict[str, Any]] = field(default_factory=list)  # tool/action/args...
    expected_effect: Dict[str, Any] = field(default_factory=dict)
    risks: List[str] = field(default_factory=list)
    validation_plan: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
