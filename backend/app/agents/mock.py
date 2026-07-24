"""Deterministic, rule-based agent provider.

This is the default so the whole system runs with no API key and every test
scenario is byte-for-byte reproducible. It implements the same three tasks the
Claude provider does, dispatching on ``AgentRequest.task``.
"""

from __future__ import annotations

import re
from typing import Any

from app.agents.provider import AgentRequest, AgentResponse

_BUG_TERMS = {
    "bug", "error", "crash", "crashes", "broken", "fails", "failing", "failure",
    "exception", "stacktrace", "500", "502", "timeout", "timing out", "not working",
    "doesn't work", "does not work", "regression", "blank screen", "freezes",
}

_BILLING_TERMS = {
    "invoice", "invoiced", "billing", "billed", "charge", "charged", "charges",
    "refund", "payment", "paid", "subscription", "card", "receipt", "price",
    "pricing", "overcharge", "double charged", "renewal", "plan",
}

_URGENT_TERMS = {"urgent", "asap", "immediately", "outage", "production down", "critical"}


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def _score(text: str, terms: set[str]) -> int:
    lowered = text.lower()
    tokens = set(_tokens(text))
    hits = 0
    for term in terms:
        if " " in term:
            hits += 1 if term in lowered else 0
        else:
            hits += 1 if term in tokens else 0
    return hits


class MockProvider:
    """Rule-based stand-in for an LLM. Same interface, zero dependencies."""

    name = "mock"

    async def generate_json(self, request: AgentRequest) -> AgentResponse:
        handler = getattr(self, f"_task_{request.task}", None)
        if handler is None:
            raise ValueError(f"mock provider has no implementation for task '{request.task}'")
        output = handler(request.inputs)
        return AgentResponse(output=output, provider=self.name, model="rule-based-v1")

    # -- tasks -----------------------------------------------------------
    def _task_classify_ticket(self, inputs: dict[str, Any]) -> dict[str, Any]:
        subject = str(inputs.get("subject") or "")
        message = str(inputs.get("message") or "")
        text = f"{subject}\n{message}"

        bug = _score(text, _BUG_TERMS)
        billing = _score(text, _BILLING_TERMS)
        total = bug + billing

        if total == 0:
            category = "unclear"
            confidence = 0.25
        elif bug == billing:
            # genuinely mixed signals -> let the human gate decide
            category = "unclear"
            confidence = 0.4
        else:
            category = "bug" if bug > billing else "billing"
            lead = max(bug, billing)
            # confidence grows with both the number and the dominance of hits
            confidence = round(min(0.95, 0.55 + 0.12 * lead - 0.1 * (total - lead)), 2)
            confidence = max(confidence, 0.3)

        priority = "medium"
        if _score(text, _URGENT_TERMS) > 0:
            priority = "urgent"
        elif category == "bug" and bug >= 3:
            priority = "high"
        elif category == "unclear":
            priority = "low"

        summary = subject.strip() or message.strip()[:120] or "Customer support request"

        return {
            "category": category,
            "confidence": confidence,
            "summary": summary[:400],
            "reasoning": (
                f"Keyword scoring over subject+body: bug={bug}, billing={billing}. "
                f"Selected '{category}' with confidence {confidence}."
            ),
            "suggested_priority": priority,
        }

    def _task_draft_bug_reply(self, inputs: dict[str, Any]) -> dict[str, Any]:
        issue = inputs.get("issue") or {}
        customer = inputs.get("customer") or {}
        summary = str(inputs.get("summary") or "your report")
        key = issue.get("key", "ENG-?")
        name = customer.get("name", "there")
        return {
            "subject": f"We're on it: {summary}"[:200],
            "body": (
                f"Hi {name},\n\n"
                f"Thanks for flagging this. I've reproduced the details you sent and "
                f"raised it with our engineering team as {key}.\n\n"
                f"Summary of what you reported: {summary}\n\n"
                f"You're on the {customer.get('plan', 'standard')} plan, so this is "
                f"queued at {issue.get('priority', 'medium')} priority. I'll update you "
                f"here as soon as there's movement on the fix.\n\n"
                "Sorry for the disruption in the meantime."
            ),
            "tone": "apologetic",
            "next_steps": [
                f"Engineering triages {key}",
                "We post an update on this thread when a fix ships",
            ],
        }

    def _task_draft_billing_reply(self, inputs: dict[str, Any]) -> dict[str, Any]:
        invoice = inputs.get("invoice") or {}
        customer = inputs.get("customer") or {}
        summary = str(inputs.get("summary") or "your billing question")
        name = customer.get("name", "there")
        number = invoice.get("number", "n/a")
        amount = invoice.get("amount_due", 0)
        currency = invoice.get("currency", "USD")
        return {
            "subject": f"About your invoice {number}"[:200],
            "body": (
                f"Hi {name},\n\n"
                f"I pulled up the invoice behind {summary}.\n\n"
                f"Invoice {number} was issued on {invoice.get('issued_on', 'n/a')} for "
                f"{amount} {currency} and is currently marked "
                f"'{invoice.get('status', 'unknown')}'. It covers the "
                f"{customer.get('plan', 'standard')} plan for "
                f"{invoice.get('period', 'the current period')}.\n\n"
                "If that doesn't match what you were charged, reply here and I'll open "
                "a billing investigation right away."
            ),
            "tone": "informative",
            "next_steps": [
                f"Review invoice {number}",
                "Reply if the amount looks wrong and we'll investigate",
            ],
        }

    def _task_draft_clarification_reply(self, inputs: dict[str, Any]) -> dict[str, Any]:
        customer = inputs.get("customer") or {}
        approval = inputs.get("approval") or {}
        summary = str(inputs.get("summary") or "your message")
        name = customer.get("name", "there")
        note = approval.get("note") or ""
        questions = [
            "What were you doing right before the problem appeared?",
            "Is this affecting billing, or the product itself?",
        ]
        if note:
            questions.insert(0, note)
        return {
            "subject": f"A couple of quick questions about: {summary}"[:200],
            "body": (
                f"Hi {name},\n\n"
                f"Thanks for reaching out about {summary}. Before I route this to the "
                "right team I'd like to make sure I understand the situation.\n\n"
                + "\n".join(f"- {q}" for q in questions)
                + "\n\nAs soon as you reply I'll get this to the right people."
            ),
            "tone": "inquisitive",
            "next_steps": questions[:3],
        }

    def _task_finalize_response(self, inputs: dict[str, Any]) -> dict[str, Any]:
        reply = inputs.get("reply") or {}
        route = str(inputs.get("route") or "unclear")
        handled_by = {
            "bug": "bug_path",
            "billing": "billing_path",
            "unclear": "human_review_path",
        }.get(route, "human_review_path")
        resolution = {
            "bug": "in_progress",
            "billing": "resolved",
            "unclear": "awaiting_customer",
        }.get(route, "awaiting_customer")

        body = str(reply.get("body") or "").strip()
        steps = reply.get("next_steps") or []
        if steps:
            body = f"{body}\n\nNext steps:\n" + "\n".join(f"- {s}" for s in steps)
        body = f"{body}\n\n-- \nCustomer Support"

        return {
            "subject": str(reply.get("subject") or "Re: your support request")[:200],
            "body": body[:4000],
            "resolution": resolution,
            "handled_by": handled_by,
        }
