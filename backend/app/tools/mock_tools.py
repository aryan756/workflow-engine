"""Mock implementations of the external systems the workflow touches.

They are deliberately dumb but *stateful*: `linear.create_issue` and
`email.send_reply` append to a shared ledger, which is how the idempotency
guarantee becomes observable rather than theoretical.
"""

from __future__ import annotations

import hashlib
from datetime import date, timedelta
from typing import Any

from app.engine.errors import ToolPermanentError
from app.tools.registry import SideEffectLedger, ToolRegistry, ToolSpec

# Module-level so the API, the engine and the tests all observe the same
# effects. In a real system this would be the external service itself.
LEDGER = SideEffectLedger()

_CUSTOMERS: dict[str, dict[str, Any]] = {
    "cus_1001": {
        "name": "Dana Whitfield",
        "email": "dana@northwind.example",
        "plan": "enterprise",
        "tier": "gold",
        "region": "eu-west",
        "open_tickets": 2,
        "lifetime_value_usd": 184000,
        "account_manager": "priya@support.example",
    },
    "cus_1002": {
        "name": "Marco Silva",
        "email": "marco@brightside.example",
        "plan": "growth",
        "tier": "silver",
        "region": "us-east",
        "open_tickets": 0,
        "lifetime_value_usd": 21400,
        "account_manager": "sam@support.example",
    },
    "cus_1003": {
        "name": "Aiko Tanaka",
        "email": "aiko@lumen.example",
        "plan": "starter",
        "tier": "bronze",
        "region": "ap-northeast",
        "open_tickets": 1,
        "lifetime_value_usd": 3600,
        "account_manager": "unassigned",
    },
}

_PLAN_PRICING = {"enterprise": 4800.00, "growth": 890.00, "starter": 79.00}


def _stable_suffix(*parts: str) -> str:
    digest = hashlib.sha256("|".join(parts).encode()).hexdigest()
    return digest[:6].upper()


async def crm_fetch_customer(args: dict[str, Any]) -> dict[str, Any]:
    """Read-only lookup - naturally idempotent."""
    customer_id = str(args.get("customer_id") or "")
    record = _CUSTOMERS.get(customer_id)
    if record is None:
        # Unknown customers are a normal business case, not a crash: return a
        # thin profile so the workflow can still produce a reply.
        return {
            "customer_id": customer_id,
            "name": "there",
            "email": None,
            "plan": "unknown",
            "tier": "none",
            "known_customer": False,
            "open_tickets": 0,
        }
    return {"customer_id": customer_id, "known_customer": True, **record}


async def linear_create_issue(args: dict[str, Any]) -> dict[str, Any]:
    """SIDE EFFECTING. Creating this twice is exactly what we must prevent."""
    title = str(args.get("title") or "").strip()
    if not title:
        raise ToolPermanentError("linear.create_issue requires a non-empty title")

    key = f"ENG-{_stable_suffix(title, str(args.get('customer_id', '')))}"
    issue = {
        "id": key.lower(),
        "key": key,
        "url": f"https://linear.example/issue/{key}",
        "title": title[:200],
        "description": str(args.get("description") or "")[:4000],
        "priority": str(args.get("priority") or "medium"),
        "customer_id": args.get("customer_id"),
        "state": "triage",
        "created_on": date.today().isoformat(),
    }
    LEDGER.linear_issues.append(issue)
    return issue


async def billing_lookup_invoice(args: dict[str, Any]) -> dict[str, Any]:
    """Read-only lookup of the customer's latest invoice."""
    customer_id = str(args.get("customer_id") or "")
    customer = _CUSTOMERS.get(customer_id)
    plan = (customer or {}).get("plan", "starter")
    amount = _PLAN_PRICING.get(plan, 79.00)
    issued = date.today() - timedelta(days=12)
    number = f"INV-{_stable_suffix(customer_id, issued.isoformat())}"
    return {
        "number": number,
        "customer_id": customer_id,
        "plan": plan,
        "amount_due": amount,
        "currency": "USD",
        "status": "paid" if (customer or {}).get("tier") == "gold" else "open",
        "issued_on": issued.isoformat(),
        "due_on": (issued + timedelta(days=30)).isoformat(),
        "period": f"{issued.replace(day=1).isoformat()} to {issued.isoformat()}",
        "line_items": [
            {"description": f"{plan} plan - monthly", "amount": amount},
        ],
    }


async def email_send_reply(args: dict[str, Any]) -> dict[str, Any]:
    """SIDE EFFECTING. The customer must not receive the same reply twice."""
    to = args.get("to")
    subject = str(args.get("subject") or "").strip()
    body = str(args.get("body") or "").strip()
    if not subject or not body:
        raise ToolPermanentError("email.send_reply requires both subject and body")

    message = {
        "message_id": f"msg_{_stable_suffix(str(to), subject, body[:64])}",
        "to": to,
        "subject": subject[:200],
        "body": body,
        "status": "sent",
    }
    LEDGER.sent_emails.append(message)
    return message


def build_default_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="crm.fetch_customer",
            description="Look up account context for a customer id.",
            handler=crm_fetch_customer,
            side_effecting=False,
            required_args=("customer_id",),
        )
    )
    registry.register(
        ToolSpec(
            name="linear.create_issue",
            description="Create a bug ticket in the issue tracker.",
            handler=linear_create_issue,
            side_effecting=True,
            required_args=("title",),
        )
    )
    registry.register(
        ToolSpec(
            name="billing.lookup_invoice",
            description="Fetch the customer's most recent invoice.",
            handler=billing_lookup_invoice,
            side_effecting=False,
            required_args=("customer_id",),
        )
    )
    registry.register(
        ToolSpec(
            name="email.send_reply",
            description="Send the drafted reply to the customer.",
            handler=email_send_reply,
            side_effecting=True,
            required_args=("subject", "body"),
        )
    )
    return registry
