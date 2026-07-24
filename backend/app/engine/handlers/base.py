"""Handler contract and the context handed to every node execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.provider import LLMProvider
from app.engine.definition import NodeDef, WorkflowDefinition
from app.engine.states import NodeStatus
from app.models import NodeLog, NodeRun, WorkflowRun
from app.tools.registry import ToolRegistry


@dataclass
class NodeResult:
    """What a handler returns on a successful attempt."""

    output: dict[str, Any] | None = None
    status: NodeStatus = NodeStatus.SUCCEEDED
    #: branch labels this node activates (branch nodes only)
    selected_labels: list[str] | None = None


class NodeLogger:
    """Buffers trace rows for one node attempt; the engine flushes them."""

    def __init__(self, session: AsyncSession, run_id: str, node_id: str, attempt: int) -> None:
        self._session = session
        self._run_id = run_id
        self._node_id = node_id
        self._attempt = attempt

    def log(self, level: str, message: str, payload: dict[str, Any] | None = None) -> None:
        self._session.add(
            NodeLog(
                run_id=self._run_id,
                node_id=self._node_id,
                attempt=self._attempt,
                level=level,
                message=message,
                payload_json=payload,
            )
        )

    def debug(self, message: str, payload: dict[str, Any] | None = None) -> None:
        self.log("debug", message, payload)

    def info(self, message: str, payload: dict[str, Any] | None = None) -> None:
        self.log("info", message, payload)

    def warning(self, message: str, payload: dict[str, Any] | None = None) -> None:
        self.log("warning", message, payload)

    def error(self, message: str, payload: dict[str, Any] | None = None) -> None:
        self.log("error", message, payload)


@dataclass
class ExecutionContext:
    run: WorkflowRun
    node_run: NodeRun
    node: NodeDef
    workflow: WorkflowDefinition
    inputs: dict[str, Any]
    session: AsyncSession
    provider: LLMProvider
    tools: ToolRegistry
    logger: NodeLogger
    attempt: int
    #: dev/test-only fault spec for this node, e.g.
    #: {"kind": "tool_after_side_effect", "times": 1}
    fault: dict[str, Any] | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def config(self) -> dict[str, Any]:
        return dict(self.node.config)

    def fault_active(self, kind: str) -> bool:
        """True while the configured fault still has firings left.

        The counter lives on the node_run row, so it survives across automatic
        retries *and* manual retries - a `times: 1` fault fires exactly once.
        """
        if not self.fault or self.fault.get("kind") != kind:
            return False
        times = int(self.fault.get("times", 1))
        return self.node_run.fault_count < times

    def consume_fault(self) -> None:
        self.node_run.fault_count += 1


class NodeHandler(Protocol):
    async def execute(self, ctx: ExecutionContext) -> NodeResult: ...
