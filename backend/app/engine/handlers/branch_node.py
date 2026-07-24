"""Branch node.

Predicates are declared as data (see :mod:`app.engine.predicates`), so the
routing rules are readable in the UI, diffable in review, and validated when
the workflow is registered rather than when a run hits them:

    "cases": [
        {"label": "unclear", "when": {"var": "confidence", "op": "lt", "value": 0.6}},
        {"label": "bug",     "when": {"var": "category",   "op": "eq", "value": "bug"}},
    ],
    "default": "unclear"

Cases are evaluated top to bottom; the first match wins. Every case's outcome
is recorded in the node output, so the routing decision stays auditable after
the fact instead of having to be reconstructed from logs.
"""

from __future__ import annotations

from typing import Any

from app.engine.errors import NodeExecutionError
from app.engine.handlers.base import ExecutionContext, NodeResult
from app.engine.predicates import PredicateError, evaluate


class BranchNodeHandler:
    async def execute(self, ctx: ExecutionContext) -> NodeResult:
        cases = ctx.config.get("cases", [])
        default = ctx.config.get("default")

        evaluated: list[dict[str, Any]] = []
        selected: str | None = None
        matched_index: int | None = None

        for index, case in enumerate(cases):
            try:
                matched = evaluate(case["when"], ctx.inputs)
            except PredicateError as exc:
                raise NodeExecutionError(
                    f"branch case {index} ('{case.get('label')}') is invalid: {exc}",
                    code="branch_config_error",
                ) from exc

            evaluated.append({"label": case["label"], "when": case["when"], "matched": matched})
            if matched and selected is None:
                selected = case["label"]
                matched_index = index

        if selected is None:
            if not default:
                raise NodeExecutionError(
                    "no branch case matched and no default label is configured",
                    code="branch_no_match",
                )
            selected = default

        ctx.logger.info(
            f"Branch selected '{selected}'.",
            {"inputs": ctx.inputs, "cases": evaluated, "default": default},
        )

        return NodeResult(
            output={
                "selected": selected,
                "matched_case": matched_index,
                "used_default": matched_index is None,
                "evaluated": evaluated,
                "inputs": ctx.inputs,
            },
            selected_labels=[selected],
        )
