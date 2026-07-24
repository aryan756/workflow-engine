"""Unit tests for the pure scheduling core."""

from __future__ import annotations

import pytest

from app.engine.definition import (
    EdgeDef,
    JoinPolicy,
    NodeDef,
    NodeType,
    WorkflowDefinition,
)
from app.engine.errors import WorkflowDefinitionError
from app.engine.scheduling import NodeState, determine_run_status, edge_state, plan
from app.engine.states import EdgeState, NodeStatus, RunStatus
from app.workflows import SUPPORT_TRIAGE

AGENT_CONFIG = {"task": "finalize_response", "contract": "final_response"}


def _branch(*labels: str) -> NodeDef:
    return NodeDef(
        id="b",
        type=NodeType.BRANCH,
        title="b",
        config={
            "cases": [
                {"label": label, "when": {"var": "x", "op": "eq", "value": label}}
                for label in labels
            ],
            "default": "left",
        },
    )


def _tiny_dag() -> WorkflowDefinition:
    return WorkflowDefinition(
        id="tiny",
        name="tiny",
        description="",
        output_node="join",
        nodes=(
            NodeDef(id="start", type=NodeType.INPUT, title="start"),
            _branch("left", "right"),
            NodeDef(id="left", type=NodeType.TOOL, title="left", config={"tool": "t"}),
            NodeDef(id="right", type=NodeType.TOOL, title="right", config={"tool": "t"}),
            NodeDef(
                id="join",
                type=NodeType.AGENT,
                title="join",
                join=JoinPolicy.ANY,
                config=AGENT_CONFIG,
            ),
        ),
        edges=(
            EdgeDef("start", "b"),
            EdgeDef("b", "left", label="left"),
            EdgeDef("b", "right", label="right"),
            EdgeDef("left", "join"),
            EdgeDef("right", "join"),
        ),
    )


def test_definition_validates():
    _tiny_dag().validate()
    SUPPORT_TRIAGE.validate()


def test_cycle_is_rejected():
    definition = WorkflowDefinition(
        id="cyclic",
        name="cyclic",
        description="",
        output_node="a",
        nodes=(
            NodeDef(id="a", type=NodeType.INPUT, title="a"),
            NodeDef(id="b", type=NodeType.AGENT, title="b", config=AGENT_CONFIG),
        ),
        edges=(EdgeDef("a", "b"), EdgeDef("b", "a")),
    )
    with pytest.raises(WorkflowDefinitionError):
        definition.validate()


def test_unwired_branch_label_is_rejected():
    definition = WorkflowDefinition(
        id="bad",
        name="bad",
        description="",
        output_node="left",
        nodes=(
            _branch("left", "right"),
            NodeDef(id="left", type=NodeType.TOOL, title="l", config={"tool": "t"}),
        ),
        edges=(EdgeDef("b", "left", label="left"),),
    )
    with pytest.raises(WorkflowDefinitionError, match="unwired"):
        definition.validate()


# --- node config validation (catches typos at registration, not mid-run) ---
def _single_node(node: NodeDef) -> WorkflowDefinition:
    return WorkflowDefinition(
        id="one",
        name="one",
        description="",
        output_node=node.id,
        nodes=(node,),
        edges=(),
    )


def test_agent_node_with_unknown_contract_is_rejected():
    definition = _single_node(
        NodeDef(
            id="a",
            type=NodeType.AGENT,
            title="a",
            config={"task": "t", "contract": "no_such_contract"},
        )
    )
    with pytest.raises(WorkflowDefinitionError, match="unknown contract"):
        definition.validate()


def test_agent_node_without_a_task_is_rejected():
    definition = _single_node(
        NodeDef(id="a", type=NodeType.AGENT, title="a", config={"contract": "final_response"})
    )
    with pytest.raises(WorkflowDefinitionError, match="requires config key 'task'"):
        definition.validate()


def test_tool_node_without_a_tool_is_rejected():
    definition = _single_node(NodeDef(id="t", type=NodeType.TOOL, title="t"))
    with pytest.raises(WorkflowDefinitionError, match="requires config key 'tool'"):
        definition.validate()


def test_branch_with_an_unknown_operator_is_rejected():
    definition = _single_node(
        NodeDef(
            id="b",
            type=NodeType.BRANCH,
            title="b",
            config={
                "cases": [{"label": "x", "when": {"var": "a", "op": "approximately"}}],
                "default": "x",
            },
        )
    )
    with pytest.raises(WorkflowDefinitionError, match="unsupported operator"):
        definition.validate()


def test_branch_case_without_a_predicate_is_rejected():
    definition = _single_node(
        NodeDef(
            id="b",
            type=NodeType.BRANCH,
            title="b",
            config={"cases": [{"label": "x"}], "default": "x"},
        )
    )
    with pytest.raises(WorkflowDefinitionError, match="needs a 'when' predicate"):
        definition.validate()


