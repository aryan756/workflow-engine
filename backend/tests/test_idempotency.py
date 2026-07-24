"""Scenario: idempotent tool calls.

The interesting failure is a node that dies *after* its tool already succeeded.
A naive engine re-runs the tool on retry and files a second Linear issue /
sends a second email. The idempotency ledger must make the retry a replay.
"""

from __future__ import annotations

from app.engine.handlers.tool_node import idempotency_key
from tests.conftest import BILLING_TICKET, BUG_TICKET


async def test_automatic_retry_after_post_tool_failure_replays_instead_of_re_running(
    start_run, ledger
):
    """One post-effect failure: the node's own retry budget absorbs it, and the
    second attempt replays the recorded result rather than filing a 2nd issue."""
    probe = await start_run(
        BUG_TICKET,
        options={
            "faults": {"create_issue": {"kind": "tool_after_side_effect", "times": 1}}
        },
    )

    assert await probe.status() == "succeeded"
    node = await probe.node("create_issue")
    assert node.attempts == 2
    assert node.output_json["replayed"] is True

    assert len(ledger.linear_issues) == 1
    calls = await probe.tool_calls("create_issue")
    assert len(calls) == 1 and calls[0].replayed_count == 1


async def test_operator_retry_after_post_tool_failure_does_not_duplicate_the_effect(
    engine, start_run, ledger
):
    """Both automatic attempts fail after the effect landed, so a human retries.
    The tool must still have been invoked exactly once."""
    probe = await start_run(
        BUG_TICKET,
        options={
            "faults": {"create_issue": {"kind": "tool_after_side_effect", "times": 2}}
        },
    )

    # the tool ran, then the node failed
    assert await probe.status() == "failed"
    assert (await probe.node("create_issue")).status == "failed"
    assert (await probe.node("create_issue")).attempts == 2
    assert len(ledger.linear_issues) == 1

    await engine.retry_node(probe.run_id, "create_issue")

    assert await probe.status() == "succeeded"
    # *** the guarantee ***: still exactly one issue
    assert len(ledger.linear_issues) == 1
    assert len(ledger.sent_emails) == 1

    node = await probe.node("create_issue")
    assert node.status == "succeeded"
    assert node.output_json["replayed"] is True
    assert node.output_json["result"] == ledger.linear_issues[0]

    calls = await probe.tool_calls("create_issue")
    assert len(calls) == 1  # one ledger row, not one per attempt
    assert calls[0].status == "succeeded"
    assert calls[0].replayed_count == 2  # attempt 2 and the operator retry

    messages = [log.message for log in await probe.logs("create_issue")]
    assert any("Idempotent replay" in m for m in messages)


async def test_every_side_effecting_tool_is_recorded_once_per_run(start_run, ledger):
    probe = await start_run(BUG_TICKET)
    calls = await probe.tool_calls()
    by_node = {c.node_id: c for c in calls}

    assert set(by_node) == {"fetch_context", "create_issue", "send_reply"}
    assert all(c.status == "succeeded" for c in calls)
    assert all(c.replayed_count == 0 for c in calls)
    assert len(ledger.linear_issues) == 1
    assert len(ledger.sent_emails) == 1


async def test_idempotency_key_is_stable_and_argument_sensitive():
    a = idempotency_key("run_1", "create_issue", "linear.create_issue", {"b": 2, "a": 1})
    b = idempotency_key("run_1", "create_issue", "linear.create_issue", {"a": 1, "b": 2})
    assert a == b  # key ordering must not matter

    different_args = idempotency_key(
        "run_1", "create_issue", "linear.create_issue", {"a": 1, "b": 3}
    )
    different_run = idempotency_key(
        "run_2", "create_issue", "linear.create_issue", {"a": 1, "b": 2}
    )
    assert len({a, different_args, different_run}) == 3


async def test_transient_failure_before_the_tool_leaves_no_side_effect(
    engine, start_run, ledger
):
    """The other half of the story: a pre-invocation failure must not record a
    phantom success that would suppress the real call."""
    probe = await start_run(
        BUG_TICKET,
        options={"faults": {"create_issue": {"kind": "tool_transient", "times": 2}}},
    )
    assert await probe.status() == "failed"
    assert ledger.linear_issues == []

    calls = await probe.tool_calls("create_issue")
    assert len(calls) == 1
    assert calls[0].status == "failed"

    await engine.retry_node(probe.run_id, "create_issue")
    assert await probe.status() == "succeeded"
    assert len(ledger.linear_issues) == 1  # the real call happened exactly once


async def test_send_reply_is_not_duplicated_when_the_final_node_is_retried(
    engine, start_run, ledger
):
    probe = await start_run(
        BILLING_TICKET,
        options={
            "faults": {"send_reply": {"kind": "tool_after_side_effect", "times": 2}}
        },
    )
    assert await probe.status() == "failed"
    assert len(ledger.sent_emails) == 1

    await engine.retry_node(probe.run_id, "send_reply")

    assert await probe.status() == "succeeded"
    assert len(ledger.sent_emails) == 1
    assert (await probe.node("send_reply")).output_json["replayed"] is True
