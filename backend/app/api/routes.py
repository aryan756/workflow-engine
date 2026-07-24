"""REST API for the workflow debugger UI."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select

from app import __version__
from app.api.deps import get_engine, get_settings_dep
from app.config import Settings
from app.db import session_scope
from app.engine.executor import (
    InvalidTransitionError,
    NodeNotFoundError,
    RunNotFoundError,
    WorkflowEngine,
)
from app.engine.scheduling import NodeState, edge_state
from app.engine.states import NodeStatus
from app.models import ApprovalRequest, NodeLog, NodeRun, ToolCall, WorkflowRun
from app.schemas import (
    ActionResponse,
    ApprovalDecisionRequest,
    ApprovalOut,
    CreateRunRequest,
    EdgeDefOut,
    EdgeStateOut,
    LogOut,
    NodeDefOut,
    NodeDetailOut,
    NodeRunOut,
    RunDetailOut,
    RunSummaryOut,
    SideEffectsOut,
    SystemInfoOut,
    ToolCallOut,
    WorkflowOut,
)
from app.tools.mock_tools import LEDGER

router = APIRouter(prefix="/api")


# ---------------------------------------------------------------- system
@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/system", response_model=SystemInfoOut)
async def system_info(
    engine: WorkflowEngine = Depends(get_engine),
    settings: Settings = Depends(get_settings_dep),
) -> SystemInfoOut:
    return SystemInfoOut(
        version=__version__,
        agent_provider=engine.provider.name,
        agent_model=(
            settings.anthropic_model if engine.provider.name == "claude" else "rule-based-v1"
        ),
        tools=[
            {
                "name": t.name,
                "description": t.description,
                "side_effecting": t.side_effecting,
            }
            for t in engine.tools.list()
        ],
        workflows=[w.id for w in engine.workflows.list()],
        fault_injection_enabled=engine.fault_injection_enabled,
    )


@router.get("/side-effects", response_model=SideEffectsOut)
async def side_effects() -> SideEffectsOut:
    """Everything the mock tools actually did - the idempotency evidence."""
    return SideEffectsOut(**LEDGER.snapshot())


@router.post("/side-effects/reset", response_model=dict)
async def reset_side_effects() -> dict[str, Any]:
    LEDGER.reset()
    return {"ok": True}


# -------------------------------------------------------------- workflows
def _workflow_out(definition) -> WorkflowOut:
    ranks = definition.ranks()
    return WorkflowOut(
        id=definition.id,
        name=definition.name,
        description=definition.description,
        version=definition.version,
        output_node=definition.output_node,
        nodes=[
            NodeDefOut(
                id=n.id,
                type=n.type.value,
                title=n.title,
                description=n.description,
                join=n.join.value,
                max_attempts=n.max_attempts,
                rank=ranks[n.id],
                config=dict(n.config),
            )
            for n in definition.nodes
        ],
        edges=[
            EdgeDefOut(source=e.source, target=e.target, label=e.label)
            for e in definition.edges
        ],
    )


@router.get("/workflows", response_model=list[WorkflowOut])
async def list_workflows(engine: WorkflowEngine = Depends(get_engine)) -> list[WorkflowOut]:
    return [_workflow_out(w) for w in engine.workflows.list()]


@router.get("/workflows/{workflow_id}", response_model=WorkflowOut)
async def get_workflow(
    workflow_id: str, engine: WorkflowEngine = Depends(get_engine)
) -> WorkflowOut:
    try:
        return _workflow_out(engine.workflows.get(workflow_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ------------------------------------------------------------------- runs
@router.post("/runs", response_model=RunSummaryOut, status_code=201)
async def create_run(
    body: CreateRunRequest, engine: WorkflowEngine = Depends(get_engine)
) -> RunSummaryOut:
    try:
        run_id = await engine.create_run(
            body.workflow_id, body.input, options=body.options
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"unknown workflow: {exc}") from exc

    engine.schedule(run_id)

    async with session_scope() as session:
        run = await _load_run(session, run_id)
        return _run_summary(run)


@router.get("/runs", response_model=list[RunSummaryOut])
async def list_runs(limit: int = Query(50, ge=1, le=200)) -> list[RunSummaryOut]:
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(WorkflowRun).order_by(WorkflowRun.created_at.desc()).limit(limit)
            )
        ).scalars()
        return [_run_summary(r) for r in rows]


@router.get("/runs/{run_id}", response_model=RunDetailOut)
async def get_run(
    run_id: str, engine: WorkflowEngine = Depends(get_engine)
) -> RunDetailOut:
    async with session_scope() as session:
        run = await _load_run(session, run_id)
        node_runs = await _load_node_runs(session, run_id)
        approvals = await _load_approvals(session, run_id)

        try:
            definition = engine.workflows.get(run.workflow_id)
        except KeyError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        states = {
            n.node_id: NodeState(
                status=NodeStatus(n.status),
                selected_labels=tuple(n.selected_labels or ()),
            )
            for n in node_runs
        }
        order = {node.id: i for i, node in enumerate(definition.nodes)}
        node_runs.sort(key=lambda n: order.get(n.node_id, 999))

        return RunDetailOut(
            **_run_summary(run).model_dump(),
            input=run.input_json or {},
            output=run.output_json,
            options=run.options_json or {},
            nodes=[_node_out(n) for n in node_runs],
            edges=[
                EdgeStateOut(
                    source=e.source,
                    target=e.target,
                    label=e.label,
                    state=edge_state(e, states).value,
                )
                for e in definition.edges
            ],
            approvals=[_approval_out(a) for a in approvals],
        )


@router.get("/runs/{run_id}/nodes/{node_id}", response_model=NodeDetailOut)
async def get_node(
    run_id: str, node_id: str, engine: WorkflowEngine = Depends(get_engine)
) -> NodeDetailOut:
    async with session_scope() as session:
        run = await _load_run(session, run_id)
        node_run = (
            await session.execute(
                select(NodeRun).where(NodeRun.run_id == run_id, NodeRun.node_id == node_id)
            )
        ).scalar_one_or_none()
        if node_run is None:
            raise HTTPException(status_code=404, detail="node not found on this run")

        logs = list(
            (
                await session.execute(
                    select(NodeLog)
                    .where(NodeLog.run_id == run_id, NodeLog.node_id == node_id)
                    .order_by(NodeLog.id.asc())
                )
            ).scalars()
        )
        tool_calls = list(
            (
                await session.execute(
                    select(ToolCall)
                    .where(ToolCall.run_id == run_id, ToolCall.node_id == node_id)
                    .order_by(ToolCall.created_at.asc())
                )
            ).scalars()
        )
        approval = (
            await session.execute(
                select(ApprovalRequest)
                .where(
                    ApprovalRequest.run_id == run_id, ApprovalRequest.node_id == node_id
                )
                .order_by(ApprovalRequest.created_at.desc())
            )
        ).scalars().first()

        try:
            node_def = engine.workflows.get(run.workflow_id).node(node_id)
            description, config = node_def.description, dict(node_def.config)
        except (KeyError, AttributeError):
            description, config = "", {}

        return NodeDetailOut(
            **_node_out(node_run).model_dump(),
            description=description,
            config=config,
            input=node_run.input_json,
            output=node_run.output_json,
            logs=[
                LogOut(
                    id=log.id,
                    attempt=log.attempt,
                    level=log.level,
                    message=log.message,
                    payload=log.payload_json,
                    created_at=log.created_at,
                )
                for log in logs
            ],
            tool_calls=[
                ToolCallOut(
                    id=tc.id,
                    tool_name=tc.tool_name,
                    idempotency_key=tc.idempotency_key,
                    status=tc.status,
                    attempt=tc.attempt,
                    replayed_count=tc.replayed_count,
                    request=tc.request_json or {},
                    response=tc.response_json,
                    error=tc.error,
                    created_at=tc.created_at,
                    completed_at=tc.completed_at,
                )
                for tc in tool_calls
            ],
            approval=_approval_out(approval) if approval else None,
        )


@router.get("/runs/{run_id}/logs", response_model=list[dict])
async def get_run_logs(run_id: str, limit: int = Query(500, ge=1, le=5000)) -> list[dict]:
    async with session_scope() as session:
        await _load_run(session, run_id)
        rows = (
            await session.execute(
                select(NodeLog)
                .where(NodeLog.run_id == run_id)
                .order_by(NodeLog.id.asc())
                .limit(limit)
            )
        ).scalars()
        return [
            {
                "id": log.id,
                "node_id": log.node_id,
                "attempt": log.attempt,
                "level": log.level,
                "message": log.message,
                "payload": log.payload_json,
                "created_at": log.created_at.isoformat(),
            }
            for log in rows
        ]


# ---------------------------------------------------------------- actions
@router.post("/runs/{run_id}/nodes/{node_id}/retry", response_model=ActionResponse)
async def retry_node(
    run_id: str, node_id: str, engine: WorkflowEngine = Depends(get_engine)
) -> ActionResponse:
    try:
        await engine.retry_node(run_id, node_id)
    except (RunNotFoundError, NodeNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ActionResponse(run_id=run_id, message=f"node '{node_id}' retried")


@router.post("/runs/{run_id}/nodes/{node_id}/approve", response_model=ActionResponse)
async def submit_approval(
    run_id: str,
    node_id: str,
    body: ApprovalDecisionRequest,
    engine: WorkflowEngine = Depends(get_engine),
) -> ActionResponse:
    try:
        await engine.submit_approval(
            run_id,
            node_id,
            decision=body.decision,
            note=body.note,
            payload=body.payload,
            decided_by=body.decided_by,
        )
    except (RunNotFoundError, NodeNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ActionResponse(run_id=run_id, message=f"approval '{body.decision}' recorded")


@router.post("/runs/{run_id}/resume", response_model=ActionResponse)
async def resume_run(
    run_id: str, engine: WorkflowEngine = Depends(get_engine)
) -> ActionResponse:
    try:
        await engine.resume_run(run_id)
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return ActionResponse(run_id=run_id, message="run resumed")


@router.post("/runs/{run_id}/cancel", response_model=ActionResponse)
async def cancel_run(
    run_id: str, engine: WorkflowEngine = Depends(get_engine)
) -> ActionResponse:
    try:
        await engine.cancel_run(run_id)
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return ActionResponse(run_id=run_id, message="run cancelled")


# ---------------------------------------------------------------- helpers
async def _load_run(session, run_id: str) -> WorkflowRun:
    run = (
        await session.execute(select(WorkflowRun).where(WorkflowRun.id == run_id))
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return run


async def _load_node_runs(session, run_id: str) -> list[NodeRun]:
    return list(
        (await session.execute(select(NodeRun).where(NodeRun.run_id == run_id))).scalars()
    )


async def _load_approvals(session, run_id: str) -> list[ApprovalRequest]:
    return list(
        (
            await session.execute(
                select(ApprovalRequest)
                .where(ApprovalRequest.run_id == run_id)
                .order_by(ApprovalRequest.created_at.asc())
            )
        ).scalars()
    )


def _run_summary(run: WorkflowRun) -> RunSummaryOut:
    return RunSummaryOut(
        id=run.id,
        workflow_id=run.workflow_id,
        status=run.status,
        title=run.title,
        created_at=run.created_at,
        updated_at=run.updated_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
        error=run.error,
    )


def _node_out(node_run: NodeRun) -> NodeRunOut:
    return NodeRunOut(
        node_id=node_run.node_id,
        node_type=node_run.node_type,
        title=node_run.title,
        status=node_run.status,
        attempts=node_run.attempts,
        max_attempts=node_run.max_attempts,
        error=node_run.error,
        error_code=node_run.error_code,
        selected_labels=node_run.selected_labels,
        started_at=node_run.started_at,
        finished_at=node_run.finished_at,
        duration_ms=node_run.duration_ms,
        has_output=node_run.output_json is not None,
    )


def _approval_out(approval: ApprovalRequest) -> ApprovalOut:
    return ApprovalOut(
        id=approval.id,
        node_id=approval.node_id,
        status=approval.status,
        prompt=approval.prompt,
        context=approval.context_json or {},
        note=approval.note,
        payload=approval.payload_json,
        decided_by=approval.decided_by,
        decided_at=approval.decided_at,
        created_at=approval.created_at,
    )