def test_binary_operator_without_a_value_is_rejected():
    definition = _single_node(
        NodeDef(
            id="b",
            type=NodeType.BRANCH,
            title="b",
            config={
                "cases": [{"label": "x", "when": {"var": "a", "op": "eq"}}],
                "default": "x",
            },
        )
    )
    with pytest.raises(WorkflowDefinitionError, match="requires a 'value'"):
        definition.validate()


def test_engine_rejects_a_workflow_naming_an_unregistered_tool(session_factory):
    """A tool typo must be a startup error, not a mid-run failure."""
    from app.agents.mock import MockProvider
    from app.engine.definition import WorkflowRegistry
    from app.engine.executor import WorkflowEngine
    from app.tools.mock_tools import build_default_registry

    registry = WorkflowRegistry()
    registry.register(
        _single_node(
            NodeDef(id="t", type=NodeType.TOOL, title="t", config={"tool": "crm.fetch_custmer"})
        )
    )
    with pytest.raises(WorkflowDefinitionError, match="unregistered tools"):
        WorkflowEngine(
            session_factory=session_factory,
            workflows=registry,
            provider=MockProvider(),
            tools=build_default_registry(),
        )


def test_entry_node_is_immediately_ready():
    dag = _tiny_dag()
    states = {n.id: NodeState(NodeStatus.PENDING) for n in dag.nodes}
    assert plan(dag, states).ready == ["start"]


def test_untaken_branch_is_pruned_and_target_skipped():
    dag = _tiny_dag()
    states = {
        "start": NodeState(NodeStatus.SUCCEEDED),
        "b": NodeState(NodeStatus.SUCCEEDED, selected_labels=("left",)),
        "left": NodeState(NodeStatus.PENDING),
        "right": NodeState(NodeStatus.PENDING),
        "join": NodeState(NodeStatus.PENDING),
    }
    assert edge_state(EdgeDef("b", "left", "left"), states) is EdgeState.ACTIVE
    assert edge_state(EdgeDef("b", "right", "right"), states) is EdgeState.PRUNED

    result = plan(dag, states)
    assert result.ready == ["left"]
    assert result.to_skip == ["right"]


def test_any_join_becomes_ready_with_one_active_and_one_pruned():
    dag = _tiny_dag()
    states = {
        "start": NodeState(NodeStatus.SUCCEEDED),
        "b": NodeState(NodeStatus.SUCCEEDED, selected_labels=("left",)),
        "left": NodeState(NodeStatus.SUCCEEDED),
        "right": NodeState(NodeStatus.SKIPPED),
        "join": NodeState(NodeStatus.PENDING),
    }
    assert plan(dag, states).ready == ["join"]


def test_failed_node_blocks_rather_than_skips_downstream():
    """A failed node must leave its successors PENDING so a retry can resume."""
    dag = _tiny_dag()
    states = {
        "start": NodeState(NodeStatus.SUCCEEDED),
        "b": NodeState(NodeStatus.SUCCEEDED, selected_labels=("left",)),
        "left": NodeState(NodeStatus.FAILED),
        "right": NodeState(NodeStatus.SKIPPED),
        "join": NodeState(NodeStatus.PENDING),
    }
    result = plan(dag, states)
    assert result.ready == []
    assert result.to_skip == []
    assert "join" in result.blocked
    assert determine_run_status(dag, states) is RunStatus.FAILED


def test_waiting_approval_reports_run_waiting():
    dag = _tiny_dag()
    states = {
        "start": NodeState(NodeStatus.SUCCEEDED),
        "b": NodeState(NodeStatus.SUCCEEDED, selected_labels=("left",)),
        "left": NodeState(NodeStatus.WAITING_APPROVAL),
        "right": NodeState(NodeStatus.SKIPPED),
        "join": NodeState(NodeStatus.PENDING),
    }
    assert determine_run_status(dag, states) is RunStatus.WAITING_APPROVAL


def test_all_terminal_reports_success():
    dag = _tiny_dag()
    states = {
        "start": NodeState(NodeStatus.SUCCEEDED),
        "b": NodeState(NodeStatus.SUCCEEDED, selected_labels=("left",)),
        "left": NodeState(NodeStatus.SUCCEEDED),
        "right": NodeState(NodeStatus.SKIPPED),
        "join": NodeState(NodeStatus.SUCCEEDED),
    }
    assert determine_run_status(dag, states) is RunStatus.SUCCEEDED


def test_support_triage_ranks_are_layered():
    ranks = SUPPORT_TRIAGE.ranks()
    assert ranks["intake"] == 0
    assert ranks["classify"] == ranks["fetch_context"] == 1
    assert ranks["route"] == 2
    assert ranks["send_reply"] == max(ranks.values())
