"""Drive the debugger UI in a real browser and assert the five scenarios.

Runs against the dev servers (backend :8000, Vite :5173). Every interaction
goes through the actual UI - selecting a scenario, clicking Start run, clicking
nodes, pressing Retry/Approve - so this covers the wiring the API tests can't.

    python scripts/ui_verify.py [--headed] [--out DIR]
"""

from __future__ import annotations

import argparse
import sys
import urllib.request
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

UI = "http://localhost:5173"
API = "http://127.0.0.1:8000/api"
SETTLED = ("succeeded", "failed", "waiting_approval", "cancelled")

failures: list[str] = []


def check(label: str, actual: object, expected: object) -> None:
    ok = actual == expected
    suffix = "" if ok else f" (expected {expected!r})"
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: {actual!r}{suffix}")
    if not ok:
        failures.append(f"{label}: got {actual!r}, expected {expected!r}")


def api_post(path: str) -> None:
    urllib.request.urlopen(urllib.request.Request(f"{API}{path}", method="POST"), timeout=10).read()


def side_effects() -> dict:
    import json

    with urllib.request.urlopen(f"{API}/side-effects", timeout=10) as response:
        return json.load(response)["counts"]


# --- UI helpers ---------------------------------------------------------
# Fault dropdown positions (see FAULTS in NewRunForm.tsx). Selected by index
# rather than label so the assertions don't break on wording tweaks.
NO_FAULT = 0
TOOL_TRANSIENT_RECOVERS = 1
TOOL_TRANSIENT_NEEDS_RETRY = 2
TOOL_AFTER_SIDE_EFFECT = 3
AGENT_INVALID_OUTPUT = 4


def current_run_id(page: Page) -> str | None:
    return page.evaluate(
        "() => document.querySelector('[data-testid=run-status]')?.dataset.runId ?? null"
    )


def start_run(page: Page, scenario: str, fault: int = NO_FAULT) -> None:
    """Start a run and block until the UI is actually showing *that* run.

    Waiting on the status pill alone is not enough: the previous run's pill is
    still mounted while the create request is in flight, so assertions would
    race against stale data. Waiting for the rendered run id to change is
    deterministic.
    """
    previous = current_run_id(page)
    selects = page.locator(".new-run select")
    selects.nth(0).select_option(label=scenario)
    if selects.count() > 1:
        selects.nth(1).select_option(index=fault)
    page.get_by_role("button", name="Start run").click()
    page.wait_for_function(
        "prev => { const el = document.querySelector('[data-testid=run-status]');"
        " return el && el.dataset.runId && el.dataset.runId !== prev; }",
        arg=previous,
        timeout=30000,
    )


def wait_settled(page: Page, timeout: int = 30000) -> str:
    """Block until the run-status pill leaves pending/running."""
    page.wait_for_function(
        "() => { const el = document.querySelector('[data-testid=run-status]');"
        " return el && ['succeeded','failed','waiting_approval','cancelled']"
        ".includes(el.dataset.status); }",
        timeout=timeout,
    )
    return page.get_by_test_id("run-status").get_attribute("data-status") or "?"


def act_and_wait(page: Page, button: str, exact: bool = False) -> str:
    """Click a Retry/Approve/Reject button and wait for the run to transition.

    `wait_settled` alone is wrong here: failed -> succeeded is a move between
    two settled states, so it would return the pre-click status immediately.
    This waits for the app to stop being busy *and* the status to actually
    change.
    """
    before = page.get_by_test_id("run-status").get_attribute("data-status")
    page.get_by_role("button", name=button, exact=exact).click()
    page.wait_for_function(
        "prev => { const app = document.querySelector('.app');"
        " const el = document.querySelector('[data-testid=run-status]');"
        " if (!app || !el || app.dataset.busy === 'true') return false;"
        " return ['succeeded','failed','waiting_approval','cancelled']"
        ".includes(el.dataset.status) && el.dataset.status !== prev; }",
        arg=before,
        timeout=30000,
    )
    return page.get_by_test_id("run-status").get_attribute("data-status") or "?"


def node_status(page: Page, node_id: str) -> str | None:
    return page.locator(f'[data-node-id="{node_id}"]').get_attribute("data-status")


