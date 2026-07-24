"""Node handlers, one per node type."""

from __future__ import annotations

from app.engine.definition import NodeType
from app.engine.handlers.agent_node import AgentNodeHandler
from app.engine.handlers.approval_node import ApprovalNodeHandler
from app.engine.handlers.base import ExecutionContext, NodeHandler, NodeResult
from app.engine.handlers.branch_node import BranchNodeHandler
from app.engine.handlers.input_node import InputNodeHandler
from app.engine.handlers.tool_node import ToolNodeHandler

HANDLERS: dict[NodeType, NodeHandler] = {
    NodeType.INPUT: InputNodeHandler(),
    NodeType.AGENT: AgentNodeHandler(),
    NodeType.BRANCH: BranchNodeHandler(),
    NodeType.TOOL: ToolNodeHandler(),
    NodeType.APPROVAL: ApprovalNodeHandler(),
}


def get_handler(node_type: NodeType) -> NodeHandler:
    if node_type not in HANDLERS:
        raise KeyError(f"no handler registered for node type '{node_type}'")
    return HANDLERS[node_type]


__all__ = [
    "HANDLERS",
    "ExecutionContext",
    "NodeHandler",
    "NodeResult",
    "get_handler",
]
