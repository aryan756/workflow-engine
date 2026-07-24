"""Customer-support triage workflow.

    intake ─┬─► classify ────┐
            └─► fetch_ctx ───┴─► route ─┬─(bug)─────► create_issue ──► draft_bug ──────┐
                                        ├─(billing)─► lookup_invoice ─► draft_billing ─┤
                                        └─(unclear)─► human_review ───► draft_clarify ─┘
                                                                                       │
                                                                  finalize ─► send_reply

`classify` and `fetch_context` fan out from `intake` and run concurrently.
`route` is a data-declared branch over the agent's validated decision.
`finalize` joins the three mutually-exclusive branches with join=any.
"""

from __future__ import annotations

from app.engine.definition import (
    EdgeDef,
    JoinPolicy,
    NodeDef,
    NodeType,
    WorkflowDefinition,
)

CLASSIFY_SYSTEM = (
    "You triage inbound customer-support tickets for a B2B SaaS company. "
    "Classify the ticket into exactly one of: 'bug' (the product is "
    "malfunctioning), 'billing' (invoices, charges, refunds, plans), or "
    "'unclear' (not enough information to route confidently). "
    "Set `confidence` to your genuine calibrated confidence in the category - "
    "anything below 0.6 will be routed to a human, which is the correct "
    "outcome when the ticket is ambiguous. Reply only with JSON matching the "
    "schema."
)

CLASSIFY_PROMPT = """Classify this support ticket.

Subject: {subject}

Body:
{message}

Channel: {channel}
"""

DRAFT_SYSTEM = (
    "You write customer-support replies for a B2B SaaS company. Be concise, "
    "specific and warm. Only state facts present in the supplied context - "
    "never invent ticket numbers, dates or amounts. Reply only with JSON "
    "matching the schema."
)

FINALIZE_SYSTEM = (
    "You are the final quality gate for an automated support workflow. Turn "
    "the drafted reply into the message that will actually be sent, and record "
    "the resolution state. Reply only with JSON matching the schema."
)


