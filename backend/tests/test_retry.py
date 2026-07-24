"""Scenario: retry and resume.

`create_issue` is configured with max_attempts=2, so a fault with
``times: 1`` is absorbed automatically and ``times: 2`` exhausts the automatic
budget and requires an operator retry.
"""

from __future__ import annotations

import pytest

from app.engine.executor import InvalidTransitionError
from tests.conftest import BUG_TICKET


async def test_transient_failure_is_absorbed_by_automatic_retry(start_run, ledger):
    probe = await start_run(
        BUG_TICKET,
        options={"faults": {"create_issue": {"kind": "tool_transient", "times": 1}}},
    )

    assert await probe.status() == "succeeded"
    node = await probe.node("create_issue")
    assert node.status == "succeeded"
    assert node.attempts == 2  # one failed, one succeeded

    # the injected failure fires before the tool runs, so nothing duplicated
    assert len(ledger.linear_issues) == 1

    messages = [log.message for log in await probe.logs("create_issue")]
    assert any("Attempt 1 failed" in m for m in messages)
    assert any("Attempt 2 succeeded" in m for m in messages)


async def test_exhausted_retries_fail_the_node_and_block_downstream(start_run):
    probe = await start_run(
        BUG_TICKET,
        options={"faults": {"create_issue": {"kind": "tool_transient", "times": 2}}},
    )

    assert await probe.status() == "failed"
    node = await probe.node("create_issue")
    assert node.status == "failed"
    assert node.attempts == 2
    assert node.error_code == "tool_transient_error"

    statuses = await probe.statuses()
    # downstream stays PENDING (blocked), never skipped - a retry must be able
    # to resume the branch
    assert statuses["draft_bug_reply"] == "pending"
    assert statuses["finalize"] == "pending"
    assert statuses["send_reply"] == "pending"

    run = await probe.run()
    assert "create_issue" in (run.error or "")


async def test_manual_retry_resumes_the_run_to_completion(engine, start_run, ledger):
    probe = await start_run(
        BUG_TICKET,
        options={"faults": {"create_issue": {"kind": "tool_transient", "times": 2}}},
    )
    assert await probe.status() == "failed"

    await engine.retry_node(probe.run_id, "create_issue")

    assert await probe.status() == "succeeded"
    assert (await probe.node("create_issue")).status == "succeeded"
    assert (await probe.node("send_reply")).status == "succeeded"
    assert len(ledger.linear_issues) == 1
    assert len(ledger.sent_emails) == 1


async def test_retry_is_rejected_for_a_node_that_did_not_fail(engine, start_run):
    probe = await start_run(BUG_TICKET)
    with pytest.raises(InvalidTransitionError):
        await engine.retry_node(probe.run_id, "intake")


async def test_agent_provider_failure_is_retried(start_run):
    probe = await start_run(
        BUG_TICKET,
        options={"faults": {"classify": {"kind": "agent_error", "times": 1}}},
    )
    assert await probe.status() == "succeeded"
    assert (await probe.node("classify")).attempts == 2


async def test_fault_injection_can_be_disabled(session_factory, ledger):
    """`options.faults` is a debug hook; a deployment must be able to refuse it."""
    from app.agents.mock import MockProvider
    from app.engine.executor import WorkflowEngine
    from app.tools.mock_tools import build_default_registry
    from app.workflows import build_registry
    from tests.conftest import RunProbe

    locked_down = WorkflowEngine(
        session_factory=session_factory,
        workflows=build_registry(),
        provider=MockProvider(),
        tools=build_default_registry(),
        allow_fault_injection=False,
    )
    run_id = await locked_down.create_run(
        "support_triage",
        BUG_TICKET,
        options={"faults": {"create_issue": {"kind": "tool_transient", "times": 5}}},
    )
    await locked_down.advance(run_id)

    probe = RunProbe(session_factory, run_id)
    assert await probe.status() == "succeeded"  # the fault never applied
    assert "faults" not in ((await probe.run()).options_json or {})


async def test_resume_recovers_a_node_left_running(engine, session_factory, start_run):
    """Simulates a process crash mid-node: resume puts it back in the queue."""
    from sqlalchemy import select

    from app.models import NodeRun, WorkflowRun

    probe = await start_run(
        BUG_TICKET,
        options={"faults": {"create_issue": {"kind": "tool_transient", "times": 2}}},
    )
    assert await probe.status() == "failed"

    # pretend the process died while create_issue was executing
    async with session_factory() as session:
        node_run = (
            await session.execute(
                select(NodeRun).where(
                    NodeRun.run_id == probe.run_id, NodeRun.node_id == "create_issue"
                )
            )
        ).scalar_one()
        node_run.status = "running"
        run = (
            await session.execute(
                select(WorkflowRun).where(WorkflowRun.id == probe.run_id)
            )
        ).scalar_one()
        run.status = "running"
        await session.commit()

    await engine.resume_run(probe.run_id)

    assert await probe.status() == "succeeded"
    messages = [log.message for log in await probe.logs("create_issue")]
    assert any("interrupted process" in m for m in messages)
