"""Composition root: builds the engine and its collaborators once per process."""

from __future__ import annotations

import logging

from fastapi import Request

from app.agents.claude import ClaudeProvider
from app.agents.mock import MockProvider
from app.agents.provider import LLMProvider
from app.config import Settings
from app.engine.executor import WorkflowEngine
from app.tools.mock_tools import build_default_registry
from app.workflows import build_registry

logger = logging.getLogger(__name__)


def build_provider(settings: Settings) -> LLMProvider:
    if settings.resolved_provider == "claude":
        if settings.agent_provider == "claude" and not settings.anthropic_api_key:
            logger.warning(
                "agent_provider=claude but ANTHROPIC_API_KEY is unset; relying on "
                "the SDK's ambient credential resolution."
            )
        logger.info("Agent provider: Claude (%s)", settings.anthropic_model)
        return ClaudeProvider(
            api_key=settings.anthropic_api_key,
            model=settings.anthropic_model,
            max_tokens=settings.anthropic_max_tokens,
            effort=settings.anthropic_effort,
        )
    logger.info("Agent provider: deterministic mock (set ANTHROPIC_API_KEY to use Claude)")
    return MockProvider()


def build_engine(settings: Settings, session_factory) -> WorkflowEngine:
    return WorkflowEngine(
        session_factory=session_factory,
        workflows=build_registry(),
        provider=build_provider(settings),
        tools=build_default_registry(),
        allow_fault_injection=settings.enable_fault_injection,
    )


def get_engine(request: Request) -> WorkflowEngine:
    return request.app.state.engine


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings
