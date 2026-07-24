"""Tool registry.

A tool is a named async callable plus metadata. `side_effecting` is what the
idempotency story hangs on: those are the tools whose replay must be prevented,
and the ones whose effects land in the :class:`SideEffectLedger`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

ToolHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    handler: ToolHandler
    side_effecting: bool = False
    required_args: tuple[str, ...] = ()


@dataclass
class SideEffectLedger:
    """Records every *real* side effect the mock tools performed.

    Tests (and the UI) assert against this to prove that retrying a node does
    not duplicate work.
    """

    linear_issues: list[dict[str, Any]] = field(default_factory=list)
    sent_emails: list[dict[str, Any]] = field(default_factory=list)

    def reset(self) -> None:
        self.linear_issues.clear()
        self.sent_emails.clear()

    def snapshot(self) -> dict[str, Any]:
        return {
            "linear_issues": list(self.linear_issues),
            "sent_emails": list(self.sent_emails),
            "counts": {
                "linear_issues": len(self.linear_issues),
                "sent_emails": len(self.sent_emails),
            },
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._items: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> ToolSpec:
        self._items[spec.name] = spec
        return spec

    def get(self, name: str) -> ToolSpec:
        if name not in self._items:
            raise KeyError(f"unknown tool '{name}'")
        return self._items[name]

    def list(self) -> list[ToolSpec]:
        return list(self._items.values())
