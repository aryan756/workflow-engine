"""Engine error taxonomy.

`retryable` is what separates "the engine should try again on its own" from
"a human has to look at this". It is surfaced in the node trace so the UI can
tell an operator whether a retry is likely to help.
"""

from __future__ import annotations

from typing import Any


class EngineError(Exception):
    """Base for everything the engine raises."""


class WorkflowDefinitionError(EngineError):
    """The workflow DAG itself is invalid (cycle, dangling edge, ...)."""


class NodeExecutionError(EngineError):
    """A node attempt failed."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "node_failed",
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.details = details or {}


class InputValidationError(NodeExecutionError):
    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            message, code="input_validation_failed", retryable=False, details=details
        )


class AgentOutputValidationError(NodeExecutionError):
    """The agent produced output that does not satisfy its declared contract.

    Retryable: a fresh sample (or a corrected prompt) may well satisfy it.
    """

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            message, code="agent_output_validation_failed", retryable=True, details=details
        )


class AgentProviderError(NodeExecutionError):
    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message, code="agent_provider_error", retryable=retryable)


class ToolTransientError(NodeExecutionError):
    """Tool failed in a way that is worth retrying (timeout, 503, ...)."""

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="tool_transient_error", retryable=True, details=details)


class ToolPermanentError(NodeExecutionError):
    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="tool_permanent_error", retryable=False, details=details)


class ApprovalRejectedError(NodeExecutionError):
    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="approval_rejected", retryable=True, details=details)


class ResolutionError(NodeExecutionError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="input_resolution_failed", retryable=False)