def all_node_statuses(page: Page) -> dict[str, str]:
    return page.evaluate(
        "() => Object.fromEntries([...document.querySelectorAll('[data-node-id]')]"
        ".map(n => [n.dataset.nodeId, n.dataset.status]))"
    )


def open_node(page: Page, node_id: str) -> None:
    page.locator(f'[data-node-id="{node_id}"]').click()
    expect(page.locator(".inspector .inspector-head h2")).to_be_visible(timeout=10000)


def inspector_tab(page: Page, name: str) -> None:
    page.locator(".tabs .tab", has_text=name).first.click()


def effects_pill(page: Page) -> str:
    return page.locator(".pill-effects").inner_text()


LAYOUT_JS = """
() => {
  const nodes = [...document.querySelectorAll('g.node')].map(g => {
    const r = g.getBoundingClientRect();
    return { id: g.dataset.nodeId, x: r.x, y: r.y, w: r.width, h: r.height };
  });
  const labels = [...document.querySelectorAll('text.edge-label')].map(t => {
    const r = t.getBoundingClientRect();
    return { text: t.textContent, x: r.x, y: r.y, w: r.width, h: r.height };
  });
  const overlaps = [];
  for (const l of labels) {
    for (const n of nodes) {
      if (l.x < n.x + n.w && l.x + l.w > n.x && l.y < n.y + n.h && l.y + l.h > n.y) {
        overlaps.push(l.text + ' over ' + n.id);
      }
    }
  }
  const panel = document.querySelector('.graph-scroll');
  const p = panel.getBoundingClientRect();
  return {
    nodeCount: nodes.length,
    overlaps,
    horizontalScroll: panel.scrollWidth > panel.clientWidth + 1,
    clipped: nodes.filter(n => n.x < p.x - 1 || n.x + n.w > p.x + p.width + 1)
                  .map(n => n.id),
  };
}
"""


def scenario_layout(page: Page, shots: Path) -> None:
    """Geometry the eye is bad at judging on a scaled-down screenshot.

    Both of these were real regressions: branch labels were being drawn on top
    of the node they came from, and the DAG overflowed its panel so half the
    nodes were unreachable without scrolling.
    """
    print("\n[6] LAYOUT - the DAG fits and nothing overlaps")
    api_post("/side-effects/reset")
    start_run(page, "Bug report")
    wait_settled(page)

    for width in (1680, 1440, 1280):
        page.set_viewport_size({"width": width, "height": 1000})
        page.wait_for_timeout(150)
        r = page.evaluate(LAYOUT_JS)
        check(f"@{width}px all nodes rendered", r["nodeCount"], 12)
        check(f"@{width}px no label/node overlap", r["overlaps"], [])
        check(f"@{width}px no clipped nodes", r["clipped"], [])
        check(f"@{width}px no horizontal scroll", r["horizontalScroll"], False)

    page.set_viewport_size({"width": 1680, "height": 1000})
    page.wait_for_timeout(150)
    page.screenshot(path=str(shots / "13-layout.png"))


# --- scenarios ----------------------------------------------------------
def scenario_branching(page: Page, shots: Path) -> None:
    print("\n[1] BRANCHING - bug ticket takes the bug path")
    api_post("/side-effects/reset")
    start_run(page, "Bug report")
    check("run status", wait_settled(page), "succeeded")

    statuses = all_node_statuses(page)
    check("create_issue", statuses.get("create_issue"), "succeeded")
    check("draft_bug_reply", statuses.get("draft_bug_reply"), "succeeded")
    check("lookup_invoice (other branch)", statuses.get("lookup_invoice"), "skipped")
    check("human_review (other branch)", statuses.get("human_review"), "skipped")
    check("side effects", side_effects(), {"linear_issues": 1, "sent_emails": 1})

    open_node(page, "route")
    inspector_tab(page, "output")
    output = page.locator(".inspector .json").inner_text()
    check("branch recorded its decision", '"selected": "bug"' in output, True)
    page.screenshot(path=str(shots / "01-branching-bug.png"), full_page=False)

    print("\n[1b] BRANCHING - billing ticket takes the billing path")
    api_post("/side-effects/reset")
    start_run(page, "Billing question")
    check("run status", wait_settled(page), "succeeded")
    statuses = all_node_statuses(page)
    check("lookup_invoice", statuses.get("lookup_invoice"), "succeeded")
    check("create_issue (other branch)", statuses.get("create_issue"), "skipped")
    check("no issue filed", side_effects()["linear_issues"], 0)
    page.screenshot(path=str(shots / "02-branching-billing.png"))


