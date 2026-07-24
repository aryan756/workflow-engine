"""End-to-end tests through the HTTP API the debugger UI actually uses."""

from __future__ import annotations

import httpx
import pytest_asyncio

from app.config import get_settings
from app.db import dispose_db
from app.tools.mock_tools import LEDGER
from tests.conftest import BUG_TICKET, UNCLEAR_TICKET


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path.as_posix()}/api.db"
    )
    monkeypatch.setenv("AGENT_PROVIDER", "mock")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    get_settings.cache_clear()
    await dispose_db()
    LEDGER.reset()

    from app.main import create_app

    app = create_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as http_client:
            http_client.app = app  # type: ignore[attr-defined]
            yield http_client

    await dispose_db()
    get_settings.cache_clear()
    LEDGER.reset()


async def _settle(client: httpx.AsyncClient) -> None:
    await client.app.state.engine.wait_for_idle()  # type: ignore[attr-defined]


async def test_system_and_workflow_endpoints(client):
    info = (await client.get("/api/system")).json()
    assert info["agent_provider"] == "mock"
    assert "support_triage" in info["workflows"]
    assert any(t["side_effecting"] for t in info["tools"])

    workflows = (await client.get("/api/workflows")).json()
    assert len(workflows) == 1
    definition = workflows[0]
    assert {n["id"] for n in definition["nodes"]} >= {"intake", "route", "send_reply"}
    assert definition["nodes"][0]["rank"] == 0
    labelled = [e for e in definition["edges"] if e["label"]]
    assert {e["label"] for e in labelled} == {"bug", "billing", "unclear"}


async def test_run_lifecycle_over_http(client):
    response = await client.post(
        "/api/runs", json={"workflow_id": "support_triage", "input": BUG_TICKET}
    )
    assert response.status_code == 201
    run_id = response.json()["id"]

    await _settle(client)

    detail = (await client.get(f"/api/runs/{run_id}")).json()
    assert detail["status"] == "succeeded"
    assert detail["output"]["handled_by"] == "bug_path"

    statuses = {n["node_id"]: n["status"] for n in detail["nodes"]}
    assert statuses["create_issue"] == "succeeded"
    assert statuses["lookup_invoice"] == "skipped"

    # the graph view gets per-edge state so dead branches can be greyed out
    edge_states = {(e["source"], e["target"]): e["state"] for e in detail["edges"]}
    assert edge_states[("route", "create_issue")] == "active"
    assert edge_states[("route", "lookup_invoice")] == "pruned"

    # node inspector payload
    node = (await client.get(f"/api/runs/{run_id}/nodes/create_issue")).json()
    assert node["input"]["title"]
    assert node["output"]["result"]["key"].startswith("ENG-")
    assert node["tool_calls"][0]["status"] == "succeeded"
    assert any("Invoking tool" in log["message"] for log in node["logs"])

    effects = (await client.get("/api/side-effects")).json()
    assert effects["counts"] == {"linear_issues": 1, "sent_emails": 1}


async def test_retry_endpoint_over_http(client):
    response = await client.post(
        "/api/runs",
        json={
            "input": BUG_TICKET,
            "options": {
                "faults": {"create_issue": {"kind": "tool_transient", "times": 2}}
            },
        },
    )
    run_id = response.json()["id"]
    await _settle(client)

    assert (await client.get(f"/api/runs/{run_id}")).json()["status"] == "failed"

    # retrying a node that did not fail is a conflict, not a crash
    conflict = await client.post(f"/api/runs/{run_id}/nodes/intake/retry")
    assert conflict.status_code == 409

    retried = await client.post(f"/api/runs/{run_id}/nodes/create_issue/retry")
    assert retried.status_code == 200
    assert (await client.get(f"/api/runs/{run_id}")).json()["status"] == "succeeded"


async def test_approval_endpoint_over_http(client):
    response = await client.post("/api/runs", json={"input": UNCLEAR_TICKET})
    run_id = response.json()["id"]
    await _settle(client)

    detail = (await client.get(f"/api/runs/{run_id}")).json()
    assert detail["status"] == "waiting_approval"
    assert detail["approvals"][0]["status"] == "pending"
    assert detail["approvals"][0]["node_id"] == "human_review"

    decision = await client.post(
        f"/api/runs/{run_id}/nodes/human_review/approve",
        json={
            "decision": "approve",
            "note": "Ask which product area this is about.",
            "decided_by": "reviewer@example.com",
        },
    )
    assert decision.status_code == 200

    detail = (await client.get(f"/api/runs/{run_id}")).json()
    assert detail["status"] == "succeeded"
    assert detail["output"]["handled_by"] == "human_review_path"

    node = (await client.get(f"/api/runs/{run_id}/nodes/human_review")).json()
    assert node["approval"]["status"] == "approved"
    assert node["approval"]["decided_by"] == "reviewer@example.com"


async def test_unknown_run_returns_404(client):
    assert (await client.get("/api/runs/run_does_not_exist")).status_code == 404
