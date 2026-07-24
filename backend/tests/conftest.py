from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.mock import MockProvider
from app.db import create_engine, init_db
from app.engine.executor import WorkflowEngine
from app.models import ApprovalRequest, NodeLog, NodeRun, ToolCall, WorkflowRun
from app.tools.mock_tools import LEDGER, build_default_registry
from app.workflows import build_registry

BUG_TICKET = {
    "customer_id": "cus_1001",
    "subject": "Dashboard crashes with a 500 error on export",
    "message": (
        "Every time I click Export on the analytics dashboard the page crashes "
        "and I get a 500 error. This is broken for my whole team since the "
        "release yesterday."
    ),
    "channel": "email",
}

BILLING_TICKET = {
    "customer_id": "cus_1002",
    "subject": "Invoice charge looks wrong",
    "message": (
        "We were charged twice on our last invoice. Can you check the payment "
        "and issue a refund for the duplicate charge on our subscription?"
    ),
    "channel": "email",
}

UNCLEAR_TICKET = {
    "customer_id": "cus_1003",
    "subject": "Question",
    "message": "Hi, can someone get back to me about the thing we discussed?",
    "channel": "email",
}


@pytest_asyncio.fixture
async def session_factory(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path.as_posix()}/test.db"
    db_engine = create_engine(url)
    await init_db(db_engine)
    factory = async_sessionmaker(db_engine, expire_on_commit=False, class_=AsyncSession)
    try:
        yield factory
    finally:
        await db_engine.dispose()


@pytest.fixture
def ledger():
    LEDGER.reset()
    yield LEDGER
    LEDGER.reset()


@pytest.fixture
def engine(session_factory, ledger) -> WorkflowEngine:
    return WorkflowEngine(
        session_factory=session_factory,
        workflows=build_registry(),
        provider=MockProvider(),
        tools=build_default_registry(),
    )


# ---------------------------------------------------------------- helpers
class RunProbe:
    """Small read-side helper so tests read like the debugger UI does."""

    def __init__(self, factory: async_sessionmaker[AsyncSession], run_id: str) -> None:
        self._factory = factory
        self.run_id = run_id

    async def run(self) -> WorkflowRun:
        async with self._factory() as session:
            return (
                await session.execute(
                    select(WorkflowRun).where(WorkflowRun.id == self.run_id)
                )
            ).scalar_one()

    async def status(self) -> str:
        return (await self.run()).status

    async def nodes(self) -> dict[str, NodeRun]:
        async with self._factory() as session:
            rows = (
                await session.execute(select(NodeRun).where(NodeRun.run_id == self.run_id))
            ).scalars()
            return {n.node_id: n for n in rows}

    async def node(self, node_id: str) -> NodeRun:
        return (await self.nodes())[node_id]

    async def statuses(self) -> dict[str, str]:
        return {k: v.status for k, v in (await self.nodes()).items()}

    async def tool_calls(self, node_id: str | None = None) -> list[ToolCall]:
        async with self._factory() as session:
            stmt = select(ToolCall).where(ToolCall.run_id == self.run_id)
            if node_id:
                stmt = stmt.where(ToolCall.node_id == node_id)
            return list((await session.execute(stmt.order_by(ToolCall.created_at))).scalars())

    async def approvals(self) -> list[ApprovalRequest]:
        async with self._factory() as session:
            return list(
                (
                    await session.execute(
                        select(ApprovalRequest).where(ApprovalRequest.run_id == self.run_id)
                    )
                ).scalars()
            )

    async def logs(self, node_id: str | None = None) -> list[NodeLog]:
        async with self._factory() as session:
            stmt = select(NodeLog).where(NodeLog.run_id == self.run_id)
            if node_id:
                stmt = stmt.where(NodeLog.node_id == node_id)
            return list((await session.execute(stmt.order_by(NodeLog.id))).scalars())


@pytest_asyncio.fixture
async def start_run(engine, session_factory):
    async def _start(
        payload: dict[str, Any],
        *,
        options: dict[str, Any] | None = None,
        workflow_id: str = "support_triage",
    ) -> RunProbe:
        run_id = await engine.create_run(workflow_id, payload, options=options or {})
        await engine.advance(run_id)
        return RunProbe(session_factory, run_id)

    return _start
