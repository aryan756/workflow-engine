"""Input node: validates the run payload against a declared contract."""

from __future__ import annotations

from pydantic import ValidationError

from app.agents.contracts import get_contract
from app.engine.errors import InputValidationError
from app.engine.handlers.base import ExecutionContext, NodeResult


class InputNodeHandler:
    async def execute(self, ctx: ExecutionContext) -> NodeResult:
        contract_name = ctx.config.get("contract")
        payload = ctx.inputs.get("payload", ctx.run.input_json or {})

        if not contract_name:
            ctx.logger.info("No contract declared; passing input through unchanged.")
            return NodeResult(output=dict(payload))

        model = get_contract(contract_name)
        try:
            validated = model.model_validate(payload)
        except ValidationError as exc:
            ctx.logger.error(
                f"Run input failed contract '{contract_name}'.",
                {"errors": exc.errors(include_url=False)},
            )
            raise InputValidationError(
                f"Run input does not satisfy contract '{contract_name}'",
                details={"errors": exc.errors(include_url=False)},
            ) from exc

        ctx.logger.info(f"Run input validated against contract '{contract_name}'.")
        return NodeResult(output=validated.model_dump(mode="json"))