def scenario_validation(page: Page, shots: Path) -> None:
    print("\n[2] VALIDATION FAILURE - bad input stops at the intake gate")
    api_post("/side-effects/reset")
    start_run(page, "Invalid input")
    check("run status", wait_settled(page), "failed")
    statuses = all_node_statuses(page)
    check("intake", statuses.get("intake"), "failed")
    check("classify blocked, not skipped", statuses.get("classify"), "pending")
    check("no side effects", side_effects(), {"linear_issues": 0, "sent_emails": 0})

    open_node(page, "intake")
    alert = page.locator(".inspector .alert-error").first.inner_text()
    check("error code shown in UI", "input_validation_failed" in alert, True)
    retry_button = page.get_by_role("button", name="Retry this node")
    check("retry button offered", retry_button.is_visible(), True)
    page.screenshot(path=str(shots / "03-validation-input.png"))

    print("\n[2b] VALIDATION FAILURE - agent output violates its contract")
    api_post("/side-effects/reset")
    start_run(page, "Bug report", AGENT_INVALID_OUTPUT)
    check("run status", wait_settled(page), "failed")
    statuses = all_node_statuses(page)
    check("classify", statuses.get("classify"), "failed")
    check("create_issue never ran", statuses.get("create_issue"), "pending")
    check("no side effects", side_effects()["linear_issues"], 0)

    open_node(page, "classify")
    inspector_tab(page, "logs")
    logs = page.locator(".inspector .logs").inner_text()
    check("repair attempts visible in trace", "failed contract validation" in logs, True)
    page.screenshot(path=str(shots / "04-validation-agent.png"))


def scenario_retry(page: Page, shots: Path) -> None:
    print("\n[3] RETRY - exhausted budget, then operator retry from the UI")
    api_post("/side-effects/reset")
    start_run(page, "Bug report", TOOL_TRANSIENT_NEEDS_RETRY)
    check("run status", wait_settled(page), "failed")
    check("create_issue failed", node_status(page, "create_issue"), "failed")
    check("downstream blocked, not skipped", node_status(page, "draft_bug_reply"), "pending")
    check("no issue filed (failed before the tool)", side_effects()["linear_issues"], 0)

    open_node(page, "create_issue")
    page.screenshot(path=str(shots / "05-retry-failed.png"))
    check("run status after retry", act_and_wait(page, "Retry this node"), "succeeded")
    check("create_issue recovered", node_status(page, "create_issue"), "succeeded")
    check("send_reply ran", node_status(page, "send_reply"), "succeeded")
    check("exactly one issue", side_effects()["linear_issues"], 1)
    page.screenshot(path=str(shots / "06-retry-recovered.png"))


def scenario_idempotency(page: Page, shots: Path) -> None:
    print("\n[4] IDEMPOTENCY - node fails AFTER the tool succeeded")
    api_post("/side-effects/reset")
    start_run(page, "Bug report", TOOL_AFTER_SIDE_EFFECT)
    check("run status", wait_settled(page), "failed")
    check("create_issue failed", node_status(page, "create_issue"), "failed")
    check("but the issue WAS filed", side_effects()["linear_issues"], 1)
    check("header shows the effect", "issues 1" in effects_pill(page), True)

    open_node(page, "create_issue")
    inspector_tab(page, "tools")
    tools = page.locator(".inspector .toolcall").inner_text()
    check("ledger row recorded as succeeded", "succeeded" in tools, True)
    page.screenshot(path=str(shots / "07-idempotency-failed.png"))

    check("run status after retry", act_and_wait(page, "Retry this node"), "succeeded")
    check("STILL exactly one issue", side_effects()["linear_issues"], 1)
    check("exactly one email", side_effects()["sent_emails"], 1)

    open_node(page, "create_issue")
    inspector_tab(page, "logs")
    logs = page.locator(".inspector .logs").inner_text()
    check("replay is visible in the trace", "Idempotent replay" in logs, True)
    inspector_tab(page, "tools")
    # times=2 poisons both automatic attempts, so the recorded result is
    # replayed twice: once by attempt 2, once by the operator retry.
    check(
        "replay counted on the ledger row",
        "replayed 2" in page.locator(".toolcall").inner_text(),
        True,
    )
    page.screenshot(path=str(shots / "08-idempotency-replayed.png"))


