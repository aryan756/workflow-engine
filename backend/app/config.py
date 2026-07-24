"""Application settings.

Everything is env-overridable so a reviewer can run the app with zero setup
(deterministic mock agent, local SQLite) or flip a single env var to run the
same workflows against the real Claude API.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- storage -----------------------------------------------------------
    database_url: str = "sqlite+aiosqlite:///./agentic_workflow.db"

    # --- agent provider ----------------------------------------------------
    # "auto"  -> Claude when ANTHROPIC_API_KEY is set, otherwise the mock
    # "mock"  -> always the deterministic rule-based provider
    # "claude"-> always Claude (fails loudly if no key is configured)
    agent_provider: Literal["auto", "mock", "claude"] = "auto"
    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-opus-4-8"
    anthropic_max_tokens: int = 4096
    # Effort trades thinking depth against latency/cost. These agent calls are
    # short structured-extraction tasks, so "low" is the right default.
    anthropic_effort: Literal["low", "medium", "high", "xhigh", "max"] = "low"

    # --- debugging -----------------------------------------------------------
    # `options.faults` on a run lets a caller force node failures. That is the
    # point of the debugger, but it is a foot-gun in a real deployment - any
    # client could poison runs. On by default here, one env var to turn off.
    enable_fault_injection: bool = True

    # --- http --------------------------------------------------------------
    cors_origins: list[str] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]

    @property
    def resolved_provider(self) -> Literal["mock", "claude"]:
        if self.agent_provider == "auto":
            return "claude" if self.anthropic_api_key else "mock"
        return self.agent_provider


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
