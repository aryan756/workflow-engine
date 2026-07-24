"""Agent / decision node.

This is where the dynamic behaviour lives. The node:

  1. renders a prompt from resolved upstream inputs,
  2. asks the configured provider for JSON,
  3. validates that JSON against the node's declared Pydantic contract,
  4. on validation failure, re-prompts with the error (bounded repair loop),
  5. only then lets the engine schedule anything downstream.

Step 3 is the hard gate the brief asks for: an unvalidated agent decision can
never reach a tool call.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import ValidationError

from app.agents.contracts import get_contract, json_schema_for
from app.agents.provider import AgentRequest
from app.engine.errors import AgentOutputValidationError, NodeExecutionError
from app.engine.handlers.base import ExecutionContext, NodeResult

DEFAULT_SYSTEM = (
    "You are a support-operations agent inside an automated workflow. "
    "Answer only with a JSON object matching the provided schema. "
    "Be factual and base every field strictly on the supplied context."
)


# Deliberately NOT str.format: prompt templates are author-written data and
# routinely contain literal braces (a JSON example, a code snippet). `format`
# would treat those as field specs and either raise or silently corrupt them.
# This substitutes only exact `{identifier}` placeholders and leaves every
# other brace untouched.
_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def render_input(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, indent=2, sort_keys=True, default=str)
    return str(value)


def render_template(template: str, inputs: dict[str, Any]) -> str:
    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in inputs:
            return match.group(0)  # unknown placeholder passes through verbatim
        return render_input(inputs[key])

    return _PLACEHOLDER.sub(replace, template)


class AgentNodeHandler:
    async def execute(self, ctx: ExecutionContext) -> NodeResult:
        config = ctx.config
        task = config["task"]
        contract_name = config["contract"]
        model = get_contract(contract_name)
        schema = json_schema_for(contract_name)
        max_repairs = int(config.get("max_repair_attempts", 2))

        prompt = render_template(config.get("prompt_template", "{context}"), ctx.inputs)
        system = config.get("system", DEFAULT_SYSTEM)

        ctx.logger.info(
            f"Agent task '{task}' -> contract '{contract_name}'.",
            {"provider": ctx.provider.name, "prompt_chars": len(prompt)},
        )

        # Fault injection is evaluated once per node attempt, so `times: 2` on a
        # node with max_attempts=2 poisons exactly the automatic attempts and
        # lets a manual retry succeed.
        force_invalid = ctx.fault_active("agent_invalid_output")
        force_error = not force_invalid and ctx.fault_active("agent_error")
        if force_invalid or force_error:
            ctx.consume_fault()

        repair_hint: str | None = None
        last_error: ValidationError | None = None
        last_raw: dict[str, Any] | None = None

        for repair in range(max_repairs + 1):
            raw = await self._invoke_provider(
                ctx,
                AgentRequest(
                    task=task,
                    system=system,
                    prompt=prompt,
                    json_schema=schema,
                    inputs=ctx.inputs,
                    repair_hint=repair_hint,
                ),
                repair=repair,
                force_invalid=force_invalid,
                force_error=force_error,
            )
            last_raw = raw

            try:
                validated = model.model_validate(raw)
            except ValidationError as exc:
                last_error = exc
                errors = exc.errors(include_url=False)
                ctx.logger.warning(
                    f"Agent output failed contract validation "
                    f"(repair {repair}/{max_repairs}).",
                    {"raw_output": raw, "errors": errors},
                )
                repair_hint = json.dumps(errors, default=str)[:1500]
                continue

            ctx.logger.info(
                "Agent output validated.",
                {"repairs_used": repair, "output": validated.model_dump(mode="json")},
            )
            return NodeResult(output=validated.model_dump(mode="json"))

        assert last_error is not None
        raise AgentOutputValidationError(
            f"Agent output failed contract '{contract_name}' after "
            f"{max_repairs + 1} attempt(s)",
            details={
                "errors": last_error.errors(include_url=False),
                "last_raw_output": last_raw,
            },
        )

    async def _invoke_provider(
        self,
        ctx: ExecutionContext,
        request: AgentRequest,
        *,
        repair: int,
        force_invalid: bool = False,
        force_error: bool = False,
    ) -> dict[str, Any]:
        # --- dev/test fault injection -----------------------------------
        if force_invalid:
            ctx.logger.warning(
                "Fault injection: forcing a schema-violating agent response.",
                {"kind": "agent_invalid_output", "repair": repair},
            )
            return {"category": "definitely-not-a-valid-category", "confidence": 4.2}

        if force_error:
            ctx.logger.warning(
                "Fault injection: simulating a provider failure.", {"kind": "agent_error"}
            )
            raise NodeExecutionError(
                "Injected agent provider failure",
                code="agent_provider_error",
                retryable=True,
            )

        response = await ctx.provider.generate_json(request)
        ctx.logger.debug(
            "Provider responded.",
            {
                "provider": response.provider,
                "model": response.model,
                "usage": response.usage,
                "repair": repair,
            },
        )
        return response.output
