"""Output contracts for agent/decision nodes.

Every agent node declares a contract by name. The engine validates the raw
provider output against it *before* any downstream node is scheduled - a
malformed decision can never propagate into a tool call.
"""

from __future__ import annotations

import copy
from typing import Any, Literal

from pydantic import BaseModel, Field


class SupportTicket(BaseModel):
    """Run input contract for the support-triage workflow."""

    customer_id: str = Field(min_length=1, max_length=64)
    subject: str = Field(min_length=1, max_length=200)
    message: str = Field(min_length=1, max_length=8000)
    channel: Literal["email", "chat", "portal"] = "email"


class TicketClassification(BaseModel):
    category: Literal["bug", "billing", "unclear"]
    confidence: float = Field(ge=0.0, le=1.0)
    summary: str = Field(min_length=1, max_length=400)
    reasoning: str = Field(min_length=1, max_length=1000)
    suggested_priority: Literal["low", "medium", "high", "urgent"]


class CustomerReply(BaseModel):
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=20, max_length=4000)
    tone: Literal["apologetic", "neutral", "informative", "inquisitive"]
    next_steps: list[str] = Field(min_length=1, max_length=6)


class FinalResponse(BaseModel):
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=20, max_length=4000)
    resolution: Literal["resolved", "in_progress", "awaiting_customer", "escalated"]
    handled_by: Literal["bug_path", "billing_path", "human_review_path"]


CONTRACTS: dict[str, type[BaseModel]] = {
    "support_ticket": SupportTicket,
    "ticket_classification": TicketClassification,
    "customer_reply": CustomerReply,
    "final_response": FinalResponse,
}


def get_contract(name: str) -> type[BaseModel]:
    if name not in CONTRACTS:
        raise KeyError(f"unknown contract '{name}'")
    return CONTRACTS[name]


# JSON-Schema keywords the Claude structured-output layer does not accept.
# We strip them from the schema we send but keep validating them locally with
# Pydantic - so the contract stays as strict as it looks.
_UNSUPPORTED_KEYWORDS = {
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "minLength",
    "maxLength",
    "minItems",
    "maxItems",
    "uniqueItems",
    "pattern",
    "format",
    "default",
}


def _strip(node: Any) -> Any:
    if isinstance(node, dict):
        cleaned = {k: _strip(v) for k, v in node.items() if k not in _UNSUPPORTED_KEYWORDS}
        if cleaned.get("type") == "object":
            cleaned.setdefault("additionalProperties", False)
            props = cleaned.get("properties") or {}
            cleaned["required"] = sorted(props.keys())
        return cleaned
    if isinstance(node, list):
        return [_strip(item) for item in node]
    return node


def json_schema_for(name: str) -> dict[str, Any]:
    """Provider-safe JSON Schema for a contract."""
    schema = copy.deepcopy(get_contract(name).model_json_schema())
    schema.pop("title", None)
    return _strip(schema)