def scenario_approval(page: Page, shots: Path) -> None:
    print("\n[5] APPROVAL - run parks, human approves from the UI")
    api_post("/side-effects/reset")
    start_run(page, "Ambiguous ticket")
    check("run status", wait_settled(page), "waiting_approval")
    check("human_review parked", node_status(page, "human_review"), "waiting_approval")
    check("bug branch skipped", node_status(page, "create_issue"), "skipped")
    check("nothing sent yet", side_effects()["sent_emails"], 0)

    open_node(page, "human_review")
    check("approval prompt shown", page.locator(".alert-approval").is_visible(), True)
    page.screenshot(path=str(shots / "09-approval-parked.png"))

    note = "Ask whether this is about billing or the product."
    page.locator(".alert-approval textarea").fill(note)
    check("run status after approval", act_and_wait(page, "Approve", exact=True), "succeeded")
    check("clarification drafted", node_status(page, "draft_clarification_reply"), "succeeded")
    check("reply sent", side_effects()["sent_emails"], 1)

    open_node(page, "draft_clarification_reply")
    inspector_tab(page, "output")
    body = page.locator(".inspector .json").inner_text()
    check("reviewer note reached the draft", "billing or the product" in body, True)
    page.screenshot(path=str(shots / "10-approval-approved.png"))

    print("\n[5b] APPROVAL - rejection stops the run and sends nothing")
    api_post("/side-effects/reset")
    start_run(page, "Ambiguous ticket")
    check("run status", wait_settled(page), "waiting_approval")
    open_node(page, "human_review")
    page.locator(".alert-approval textarea").fill("Duplicate of an existing thread.")
    check("run status after rejection", act_and_wait(page, "Reject"), "failed")
    check("nothing sent", side_effects()["sent_emails"], 0)
    open_node(page, "human_review")
    check(
        "rejection reason surfaced",
        "approval_rejected" in page.locator(".inspector .alert-error").first.inner_text(),
        True,
    )
    page.screenshot(path=str(shots / "11-approval-rejected.png"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--out", default="../ui-verification")
    args = parser.parse_args()

    shots = Path(__file__).resolve().parent / args.out
    shots.mkdir(parents=True, exist_ok=True)

    console_errors: list[str] = []

    with sync_playwright() as p:
        # Use an already-installed browser so there is nothing to download.
        browser = None
        for channel in ("chrome", "msedge", None):
            try:
                browser = p.chromium.launch(channel=channel, headless=not args.headed)
                break
            except Exception:  # noqa: BLE001 - try the next channel
                continue
        if browser is None:
            print("No Chrome/Edge found. Install one, or run: playwright install chromium")
            return 1
        page = browser.new_page(viewport={"width": 1680, "height": 1000})
        page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: console_errors.append(f"pageerror: {e}"))
        page.on(
            "requestfailed",
            lambda r: console_errors.append(f"requestfailed: {r.url}"),
        )
        page.on(
            "response",
            lambda r: console_errors.append(f"http {r.status}: {r.url}")
            if r.status >= 400 and "favicon" not in r.url
            else None,
        )
        page.goto(UI, wait_until="networkidle", timeout=30000)

        for scenario in (
            scenario_branching,
            scenario_validation,
            scenario_retry,
            scenario_idempotency,
            scenario_approval,
            scenario_layout,
        ):
            scenario(page, shots)

        page.screenshot(path=str(shots / "12-full-page.png"), full_page=True)
        browser.close()

    print("\n" + "=" * 66)
    if console_errors:
        print(f"BROWSER CONSOLE ERRORS ({len(console_errors)}):")
        for err in console_errors[:10]:
            print(f"  - {err[:160]}")
        failures.append(f"{len(console_errors)} browser console error(s)")
    else:
        print("No browser console errors.")

    if failures:
        print(f"\n{len(failures)} FAILURE(S):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nAll UI assertions passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())


