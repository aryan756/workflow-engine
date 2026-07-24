"""Bundled workflow definitions."""

from __future__ import annotations

from app.engine.definition import WorkflowRegistry
from app.workflows.support_triage import SUPPORT_TRIAGE


def build_registry() -> WorkflowRegistry:
    registry = WorkflowRegistry()
    registry.register(SUPPORT_TRIAGE)
    return registry


__all__ = ["SUPPORT_TRIAGE", "build_registry"]
