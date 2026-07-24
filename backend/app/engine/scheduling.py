"""Pure scheduling logic.

Kept free of I/O so the trickiest part of the engine - "which nodes may run,
which are dead branches, and is the run finished?" - is directly unit-testable.

Edge semantics
--------------
active     source succeeded and (edge is unconditional OR the branch chose it)
pruned     source was skipped, or the branch chose a different label
blocked    source failed: the target must wait for a retry, NOT be skipped
unresolved source hasn't finished yet

Node semantics
--------------
join=all   ready when every incoming edge is active; skipped if any is pruned
join=any   ready when at least one is active; skipped only if all are pruned
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.engine.definition import EdgeDef, JoinPolicy, WorkflowDefinition
from app.engine.states import EdgeState, NodeStatus, RunStatus


@dataclass(frozen=True)
class NodeState:
    status: NodeStatus
    selected_labels: tuple[str, ...] = ()


@dataclass
class SchedulingPlan:
    ready: list[str] = field(default_factory=list)
    to_skip: list[str] = field(default_factory=list)
    waiting_approval: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    running: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)


def edge_state(edge: EdgeDef, states: dict[str, NodeState]) -> EdgeState:
    source = states.get(edge.source)
    if source is None:
        return EdgeState.UNRESOLVED

    if source.status is NodeStatus.SUCCEEDED:
        if edge.label is None:
            return EdgeState.ACTIVE
        return EdgeState.ACTIVE if edge.label in source.selected_labels else EdgeState.PRUNED

    if source.status is NodeStatus.SKIPPED:
        return EdgeState.PRUNED

    if source.status in (NodeStatus.FAILED, NodeStatus.CANCELLED):
        return EdgeState.BLOCKED

    return EdgeState.UNRESOLVED


def plan(definition: WorkflowDefinition, states: dict[str, NodeState]) -> SchedulingPlan:
    result = SchedulingPlan()

    for node in definition.nodes:
        state = states.get(node.id)
        status = state.status if state else NodeStatus.PENDING

        if status is NodeStatus.RUNNING:
            result.running.append(node.id)
            continue
        if status is NodeStatus.WAITING_APPROVAL:
            result.waiting_approval.append(node.id)
            continue
        if status is NodeStatus.FAILED:
            result.failed.append(node.id)
            continue
        if status is not NodeStatus.PENDING:
            continue  # succeeded / skipped / cancelled - nothing to do

        incoming = definition.incoming(node.id)
        if not incoming:
            result.ready.append(node.id)
            continue

        resolved = [edge_state(e, states) for e in incoming]

        if EdgeState.BLOCKED in resolved or EdgeState.UNRESOLVED in resolved:
            result.blocked.append(node.id)
            continue

        active = resolved.count(EdgeState.ACTIVE)
        if node.join is JoinPolicy.ALL:
            if active == len(resolved):
                result.ready.append(node.id)
            else:
                result.to_skip.append(node.id)
        else:  # ANY
            if active > 0:
                result.ready.append(node.id)
            else:
                result.to_skip.append(node.id)

    return result


def determine_run_status(
    definition: WorkflowDefinition,
    states: dict[str, NodeState],
    current: SchedulingPlan | None = None,
) -> RunStatus:
    """Called once no node is ready to run.

    Pass an already-computed plan to avoid recomputing it.
    """
    current = current if current is not None else plan(definition, states)

    if current.running or current.ready:
        return RunStatus.RUNNING
    if current.waiting_approval:
        return RunStatus.WAITING_APPROVAL
    if current.failed:
        return RunStatus.FAILED

    unfinished = [
        node.id
        for node in definition.nodes
        if (states.get(node.id) or NodeState(NodeStatus.PENDING)).status
        not in (NodeStatus.SUCCEEDED, NodeStatus.SKIPPED, NodeStatus.CANCELLED)
    ]
    if unfinished:
        # Nothing runnable, nothing failed, yet nodes remain: the DAG cannot
        # make progress. Surface it rather than hanging.
        return RunStatus.FAILED
    return RunStatus.SUCCEEDED
