"""Human-approval node.

First execution opens an ApprovalRequest and parks the node in
WAITING_APPROVAL - the engine stops scheduling that branch and the run reports
itself as waiting. When a decision arrives via the API the node is put back to
PENDING; this handler then re-runs, finds the decided request and either
succeeds (carrying the human's payload downstream) or fails with
`approval_rejected`.
"""

from __future__ import annotations

from sqlalchemy import select

from app.engine.errors import ApprovalRejectedError
from app.engine.handlers.base import ExecutionContext, NodeResult
from app.engine.states import NodeStatus
from app.models import ApprovalRequest, new_id


class ApprovalNodeHandler:
    async def execute(self, ctx: ExecutionContext) -> NodeResult:
        request = (
            await ctx.session.execute(
                select(ApprovalRequest)
                .where(
                    ApprovalRequest.run_id == ctx.run.id,
                    ApprovalRequest.node_id == ctx.node.id,
                    ApprovalRequest.status != "superseded",
                )
                # id breaks ties: two requests created in the same clock tick
                # would otherwise order non-deterministically.
                .order_by(ApprovalRequest.created_at.desc(), ApprovalRequest.id.desc())
            )
        ).scalars().first()

        # --- decision already made -> resolve the node -------------------
        if request is not None and request.status != "pending":
            if request.status == "approved":
                ctx.logger.info(
                    "Approval granted; continuing.",
                    {
                        "decided_by": request.decided_by,
                        "note": request.note,
                        "payload": request.payload_json,
                    },
                )
                return NodeResult(
                    output={
                        "decision": "approved",
                        "note": request.note,
                        "payload": request.payload_json or {},
                        "decided_by": request.decided_by,
                        "decided_at": request.decided_at.isoformat()
                        if request.decided_at
                        else None,
                    }
                )

            ctx.logger.error(
                "Approval rejected.",
                {"decided_by": request.decided_by, "note": request.note},
            )
            raise ApprovalRejectedError(
                f"Human reviewer rejected this step: {request.note or 'no reason given'}",
                details={"decided_by": request.decided_by},
            )

        # --- open a fresh request and park --------------------------------
        if request is None:
            request = ApprovalRequest(
                id=new_id("apr"),
                run_id=ctx.run.id,
                node_id=ctx.node.id,
                status="pending",
                prompt=str(ctx.config.get("prompt", "Approve this step?")),
                context_json=ctx.inputs,
            )
            ctx.session.add(request)
            ctx.logger.info(
                "Waiting for human approval.",
                {"approval_id": request.id, "prompt": request.prompt},
            )
        else:
            ctx.logger.info(
                "Still waiting for human approval.", {"approval_id": request.id}
            )

        return NodeResult(status=NodeStatus.WAITING_APPROVAL, output=None)
