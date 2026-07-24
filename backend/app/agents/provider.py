"""The provider seam.

An agent node never talks to a vendor SDK directly. It builds an
:class:`AgentRequest` and hands it to whatever provider is configured, which
makes the deterministic mock and the real Claude client fully interchangeable
- including in tests, where a fault-injecting provider is swapped in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class AgentRequest:
    #: stable identifier for the agent's job, e.g. "classify_ticket".
    #: The mock provider dispatches on it; Claude ignores it.
    task: str
    system: str
    prompt: str
    json_schema: dict[str, Any]
    inputs: dict[str, Any] = field(default_factory=dict)
    #: validation feedback from a previous attempt, used for the repair loop
    repair_hint: str | None = None


@dataclass
class AgentResponse:
    output: dict[str, Any]
    provider: str
    model: str | None = None
    usage: dict[str, Any] | None = None
    raw_text: str | None = None


@runtime_checkable
class LLMProvider(Protocol):
    name: str

    async def generate_json(self, request: AgentRequest) -> AgentResponse:
        """Return a JSON object that *should* satisfy ``request.json_schema``.

        Providers are not required to guarantee the shape - the engine
        validates against the Pydantic contract regardless.
        """
        ...
