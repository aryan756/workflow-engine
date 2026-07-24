"""Run / node status vocabulary."""

from __future__ import annotations

from enum import StrEnum


class NodeStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    WAITING_APPROVAL = "waiting_approval"
    CANCELLED = "cancelled"


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in {RunStatus.SUCCEEDED, RunStatus.CANCELLED}


class EdgeState(StrEnum):
    """How an incoming edge resolves for the target node."""

    ACTIVE = "active"  # source succeeded and this branch was taken
    PRUNED = "pruned"  # source skipped, or this branch was not taken
    BLOCKED = "blocked"  # source failed - target must wait for a retry
    UNRESOLVED = "unresolved"  # source has not finished yet
