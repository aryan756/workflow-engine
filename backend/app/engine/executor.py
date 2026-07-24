"""The workflow engine.

Responsibilities:
  * materialise a run (one node_run row per node, all PENDING),
  * repeatedly ask :mod:`app.engine.scheduling` which nodes may run,
  * execute ready nodes concurrently, each in its own DB session,
  * persist status/IO/trace for every attempt,
  * expose retry / approve / resume so a stuck run can be driven forward.

All state lives in the database. The in-process pieces (locks, background
tasks) are pure optimisation - `resume_run` can pick a run back up after a
process restart because nothing important is held in memory.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.provider import LLMProvider
from app.engine.definition import WorkflowDefinition, WorkflowRegistry
from app.engine.errors import EngineError, NodeExecutionError, WorkflowDefinitionError
from app.engine.handlers import get_handler
from app.engine.handlers.base import ExecutionContext, NodeLogger
from app.engine.resolver import NodeView, ResolutionContext, resolve_inputs
from app.engine.scheduling import NodeState, determine_run_status, plan
from app.engine.states import NodeStatus, RunStatus
from app.models import ApprovalRequest, NodeLog, NodeRun, WorkflowRun, new_id, utcnow
from app.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


class RunNotFoundError(EngineError):
    pass


class NodeNotFoundError(EngineError):
    pass


class InvalidTransitionError(EngineError):
    pass


@dataclass
class _LockEntry:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    waiters: int = 0


class WorkflowEngine:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        workflows: WorkflowRegistry,
        provider: LLMProvider,
        tools: ToolRegistry,
        allow_fault_injection: bool = True,
    ) -> None:
        self._session_factory = session_factory
        self._workflows = workflows
        self._provider = provider
        self._tools = tools
        self._allow_fault_injection = allow_fault_injection
        self._locks: dict[str, _LockEntry] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self._validate_tool_bindings()

    def _validate_tool_bindings(self) -> None:
        """Fail at startup if a workflow names a tool nobody registered.

        Without this the typo surfaces only when a run reaches that node,
        which may be after upstream side effects have already happened.
        """
        known = {tool.name for tool in self._tools.list()}
        for definition in self._workflows.list():
            missing = sorted(definition.required_tools() - known)
            if missing:
                raise WorkflowDefinitionError(
                    f"workflow '{definition.id}' references unregistered tools: "
                    f"{missing} (registered: {sorted(known)})"
                )

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    @property
    def workflows(self) -> WorkflowRegistry:
        return self._workflows

    @property
    def provider(self) -> LLMProvider:
        return self._provider

    @property
    def tools(self) -> ToolRegistry:
        return self._tools

    async def create_run(
        self,
        workflow_id: str,
        payload: dict[str, Any],
        *,
        options: dict[str, Any] | None = None,
        title: str | None = None,
    ) -> str:
        definition = self._workflows.get(workflow_id)
        run_id = new_id("run")
        options = self._sanitize_options(options)

        async with self._session_factory() as session:
            run = WorkflowRun(
                id=run_id,
                workflow_id=definition.id,
                workflow_version=definition.version,
                status=RunStatus.PENDING.value,
                input_json=payload,
                options_json=options,
                title=title or _derive_title(payload),
            )
            session.add(run)
            # The unit of work orders inserts by *relationships*, and node_logs
            # deliberately has no ORM relationship to workflow_runs. Flush the
            # parent row first so the FK is satisfiable.
            await session.flush()
            for node in definition.nodes:
                session.add(
                    NodeRun(
                        id=new_id("nr"),
                        run_id=run_id,
                        node_id=node.id,
                        node_type=node.type.value,
                        title=node.title,
                        status=NodeStatus.PENDING.value,
                        max_attempts=node.max_attempts,
                    )
                )
            session.add(
                NodeLog(
                    run_id=run_id,
                    node_id="__run__",
                    attempt=0,
                    level="info",
                    message=f"Run created for workflow '{definition.id}'.",
                    payload_json={"input": payload, "options": options},
                )
            )
            await session.commit()

        return run_id

    def _sanitize_options(self, options: dict[str, Any] | None) -> dict[str, Any]:
        options = dict(options or {})
        if options.get("faults") and not self._allow_fault_injection:
            logger.warning("Fault injection is disabled; dropping 'faults' from run options.")
            options.pop("faults")
        return options

    @property
    def fault_injection_enabled(self) -> bool:
        return self._allow_fault_injection

    def schedule(self, run_id: str) -> asyncio.Task[None]:
        """Drive a run forward in the background."""
        task = asyncio.create_task(self._advance_guarded(run_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def wait_for_idle(self) -> None:
        """Await all in-flight background advances (used by tests)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def advance(self, run_id: str) -> None:
        """Run the scheduling loop until nothing else can be executed."""
        definition = await self._definition_for(run_id)
        guard = len(definition.nodes) * 4 + 50

        async with self._run_lock(run_id):
            for _ in range(guard):
                progressed = await self._advance_once(run_id, definition)
                if not progressed:
                    return
            logger.error("advance() guard tripped for run %s", run_id)

    async def retry_node(self, run_id: str, node_id: str) -> None:
        async with self._run_lock(run_id), self._session_factory() as session:
            run = await self._get_run(session, run_id)
            node_run = await self._get_node_run(session, run_id, node_id)
            status = NodeStatus(node_run.status)
            if status is not NodeStatus.FAILED:
                raise InvalidTransitionError(
                    f"node '{node_id}' is {status.value}; only failed nodes can be retried"
                )

            session.add(
                NodeLog(
                    run_id=run_id,
                    node_id=node_id,
                    attempt=node_run.attempts,
                    level="info",
                    message="Manual retry requested; node reset to pending.",
                    payload_json={
                        "previous_error": node_run.error,
                        "previous_error_code": node_run.error_code,
                    },
                )
            )
            # An approval node being retried must open a *fresh* gate rather
            # than re-reading the decision that failed it.
            for request in (
                await session.execute(
                    select(ApprovalRequest).where(
                        ApprovalRequest.run_id == run_id,
                        ApprovalRequest.node_id == node_id,
                        ApprovalRequest.status != "superseded",
                    )
                )
            ).scalars():
                request.status = "superseded"

            node_run.status = NodeStatus.PENDING.value
            node_run.error = None
            node_run.error_code = None
            node_run.finished_at = None
            node_run.duration_ms = None

            run.status = RunStatus.RUNNING.value
            run.error = None
            run.finished_at = None
            await session.commit()

        await self.advance(run_id)

    async def submit_approval(
        self,
        run_id: str,
        node_id: str,
        *,
        decision: str,
        note: str | None = None,
        payload: dict[str, Any] | None = None,
        decided_by: str | None = None,
    ) -> None:
        if decision not in {"approve", "reject"}:
            raise InvalidTransitionError("decision must be 'approve' or 'reject'")

        async with self._run_lock(run_id), self._session_factory() as session:
            run = await self._get_run(session, run_id)
            node_run = await self._get_node_run(session, run_id, node_id)
            if NodeStatus(node_run.status) is not NodeStatus.WAITING_APPROVAL:
                raise InvalidTransitionError(
                    f"node '{node_id}' is {node_run.status}, not waiting for approval"
                )

            request = (
                await session.execute(
                    select(ApprovalRequest)
                    .where(
                        ApprovalRequest.run_id == run_id,
                        ApprovalRequest.node_id == node_id,
                        ApprovalRequest.status == "pending",
                    )
                    .order_by(ApprovalRequest.created_at.desc())
                )
            ).scalars().first()
            if request is None:
                raise InvalidTransitionError(
                    f"no pending approval request for node '{node_id}'"
                )

            request.status = "approved" if decision == "approve" else "rejected"
            request.decision = request.status
            request.note = note
            request.payload_json = payload or {}
            request.decided_by = decided_by or "operator"
            request.decided_at = utcnow()

            session.add(
                NodeLog(
                    run_id=run_id,
                    node_id=node_id,
                    attempt=node_run.attempts,
                    level="info",
                    message=f"Human decision recorded: {request.status}.",
                    payload_json={
                        "note": note,
                        "payload": payload or {},
                        "decided_by": request.decided_by,
                    },
                )
            )
            # Put the node back in the queue; the handler will observe the
            # decision on its next execution.
            node_run.status = NodeStatus.PENDING.value
            run.status = RunStatus.RUNNING.value
            run.finished_at = None
            await session.commit()

        await self.advance(run_id)

    async def resume_run(self, run_id: str) -> None:
        """Re-drive a run, recovering nodes left RUNNING by a crash."""
        async with self._run_lock(run_id), self._session_factory() as session:
            run = await self._get_run(session, run_id)
            node_runs = await self._list_node_runs(session, run_id)
            recovered = [n for n in node_runs if n.status == NodeStatus.RUNNING.value]
            for node_run in recovered:
                node_run.status = NodeStatus.PENDING.value
                session.add(
                    NodeLog(
                        run_id=run_id,
                        node_id=node_run.node_id,
                        attempt=node_run.attempts,
                        level="warning",
                        message="Node was left running by an interrupted process; "
                        "reset to pending on resume.",
                    )
                )
            if RunStatus(run.status).is_terminal and not recovered:
                return
            run.status = RunStatus.RUNNING.value
            run.finished_at = None
            await session.commit()

        await self.advance(run_id)

    async def cancel_run(self, run_id: str) -> None:
        async with self._run_lock(run_id), self._session_factory() as session:
            run = await self._get_run(session, run_id)
            for node_run in await self._list_node_runs(session, run_id):
                if NodeStatus(node_run.status) in (
                    NodeStatus.PENDING,
                    NodeStatus.WAITING_APPROVAL,
                    NodeStatus.RUNNING,
                ):
                    node_run.status = NodeStatus.CANCELLED.value
            run.status = RunStatus.CANCELLED.value
            run.finished_at = utcnow()
            await session.commit()

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    @asynccontextmanager
    async def _run_lock(self, run_id: str) -> AsyncIterator[None]:
        """Serialise all mutations of one run.

        Ref-counted so the map doesn't grow one dead entry per run forever.
        The entry is dropped only when nobody holds *or waits for* it, which is
        what keeps two callers from ever ending up on different lock objects
        for the same run.
        """
        entry = self._locks.get(run_id)
        if entry is None:
            entry = _LockEntry()
            self._locks[run_id] = entry
        entry.waiters += 1
        try:
            async with entry.lock:
                yield
        finally:
            entry.waiters -= 1
            if entry.waiters == 0 and self._locks.get(run_id) is entry:
                del self._locks[run_id]

    async def _advance_guarded(self, run_id: str) -> None:
        try:
            await self.advance(run_id)
        except Exception:
            logger.exception("run %s failed to advance", run_id)

    async def _definition_for(self, run_id: str) -> WorkflowDefinition:
        async with self._session_factory() as session:
            run = await self._get_run(session, run_id)
            return self._workflows.get(run.workflow_id)

    async def _advance_once(self, run_id: str, definition: WorkflowDefinition) -> bool:
        """One scheduling round. Returns True if it made progress."""
        async with self._session_factory() as session:
            run = await self._get_run(session, run_id)
            if RunStatus(run.status) is RunStatus.CANCELLED:
                return False

            node_runs = await self._list_node_runs(session, run_id)
            states = _states_from(node_runs)
            current = plan(definition, states)

            if current.to_skip:
                by_id = {n.node_id: n for n in node_runs}
                for node_id in current.to_skip:
                    node_run = by_id[node_id]
                    node_run.status = NodeStatus.SKIPPED.value
                    node_run.finished_at = utcnow()
                    session.add(
                        NodeLog(
                            run_id=run_id,
                            node_id=node_id,
                            attempt=node_run.attempts,
                            level="info",
                            message="Branch not taken; node skipped.",
                        )
                    )
                run.status = RunStatus.RUNNING.value
                await session.commit()
                return True

            if not current.ready:
                status = determine_run_status(definition, states, current)
                self._finalize(run, node_runs, definition, status)
                await session.commit()
                return False

            ready = list(current.ready)
            if RunStatus(run.status) is not RunStatus.RUNNING:
                run.status = RunStatus.RUNNING.value
            if run.started_at is None:
                run.started_at = utcnow()
            run.finished_at = None
            await session.commit()

        # Ready nodes are independent by construction, so run them together.
        await asyncio.gather(
            *(self._execute_node(run_id, node_id, definition) for node_id in ready)
        )
        return True

    def _finalize(
        self,
        run: WorkflowRun,
        node_runs: list[NodeRun],
        definition: WorkflowDefinition,
        status: RunStatus,
    ) -> None:
        run.status = status.value
        if status is RunStatus.SUCCEEDED:
            output_node = next(
                (n for n in node_runs if n.node_id == definition.output_node), None
            )
            run.output_json = output_node.output_json if output_node else None
            run.error = None
            run.finished_at = utcnow()
        elif status is RunStatus.FAILED:
            failures = [n for n in node_runs if n.status == NodeStatus.FAILED.value]
            if failures:
                run.error = "; ".join(
                    f"{n.node_id}: {n.error_code or 'error'} - {n.error}" for n in failures
                )
            else:
                run.error = "run cannot make progress (no runnable nodes remain)"
            run.finished_at = utcnow()
        elif status is RunStatus.WAITING_APPROVAL:
            run.finished_at = None

    async def _execute_node(
        self, run_id: str, node_id: str, definition: WorkflowDefinition
    ) -> None:
        node = definition.node(node_id)

        async with self._session_factory() as session:
            run = await self._get_run(session, run_id)
            node_run = await self._get_node_run(session, run_id, node_id)
            if NodeStatus(node_run.status) is not NodeStatus.PENDING:
                return  # another pass already claimed it

            node_run.status = NodeStatus.RUNNING.value
            node_run.started_at = utcnow()
            await session.commit()

            fault = (run.options_json or {}).get("faults", {}).get(node_id)
            handler = get_handler(node.type)
            started = time.perf_counter()
            last_error: NodeExecutionError | None = None

            for local_attempt in range(node.max_attempts):
                node_run.attempts += 1
                attempt_no = node_run.attempts
                node_logger = NodeLogger(session, run_id, node_id, attempt_no)

                try:
                    resolution = await self._resolution_context(session, run, definition)
                    inputs = resolve_inputs(node.config.get("inputs"), resolution)
                    node_run.input_json = inputs

                    node_logger.info(
                        f"Attempt {attempt_no} started.",
                        {"node_type": node.type.value, "inputs": inputs},
                    )

                    ctx = ExecutionContext(
                        run=run,
                        node_run=node_run,
                        node=node,
                        workflow=definition,
                        inputs=inputs,
                        session=session,
                        provider=self._provider,
                        tools=self._tools,
                        logger=node_logger,
                        attempt=attempt_no,
                        fault=fault,
                    )
                    result = await handler.execute(ctx)

                except NodeExecutionError as exc:
                    last_error = exc
                    node_logger.error(
                        f"Attempt {attempt_no} failed: {exc}",
                        {"code": exc.code, "retryable": exc.retryable, **exc.details},
                    )
                    will_retry = exc.retryable and local_attempt < node.max_attempts - 1
                    await _safe_commit(session)
                    if will_retry:
                        if node.retry_backoff_seconds:
                            await asyncio.sleep(
                                node.retry_backoff_seconds * (2**local_attempt)
                            )
                        continue
                    break

                except Exception as exc:
                    last_error = NodeExecutionError(
                        f"unhandled {type(exc).__name__}: {exc}",
                        code="internal_error",
                        retryable=False,
                    )
                    logger.exception("node %s/%s crashed", run_id, node_id)
                    node_logger.error(f"Attempt {attempt_no} crashed: {exc!r}")
                    await _safe_commit(session)
                    break

                else:
                    duration = (time.perf_counter() - started) * 1000
                    node_run.status = result.status.value
                    node_run.output_json = result.output
                    node_run.selected_labels = result.selected_labels
                    node_run.error = None
                    node_run.error_code = None
                    if result.status is NodeStatus.SUCCEEDED:
                        node_run.finished_at = utcnow()
                        node_run.duration_ms = duration
                        node_logger.info(
                            f"Attempt {attempt_no} succeeded.",
                            {"duration_ms": round(duration, 2)},
                        )
                    else:
                        node_logger.info(f"Node parked in state '{result.status.value}'.")
                    await _safe_commit(session)
                    return

            # every attempt failed
            duration = (time.perf_counter() - started) * 1000
            assert last_error is not None
            await self._persist_failure(run_id, node_id, last_error, duration)

    async def _persist_failure(
        self, run_id: str, node_id: str, error: NodeExecutionError, duration_ms: float
    ) -> None:
        """Written in a fresh session so a poisoned session can't lose the status."""
        async with self._session_factory() as session:
            node_run = await self._get_node_run(session, run_id, node_id)
            node_run.status = NodeStatus.FAILED.value
            node_run.error = str(error)
            node_run.error_code = error.code
            node_run.finished_at = utcnow()
            node_run.duration_ms = duration_ms
            session.add(
                NodeLog(
                    run_id=run_id,
                    node_id=node_id,
                    attempt=node_run.attempts,
                    level="error",
                    message=f"Node failed after {node_run.attempts} attempt(s).",
                    payload_json={"code": error.code, "retryable": error.retryable},
                )
            )
            await session.commit()

    async def _resolution_context(
        self, session: AsyncSession, run: WorkflowRun, definition: WorkflowDefinition
    ) -> ResolutionContext:
        node_runs = await self._list_node_runs(session, run.id)
        return ResolutionContext(
            run_id=run.id,
            workflow_id=definition.id,
            run_input=run.input_json or {},
            nodes={
                n.node_id: NodeView(status=n.status, output=n.output_json) for n in node_runs
            },
        )

    # -- small data helpers ---------------------------------------------
    async def _get_run(self, session: AsyncSession, run_id: str) -> WorkflowRun:
        run = (
            await session.execute(select(WorkflowRun).where(WorkflowRun.id == run_id))
        ).scalar_one_or_none()
        if run is None:
            raise RunNotFoundError(f"run '{run_id}' not found")
        return run

    async def _get_node_run(
        self, session: AsyncSession, run_id: str, node_id: str
    ) -> NodeRun:
        node_run = (
            await session.execute(
                select(NodeRun).where(NodeRun.run_id == run_id, NodeRun.node_id == node_id)
            )
        ).scalar_one_or_none()
        if node_run is None:
            raise NodeNotFoundError(f"node '{node_id}' not found on run '{run_id}'")
        return node_run

    async def _list_node_runs(self, session: AsyncSession, run_id: str) -> list[NodeRun]:
        return list(
            (
                await session.execute(select(NodeRun).where(NodeRun.run_id == run_id))
            ).scalars()
        )


def _states_from(node_runs: list[NodeRun]) -> dict[str, NodeState]:
    return {
        n.node_id: NodeState(
            status=NodeStatus(n.status),
            selected_labels=tuple(n.selected_labels or ()),
        )
        for n in node_runs
    }


async def _safe_commit(session: AsyncSession) -> None:
    try:
        await session.commit()
    except Exception:
        logger.exception("commit failed; rolling back")
        await session.rollback()


def _derive_title(payload: dict[str, Any]) -> str:
    subject = str(payload.get("subject") or "").strip()
    if subject:
        return subject[:120]
    message = str(payload.get("message") or "").strip()
    return (message[:80] + "...") if message else "Untitled run"


__all__ = [
    "InvalidTransitionError",
    "NodeNotFoundError",
    "RunNotFoundError",
    "WorkflowEngine",
]
