"""Persistence model.

Five tables carry the whole runtime:

  workflow_runs      one row per run, with the run-level input/output/status
  node_runs          one row per node per run - the durable state machine
  node_logs          append-only trace: every attempt, input, output, error
  tool_calls         idempotency ledger; a unique key guards side effects
  approval_requests  human-in-the-loop gates
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


class Base(DeclarativeBase):
    pass


class WorkflowRun(Base):
    __tablename__ = "workflow_runs"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workflow_id: Mapped[str] = mapped_column(String(80), index=True)
    workflow_version: Mapped[str] = mapped_column(String(20), default="1")
    status: Mapped[str] = mapped_column(String(24), index=True)

    input_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    output_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    options_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    title: Mapped[str | None] = mapped_column(String(200), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # lazy="raise": node rows are always queried explicitly (often filtered or
    # in a different session), so an implicit load here would be an unnoticed
    # extra query on every run fetch. Deletion is handled by the DB-level
    # ON DELETE CASCADE, with PRAGMA foreign_keys=ON.
    nodes: Mapped[list[NodeRun]] = relationship(
        back_populates="run", cascade="all, delete-orphan", lazy="raise"
    )


class NodeRun(Base):
    """Durable per-node state. The engine only ever transitions a node through
    this row, which is what makes resume-after-restart possible."""

    __tablename__ = "node_runs"
    __table_args__ = (
        UniqueConstraint("run_id", "node_id", name="uq_node_runs_run_node"),
        Index("ix_node_runs_run_status", "run_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("workflow_runs.id", ondelete="CASCADE"), index=True
    )
    node_id: Mapped[str] = mapped_column(String(80))
    node_type: Mapped[str] = mapped_column(String(24))
    title: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(24), index=True)

    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=1)
    fault_count: Mapped[int] = mapped_column(Integer, default=0)

    input_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    output_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    selected_labels: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)

    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(60), nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[float | None] = mapped_column(Float, nullable=True)

    run: Mapped[WorkflowRun] = relationship(back_populates="nodes")


class NodeLog(Base):
    """Append-only trace. Never mutated, so a retry keeps the failed attempt's
    evidence next to the successful one."""

    __tablename__ = "node_logs"
    __table_args__ = (Index("ix_node_logs_run_node", "run_id", "node_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("workflow_runs.id", ondelete="CASCADE"), index=True
    )
    node_id: Mapped[str] = mapped_column(String(80))
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    level: Mapped[str] = mapped_column(String(12))
    message: Mapped[str] = mapped_column(Text)
    payload_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ToolCall(Base):
    """Idempotency ledger for tool invocations.

    `idempotency_key` is a hash of (run, node, tool, canonical args). A retry
    that produces the same key short-circuits to the recorded response instead
    of re-running the side effect.
    """

    __tablename__ = "tool_calls"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_tool_calls_idempotency_key"),
        Index("ix_tool_calls_run_node", "run_id", "node_id"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("workflow_runs.id", ondelete="CASCADE"), index=True
    )
    node_id: Mapped[str] = mapped_column(String(80))
    tool_name: Mapped[str] = mapped_column(String(80))
    idempotency_key: Mapped[str] = mapped_column(String(80))

    status: Mapped[str] = mapped_column(String(20))  # in_progress | succeeded | failed
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    replayed_count: Mapped[int] = mapped_column(Integer, default=0)

    request_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    response_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ApprovalRequest(Base):
    __tablename__ = "approval_requests"
    __table_args__ = (Index("ix_approval_run_node", "run_id", "node_id"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("workflow_runs.id", ondelete="CASCADE"), index=True
    )
    node_id: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(20))  # pending | approved | rejected

    prompt: Mapped[str] = mapped_column(Text)
    context_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    decision: Mapped[str | None] = mapped_column(String(20), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
