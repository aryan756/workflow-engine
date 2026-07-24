"""Claude-backed agent provider.

Uses the Anthropic Messages API with structured outputs, so the model is
constrained to the same JSON Schema the node's Pydantic contract describes.
The engine still re-validates locally - a provider is never trusted.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from anthropic import (
    APIConnectionError,
    APIStatusError,
    AsyncAnthropic,
    RateLimitError,
)

from app.agents.provider import AgentRequest, AgentResponse
from app.engine.errors import AgentProviderError

logger = logging.getLogger(__name__)


class ClaudeProvider:
    """Calls the Claude Messages API and returns parsed JSON."""

    name = "claude"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = "claude-opus-4-8",
        max_tokens: int = 4096,
        effort: str = "low",
    ) -> None:
        # A bare AsyncAnthropic() also picks up ANTHROPIC_API_KEY / an
        # `ant auth login` profile, so api_key stays optional.
        self._client = AsyncAnthropic(api_key=api_key) if api_key else AsyncAnthropic()
        self._model = model
        self._max_tokens = max_tokens
        self._effort = effort

    async def generate_json(self, request: AgentRequest) -> AgentResponse:
        prompt = request.prompt
        if request.repair_hint:
            prompt = (
                f"{prompt}\n\n"
                "Your previous answer failed schema validation with the following "
                f"error:\n{request.repair_hint}\n"
                "Return a corrected JSON object that satisfies the schema exactly."
            )

        try:
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                system=request.system,
                messages=[{"role": "user", "content": prompt}],
                output_config={
                    "format": {"type": "json_schema", "schema": request.json_schema},
                    "effort": self._effort,
                },
            )
        except RateLimitError as exc:
            raise AgentProviderError(f"Claude rate limited: {exc}", retryable=True) from exc
        except APIConnectionError as exc:
            raise AgentProviderError(f"Claude connection error: {exc}", retryable=True) from exc
        except APIStatusError as exc:
            raise AgentProviderError(
                f"Claude API error {exc.status_code}: {exc}",
                retryable=exc.status_code >= 500,
            ) from exc

        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            raise AgentProviderError(
                f"Claude refused the request (category={getattr(details, 'category', None)})",
                retryable=False,
            )
        if response.stop_reason == "max_tokens":
            raise AgentProviderError(
                "Claude response was truncated by max_tokens; raise anthropic_max_tokens",
                retryable=True,
            )

        text = next((b.text for b in response.content if b.type == "text"), None)
        if not text:
            raise AgentProviderError("Claude returned no text content", retryable=True)

        try:
            output: Any = json.loads(text)
        except json.JSONDecodeError as exc:
            raise AgentProviderError(
                f"Claude returned non-JSON output: {exc}", retryable=True
            ) from exc

        if not isinstance(output, dict):
            raise AgentProviderError(
                f"Claude returned a {type(output).__name__}, expected a JSON object",
                retryable=True,
            )

        return AgentResponse(
            output=output,
            provider=self.name,
            model=response.model,
            usage={
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
            },
            raw_text=text,
        )
