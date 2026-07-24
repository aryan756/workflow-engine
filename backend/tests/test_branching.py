"""Scenario: conditional branching.

Each of the three routes must run exactly its own path and skip the other two.
"""

from __future__ import annotations

from tests.conftest import BILLING_TICKET, BUG_TICKET, UNCLEAR_TICKET

BUG_PATH = ["create_issue", "draft_bug_reply"]
BILLING_PATH = ["lookup_invoice", "draft_billing_reply"]
HUMAN_PATH = ["human_review", "draft_clarification_reply"]


async def test_bug_ticket_takes_the_bug_path(start_run, ledger):
    probe = await start_run(BUG_TICKET)

    assert await probe.status() == "succeeded"
    statuses = await probe.statuses()

    assert (await probe.node("route")).selected_labels == ["bug"]
    for node_id in BUG_PATH:
        assert statuses[node_id] == "succeeded", node_id
    for node_id in BILLING_PATH + HUMAN_PATH:
        assert statuses[node_id] == "skipped", node_id

    # the bug path is the one that files an issue
    assert len(ledger.linear_issues) == 1
    assert len(ledger.sent_emails) == 1

    run = await probe.run()
    assert run.output_json["handled_by"] == "bug_path"
    assert run.output_json["resolution"] == "in_progress"


async def test_billing_ticket_takes_the_billing_path(start_run, ledger):
    probe = await start_run(BILLING_TICKET)

    assert await probe.status() == "succeeded"
    statuses = await probe.statuses()

    assert (await probe.node("route")).selected_labels == ["billing"]
    for node_id in BILLING_PATH:
        assert statuses[node_id] == "succeeded", node_id
    for node_id in BUG_PATH + HUMAN_PATH:
        assert statuses[node_id] == "skipped", node_id

    # no issue filed on the billing path
    assert ledger.linear_issues == []
    assert len(ledger.sent_emails) == 1

    run = await probe.run()
    assert run.output_json["handled_by"] == "billing_path"


async def test_ambiguous_ticket_routes_to_human_review(start_run):
    probe = await start_run(UNCLEAR_TICKET)

    # the run parks instead of finishing
    assert await probe.status() == "waiting_approval"
    statuses = await probe.statuses()

    assert (await probe.node("route")).selected_labels == ["unclear"]
    assert statuses["human_review"] == "waiting_approval"
    for node_id in BUG_PATH + BILLING_PATH:
        assert statuses[node_id] == "skipped", node_id
    # downstream of the gate is blocked, not skipped
    assert statuses["draft_clarification_reply"] == "pending"
    assert statuses["send_reply"] == "pending"


async def test_low_confidence_overrides_a_predicted_category(start_run):
    """The confidence gate is evaluated before the category cases."""
    probe = await start_run(
        {
            "customer_id": "cus_1001",
            "subject": "invoice error",
            "message": "There is an error on my invoice and a charge that crashed.",
            "channel": "email",
        }
    )
    classification = (await probe.node("classify")).output_json
    route = (await probe.node("route")).output_json

    if classification["confidence"] < 0.6:
        assert route["selected"] == "unclear"
    else:
        assert route["selected"] == classification["category"]

    # the branch decision is fully inspectable in the node output
    assert [case["label"] for case in route["evaluated"]] == ["unclear", "bug", "billing"]


async def test_parallel_fan_out_runs_both_children(start_run):
    probe = await start_run(BUG_TICKET)
    statuses = await probe.statuses()
    assert statuses["classify"] == "succeeded"
    assert statuses["fetch_context"] == "succeeded"

    intake_finished = (await probe.node("intake")).finished_at
    for node_id in ("classify", "fetch_context"):
        assert (await probe.node(node_id)).started_at >= intake_finished
