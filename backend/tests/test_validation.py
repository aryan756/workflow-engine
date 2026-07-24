"""Scenario: validation failure.

Two gates are covered:
  * the run input must satisfy the SupportTicket contract,
  * an agent's raw output must satisfy its declared contract before any
    downstream node - in particular any side-effecting tool - is scheduled.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.agents.contracts import TicketClassification, json_schema_for
from tests.conftest import BUG_TICKET


async def test_invalid_run_input_fails_at_the_intake_gate(start_run, ledger):
    probe = await start_run(
        {"customer_id": "", "subject": "", "message": "", "channel": "carrier-pigeon"}
    )

    assert await probe.status() == "failed"
    node = await probe.node("intake")
    assert node.status == "failed"
    assert node.error_code == "input_validation_failed"
    assert node.attempts == 1  # not retryable, so no wasted attempts

    statuses = await probe.statuses()
    assert statuses["classify"] == "pending"
    assert ledger.linear_issues == []
    assert ledger.sent_emails == []


async def test_agent_output_violating_its_contract_fails_the_node(start_run, ledger):
    probe = await start_run(
        BUG_TICKET,
        # classify has max_attempts=2, so times=2 poisons both automatic attempts
        options={"faults": {"classify": {"kind": "agent_invalid_output", "times": 2}}},
    )

    assert await probe.status() == "failed"
    node = await probe.node("classify")
    assert node.status == "failed"
    assert node.error_code == "agent_output_validation_failed"

    # nothing downstream of the bad decision ran, and no side effect happened
    statuses = await probe.statuses()
    assert statuses["route"] == "pending"
    assert statuses["create_issue"] == "pending"
    assert ledger.linear_issues == []
    assert ledger.sent_emails == []


async def test_validation_failure_is_fully_traced(start_run):
    probe = await start_run(
        BUG_TICKET,
        options={"faults": {"classify": {"kind": "agent_invalid_output", "times": 2}}},
    )

    logs = await probe.logs("classify")
    warnings = [log for log in logs if log.level == "warning" and log.payload_json]
    contract_failures = [
        log for log in warnings if "errors" in (log.payload_json or {})
    ]
    # 3 attempts per execution (1 + 2 repairs) x 2 automatic attempts
    assert len(contract_failures) == 6
    first = contract_failures[0].payload_json
    assert first["raw_output"]["category"] == "definitely-not-a-valid-category"
    assert any(err["loc"] == ["category"] for err in first["errors"])


async def test_retry_after_the_fault_is_exhausted_completes_the_run(
    engine, start_run, ledger
):
    probe = await start_run(
        BUG_TICKET,
        options={"faults": {"classify": {"kind": "agent_invalid_output", "times": 2}}},
    )
    assert await probe.status() == "failed"

    await engine.retry_node(probe.run_id, "classify")

    assert await probe.status() == "succeeded"
    assert (await probe.node("classify")).status == "succeeded"
    assert len(ledger.sent_emails) == 1


async def test_repair_loop_recovers_within_a_single_attempt(engine, session_factory):
    """A provider that is wrong once and right afterwards must not fail the node."""
    from app.agents.mock import MockProvider
    from app.agents.provider import AgentRequest, AgentResponse
    from app.engine.executor import WorkflowEngine
    from app.tools.mock_tools import build_default_registry
    from app.workflows import build_registry

    class FlakyProvider(MockProvider):
        name = "flaky"

        def __init__(self) -> None:
            self.calls = 0

        async def generate_json(self, request: AgentRequest) -> AgentResponse:
            if request.task == "classify_ticket":
                self.calls += 1
                if self.calls == 1:
                    return AgentResponse(
                        output={"category": "bug"},  # missing required fields
                        provider=self.name,
                    )
            return await super().generate_json(request)

    provider = FlakyProvider()
    flaky_engine = WorkflowEngine(
        session_factory=session_factory,
        workflows=build_registry(),
        provider=provider,
        tools=build_default_registry(),
    )
    run_id = await flaky_engine.create_run("support_triage", BUG_TICKET)
    await flaky_engine.advance(run_id)

    from tests.conftest import RunProbe

    probe = RunProbe(session_factory, run_id)
    node = await probe.node("classify")
    assert node.status == "succeeded"
    assert node.attempts == 1  # repaired inside the first attempt
    assert provider.calls == 2  # one bad answer, one good


def test_contract_schema_is_provider_safe():
    """Unsupported JSON-Schema keywords are stripped, but Pydantic still enforces them."""
    schema = json_schema_for("ticket_classification")
    confidence = schema["properties"]["confidence"]
    assert "minimum" not in confidence and "maximum" not in confidence
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])

    with pytest.raises(ValidationError):
        TicketClassification(
            category="bug",
            confidence=4.2,  # still rejected locally
            summary="x",
            reasoning="y",
            suggested_priority="high",
        )
