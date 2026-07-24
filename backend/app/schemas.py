"""API request/response models."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


# --- workflow definition ------------------------------------------------
class NodeDefOut(BaseModel):
    id: str
    type: str
    title: str
    description: str
    join: str
    max_attempts: int
    rank: int
    config: dict[str, Any]


class EdgeDefOut(BaseModel):
    source: str
    target: str
    label: str | None = None


class WorkflowOut(BaseModel):
    id: str
    name: str
    description: str
    version: str
    output_node: str
    nodes: list[NodeDefOut]
    edges: list[EdgeDefOut]


# --- runs ---------------------------------------------------------------
class CreateRunRequest(BaseModel):
    workflow_id: str = "support_triage"
    input: dict[str, Any]
    #: dev/test only: {"faults": {"<node_id>": {"kind": "...", "times": 1}}}
    options: dict[str, Any] = Field(default_factory=dict)


class NodeRunOut(BaseModel):
    node_id: str
    node_type: str
    title: str
    status: str
    attempts: int
    max_attempts: int
    error: str | None = None
    error_code: str | None = None
    selected_labels: list[str] | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: float | None = None
    has_output: bool = False


class RunSummaryOut(BaseModel):
    id: str
    workflow_id: str
    status: str
    title: str | None = None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None


class ApprovalOut(BaseModel):
    id: str
    node_id: str
    status: str
    prompt: str
    context: dict[str, Any]
    note: str | None = None
    payload: dict[str, Any] | None = None
    decided_by: str | None = None
    decided_at: datetime | None = None
    created_at: datetime


class EdgeStateOut(BaseModel):
    source: str
    target: str
    label: str | None = None
    state: str


class RunDetailOut(RunSummaryOut):
    input: dict[str, Any]
    output: dict[str, Any] | None = None
    options: dict[str, Any] = Field(default_factory=dict)
    nodes: list[NodeRunOut]
    edges: list[EdgeStateOut]
    approvals: list[ApprovalOut]


class LogOut(BaseModel):
    id: int
    attempt: int
    level: str
    message: str
    payload: dict[str, Any] | None = None
    created_at: datetime


class ToolCallOut(BaseModel):
    id: str
    tool_name: str
    idempotency_key: str
    status: str
    attempt: int
    replayed_count: int
    request: dict[str, Any]
    response: dict[str, Any] | None = None
    error: str | None = None
    created_at: datetime
    completed_at: datetime | None = None


class NodeDetailOut(NodeRunOut):
    description: str = ""
    config: dict[str, Any] = Field(default_factory=dict)
    input: dict[str, Any] | None = None
    output: dict[str, Any] | None = None
    logs: list[LogOut] = Field(default_factory=list)
    tool_calls: list[ToolCallOut] = Field(default_factory=list)
    approval: ApprovalOut | None = None


class ApprovalDecisionRequest(BaseModel):
    decision: Literal["approve", "reject"]
    note: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    decided_by: str | None = None


class ActionResponse(BaseModel):
    ok: bool = True
    run_id: str
    message: str


class SystemInfoOut(BaseModel):
    version: str
    agent_provider: str
    agent_model: str | None = None
    tools: list[dict[str, Any]]
    workflows: list[str]
    fault_injection_enabled: bool = True


class SideEffectsOut(BaseModel):
    linear_issues: list[dict[str, Any]]
    sent_emails: list[dict[str, Any]]
    counts: dict[str, int]
