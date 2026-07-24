"""Scenario: human approval."""

from __future__ import annotations

import pytest

from app.engine.executor import InvalidTransitionError
from tests.conftest import BUG_TICKET, UNCLEAR_TICKET


async def test_run_parks_and_opens_an_approval_request(start_run):
    probe = await start_run(UNCLEAR_TICKET)

    assert await probe.status() == "waiting_approval"
    approvals = await probe.approvals()
    assert len(approvals) == 1
    assert approvals[0].status == "pending"
    assert approvals[0].node_id == "human_review"
    # the reviewer gets the full decision context, not just a yes/no prompt
    assert set(approvals[0].context_json) == {
        "subject",
        "message",
        "classification",
        "customer",
    }


async def test_approval_resumes_the_run_and_carries_the_note_downstream(
    engine, start_run, ledger
):
    probe = await start_run(UNCLEAR_TICKET)
    assert await probe.status() == "waiting_approval"
    assert ledger.sent_emails == []

    await engine.submit_approval(
        probe.run_id,
        "human_review",
        decision="approve",
        note="Ask them which product area this is about.",
        decided_by="priya@support.example",
    )

    assert await probe.status() == "succeeded"
    statuses = await probe.statuses()
    assert statuses["human_review"] == "succeeded"
    assert statuses["draft_clarification_reply"] == "succeeded"
    assert statuses["send_reply"] == "succeeded"

    approval_output = (await probe.node("human_review")).output_json
    assert approval_output["decision"] == "approved"
    assert approval_output["decided_by"] == "priya@support.example"

    # the reviewer's note actually reaches the customer-facing draft
    draft = (await probe.node("draft_clarification_reply")).output_json
    assert "which product area" in draft["body"]

    run = await probe.run()
    assert run.output_json["handled_by"] == "human_review_path"
    assert run.output_json["resolution"] == "awaiting_customer"
    assert len(ledger.sent_emails) == 1


async def test_rejection_fails_the_run_without_sending_anything(
    engine, start_run, ledger
):
    probe = await start_run(UNCLEAR_TICKET)

    await engine.submit_approval(
        probe.run_id,
        "human_review",
        decision="reject",
        note="Duplicate of an existing thread.",
        decided_by="sam@support.example",
    )

    assert await probe.status() == "failed"
    node = await probe.node("human_review")
    assert node.status == "failed"
    assert node.error_code == "approval_rejected"
    assert "Duplicate of an existing thread." in node.error
    assert ledger.sent_emails == []


async def test_retry_after_rejection_reopens_the_gate(engine, start_run):
    probe = await start_run(UNCLEAR_TICKET)
    await engine.submit_approval(probe.run_id, "human_review", decision="reject")
    assert await probe.status() == "failed"

    await engine.retry_node(probe.run_id, "human_review")

    # the old decision is superseded, a fresh gate is opened, run parks again
    assert await probe.status() == "waiting_approval"
    approvals = await probe.approvals()
    assert [a.status for a in approvals] == ["superseded", "pending"]

    await engine.submit_approval(probe.run_id, "human_review", decision="approve")
    assert await probe.status() == "succeeded"


async def test_approving_a_node_that_is_not_waiting_is_rejected(engine, start_run):
    probe = await start_run(BUG_TICKET)
    with pytest.raises(InvalidTransitionError):
        await engine.submit_approval(probe.run_id, "human_review", decision="approve")
