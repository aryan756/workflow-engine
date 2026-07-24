"""Tool node with an idempotency ledger.

Every invocation is keyed by ``sha256(run_id | node_id | tool | canonical args)``
and recorded in the ``tool_calls`` table under a UNIQUE constraint. If a node is
retried and resolves to the same arguments, the recorded response is replayed
instead of calling the tool again - so a retry after a post-tool failure cannot
create a second Linear issue or send a second email.

The ledger row is committed the moment the side effect succeeds, *before* any
later failure in the same node. That ordering is what makes the guarantee hold:
the durable record of the effect always outlives the attempt that produced it.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select

from app.engine.errors import NodeExecutionError, ToolPermanentError, ToolTransientError
from app.engine.handlers.base import ExecutionContext, NodeResult
from app.models import ToolCall, new_id, utcnow


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def idempotency_key(run_id: str, node_id: str, tool_name: str, args: dict[str, Any]) -> str:
    material = f"{run_id}|{node_id}|{tool_name}|{canonical_json(args)}"
    return hashlib.sha256(material.encode()).hexdigest()[:48]


class ToolNodeHandler:
    async def execute(self, ctx: ExecutionContext) -> NodeResult:
        config = ctx.config
        tool_name = config["tool"]
        spec = ctx.tools.get(tool_name)
        args = dict(ctx.inputs)

        missing = [a for a in spec.required_args if args.get(a) in (None, "")]
        if missing:
            raise ToolPermanentError(
                f"tool '{tool_name}' is missing required arguments: {missing}",
                details={"args": args},
            )

        key = idempotency_key(ctx.run.id, ctx.node.id, tool_name, args)

        existing = (
            await ctx.session.execute(
                select(ToolCall).where(ToolCall.idempotency_key == key)
            )
        ).scalar_one_or_none()

        # --- replay path -------------------------------------------------
        if existing is not None and existing.status == "succeeded":
            existing.replayed_count += 1
            ctx.logger.info(
                f"Idempotent replay: '{tool_name}' already succeeded for these "
                "arguments; returning the recorded response without re-invoking it.",
                {
                    "idempotency_key": key,
                    "tool_call_id": existing.id,
                    "replayed_count": existing.replayed_count,
                    "side_effecting": spec.side_effecting,
                },
            )
            await ctx.session.commit()
            await self._maybe_post_effect_fault(ctx, tool_name, key)
            return NodeResult(
                output={
                    "tool": tool_name,
                    "result": existing.response_json,
                    "idempotency_key": key,
                    "replayed": True,
                }
            )

        if existing is not None and existing.status == "in_progress":
            # A crash between "started" and "finished". We cannot know whether
            # the effect landed, so we re-run and say so loudly in the trace.
            ctx.logger.warning(
                f"Previous invocation of '{tool_name}' never completed; re-invoking "
                "(at-least-once for this case).",
                {"idempotency_key": key, "tool_call_id": existing.id},
            )
            call = existing
            call.attempt += 1
            call.status = "in_progress"
            call.error = None
        elif existing is not None:  # previously failed -> genuine retry
            ctx.logger.info(
                f"Previous invocation of '{tool_name}' failed; retrying.",
                {"idempotency_key": key, "previous_error": existing.error},
            )
            call = existing
            call.attempt += 1
            call.status = "in_progress"
            call.error = None
        else:
            call = ToolCall(
                id=new_id("tc"),
                run_id=ctx.run.id,
                node_id=ctx.node.id,
                tool_name=tool_name,
                idempotency_key=key,
                status="in_progress",
                attempt=ctx.attempt,
                request_json=args,
            )
            ctx.session.add(call)

        # Persist the "about to run" marker before touching the outside world.
        await ctx.session.commit()

        # --- dev/test fault: fail *before* the side effect ----------------
        if ctx.fault_active("tool_transient"):
            ctx.consume_fault()
            call.status = "failed"
            call.error = "Injected transient failure before invocation"
            call.completed_at = utcnow()
            ctx.logger.warning(
                "Fault injection: transient failure before the tool ran "
                "(no side effect).",
                {"kind": "tool_transient", "idempotency_key": key},
            )
            await ctx.session.commit()
            raise ToolTransientError(
                f"Injected transient failure calling '{tool_name}'",
                details={"idempotency_key": key},
            )

        ctx.logger.info(
            f"Invoking tool '{tool_name}'.",
            {"args": args, "idempotency_key": key, "side_effecting": spec.side_effecting},
        )

        try:
            result = await spec.handler(args)
        except NodeExecutionError as exc:
            call.status = "failed"
            call.error = str(exc)
            call.completed_at = utcnow()
            ctx.logger.error(f"Tool '{tool_name}' failed: {exc}", {"code": exc.code})
            await ctx.session.commit()
            raise
        except Exception as exc:
            call.status = "failed"
            call.error = repr(exc)
            call.completed_at = utcnow()
            ctx.logger.error(f"Tool '{tool_name}' raised {type(exc).__name__}: {exc}")
            await ctx.session.commit()
            raise ToolTransientError(
                f"tool '{tool_name}' raised {type(exc).__name__}: {exc}",
                details={"idempotency_key": key},
            ) from exc

        call.status = "succeeded"
        call.response_json = result
        call.completed_at = utcnow()
        ctx.logger.info(
            f"Tool '{tool_name}' succeeded.",
            {"result": result, "idempotency_key": key},
        )
        # Commit the effect *now*. Anything that fails after this point must
        # not be able to un-record it.
        await ctx.session.commit()

        await self._maybe_post_effect_fault(ctx, tool_name, key)

        return NodeResult(
            output={
                "tool": tool_name,
                "result": result,
                "idempotency_key": key,
                "replayed": False,
            }
        )

    @staticmethod
    async def _maybe_post_effect_fault(
        ctx: ExecutionContext, tool_name: str, key: str
    ) -> None:
        """Dev/test fault: fail the node *after* the effect is durably recorded.

        Deliberately fires on the replay path too, so a fault with
        ``times: 2`` can exhaust a node's automatic attempts and force an
        operator retry - while the tool itself is still only ever invoked once.
        """
        if not ctx.fault_active("tool_after_side_effect"):
            return
        ctx.consume_fault()
        ctx.logger.warning(
            "Fault injection: node failing after the tool result was already "
            "recorded. A retry must replay it, not re-invoke the tool.",
            {"kind": "tool_after_side_effect", "idempotency_key": key},
        )
        await ctx.session.commit()
        raise ToolTransientError(
            f"Injected post-commit failure after '{tool_name}' succeeded",
            details={"idempotency_key": key},
        )