SUPPORT_TRIAGE = WorkflowDefinition(
    id="support_triage",
    name="Customer Support Triage",
    description=(
        "Classify an inbound support request, gather account context, take the "
        "bug / billing / human-review path, and send one final reply."
    ),
    # The run's result is the final customer-facing response; `send_reply` is
    # the delivery step that follows it.
    output_node="finalize",
    nodes=(
        NodeDef(
            id="intake",
            type=NodeType.INPUT,
            title="Receive request",
            description="Validate the inbound ticket against the SupportTicket contract.",
            config={
                "contract": "support_ticket",
                "inputs": {"payload": "$.run.input"},
            },
        ),
        NodeDef(
            id="classify",
            type=NodeType.AGENT,
            title="Classify issue",
            description="Agent decision: bug / billing / unclear, with confidence.",
            max_attempts=2,
            config={
                "task": "classify_ticket",
                "contract": "ticket_classification",
                "system": CLASSIFY_SYSTEM,
                "prompt_template": CLASSIFY_PROMPT,
                "max_repair_attempts": 2,
                "inputs": {
                    "subject": "$.nodes.intake.output.subject",
                    "message": "$.nodes.intake.output.message",
                    "channel": "$.nodes.intake.output.channel",
                },
            },
        ),
        NodeDef(
            id="fetch_context",
            type=NodeType.TOOL,
            title="Fetch customer context",
            description="Read-only CRM lookup; runs in parallel with classification.",
            max_attempts=3,
            retry_backoff_seconds=0.05,
            config={
                "tool": "crm.fetch_customer",
                "inputs": {"customer_id": "$.nodes.intake.output.customer_id"},
            },
        ),
        NodeDef(
            id="route",
            type=NodeType.BRANCH,
            title="Choose execution path",
            description=(
                "Low confidence always wins over the predicted category, so an "
                "unsure agent escalates to a human instead of acting."
            ),
            config={
                "inputs": {
                    "category": "$.nodes.classify.output.category",
                    "confidence": "$.nodes.classify.output.confidence",
                },
                "cases": [
                    {
                        "label": "unclear",
                        "when": {"var": "confidence", "op": "lt", "value": 0.6},
                    },
                    {
                        "label": "bug",
                        "when": {"var": "category", "op": "eq", "value": "bug"},
                    },
                    {
                        "label": "billing",
                        "when": {"var": "category", "op": "eq", "value": "billing"},
                    },
                ],
                "default": "unclear",
            },
        ),
        # --- bug path ---------------------------------------------------
        NodeDef(
            id="create_issue",
            type=NodeType.TOOL,
            title="Create Linear issue",
            description="SIDE EFFECTING - guarded by the idempotency ledger.",
            max_attempts=2,
            retry_backoff_seconds=0.05,
            config={
                "tool": "linear.create_issue",
                "inputs": {
                    "title": "$.nodes.classify.output.summary",
                    "description": "$.nodes.intake.output.message",
                    "priority": "$.nodes.classify.output.suggested_priority",
                    "customer_id": "$.nodes.intake.output.customer_id",
                },
            },
        ),
        NodeDef(
            id="draft_bug_reply",
            type=NodeType.AGENT,
            title="Draft bug reply",
            max_attempts=2,
            config={
                "task": "draft_bug_reply",
                "contract": "customer_reply",
                "system": DRAFT_SYSTEM,
                "prompt_template": (
                    "Write a reply to a customer whose bug report we have just "
                    "filed with engineering.\n\n"
                    "Ticket summary: {summary}\n\n"
                    "Original message:\n{message}\n\n"
                    "Customer account:\n{customer}\n\n"
                    "Filed issue:\n{issue}\n"
                ),
                "inputs": {
                    "summary": "$.nodes.classify.output.summary",
                    "message": "$.nodes.intake.output.message",
                    "customer": "$.nodes.fetch_context.output.result",
                    "issue": "$.nodes.create_issue.output.result",
                },
            },
        ),
        # --- billing path -----------------------------------------------
        NodeDef(
            id="lookup_invoice",
            type=NodeType.TOOL,
            title="Look up invoice",
            max_attempts=3,
            retry_backoff_seconds=0.05,
            config={
                "tool": "billing.lookup_invoice",
                "inputs": {"customer_id": "$.nodes.intake.output.customer_id"},
            },
        ),
        NodeDef(
            id="draft_billing_reply",
            type=NodeType.AGENT,
            title="Draft billing reply",
            max_attempts=2,
            config={
                "task": "draft_billing_reply",
                "contract": "customer_reply",
                "system": DRAFT_SYSTEM,
                "prompt_template": (
                    "Write a reply to a customer with a billing question.\n\n"
                    "Ticket summary: {summary}\n\n"
                    "Original message:\n{message}\n\n"
                    "Customer account:\n{customer}\n\n"
                    "Invoice on file:\n{invoice}\n"
                ),
                "inputs": {
                    "summary": "$.nodes.classify.output.summary",
                    "message": "$.nodes.intake.output.message",
                    "customer": "$.nodes.fetch_context.output.result",
                    "invoice": "$.nodes.lookup_invoice.output.result",
                },
            },
        ),
        # --- human review path -------------------------------------------
        NodeDef(
            id="human_review",
            type=NodeType.APPROVAL,
            title="Human approval",
            description=(
                "Pause. A reviewer approves sending a clarifying reply (and may "
                "attach a question to ask), or rejects the run."
            ),
            config={
                "prompt": (
                    "The classifier was not confident enough to route this ticket "
                    "automatically. Approve to send a clarifying reply, or reject "
                    "to stop the run."
                ),
                "inputs": {
                    "subject": "$.nodes.intake.output.subject",
                    "message": "$.nodes.intake.output.message",
                    "classification": "$.nodes.classify.output",
                    "customer": "$.nodes.fetch_context.output.result",
                },
            },
        ),
        NodeDef(
            id="draft_clarification_reply",
            type=NodeType.AGENT,
            title="Draft clarification",
            max_attempts=2,
            config={
                "task": "draft_clarification_reply",
                "contract": "customer_reply",
                "system": DRAFT_SYSTEM,
                "prompt_template": (
                    "The ticket below could not be classified confidently and a "
                    "human reviewer approved sending a clarifying reply.\n\n"
                    "Ticket summary: {summary}\n\n"
                    "Original message:\n{message}\n\n"
                    "Customer account:\n{customer}\n\n"
                    "Reviewer note (ask this if present):\n{approval}\n\n"
                    "Ask at most three specific questions that would let us route "
                    "this to the right team."
                ),
                "inputs": {
                    "summary": "$.nodes.classify.output.summary",
                    "message": "$.nodes.intake.output.message",
                    "customer": "$.nodes.fetch_context.output.result",
                    "approval": "$.nodes.human_review.output",
                },
            },
        ),
        # --- join + delivery ---------------------------------------------
        NodeDef(
            id="finalize",
            type=NodeType.AGENT,
            title="Produce final response",
            join=JoinPolicy.ANY,
            max_attempts=2,
            config={
                "task": "finalize_response",
                "contract": "final_response",
                "system": FINALIZE_SYSTEM,
                "prompt_template": (
                    "Path taken: {route}\n\n"
                    "Drafted reply:\n{reply}\n\n"
                    "Produce the final message to send and the resolution state."
                ),
                "inputs": {
                    "route": "$.nodes.route.output.selected",
                    "reply": {
                        "first_of": [
                            "$.nodes.draft_bug_reply.output",
                            "$.nodes.draft_billing_reply.output",
                            "$.nodes.draft_clarification_reply.output",
                        ]
                    },
                },
            },
        ),
        NodeDef(
            id="send_reply",
            type=NodeType.TOOL,
            title="Send reply",
            description="SIDE EFFECTING - the customer must not be emailed twice.",
            max_attempts=2,
            retry_backoff_seconds=0.05,
            config={
                "tool": "email.send_reply",
                "inputs": {
                    "to": "$.nodes.fetch_context.output.result.email",
                    "subject": "$.nodes.finalize.output.subject",
                    "body": "$.nodes.finalize.output.body",
                },
            },
        ),
    ),
    edges=(
        EdgeDef("intake", "classify"),
        EdgeDef("intake", "fetch_context"),
        EdgeDef("classify", "route"),
        EdgeDef("fetch_context", "route"),
        EdgeDef("route", "create_issue", label="bug"),
        EdgeDef("route", "lookup_invoice", label="billing"),
        EdgeDef("route", "human_review", label="unclear"),
        EdgeDef("create_issue", "draft_bug_reply"),
        EdgeDef("lookup_invoice", "draft_billing_reply"),
        EdgeDef("human_review", "draft_clarification_reply"),
        EdgeDef("draft_bug_reply", "finalize"),
        EdgeDef("draft_billing_reply", "finalize"),
        EdgeDef("draft_clarification_reply", "finalize"),
        EdgeDef("finalize", "send_reply"),
    ),
)
