"""Small shared boundaries for tool-free structured agents."""

from __future__ import annotations

from typing import Any, Protocol, TypeVar

from agents import Agent, set_default_openai_key
from pydantic import BaseModel

from fanatic_agents.core.settings import ApplicationSettings, get_settings


class SynchronousRunner(Protocol):
    """Injection boundary around the Agents SDK synchronous runner."""

    def run_sync(
        self,
        starting_agent: Agent[Any],
        input: str,
        *,
        max_turns: int,
    ) -> Any: ...


class AgentExecutionError(RuntimeError):
    """Safe domain error for a failed structured agent call."""


class OpenAIConfigurationError(RuntimeError):
    """Safe domain error for provider initialization failures."""


StructuredOutput = TypeVar("StructuredOutput", bound=BaseModel)


def resolve_model(
    model: str | None, settings: ApplicationSettings | None = None
) -> str | None:
    """Resolve the optional model without hard-coding a provider model."""
    if model is not None:
        return model
    return (settings or get_settings()).fanatic_agents_model


def configure_openai_sdk(settings: ApplicationSettings | None = None) -> bool:
    """Configure the provider from secret settings without logging the key."""
    application_settings = settings or get_settings()
    api_key = application_settings.openai_api_key_value()
    if api_key is None:
        return False
    try:
        set_default_openai_key(api_key)
    except Exception:
        raise OpenAIConfigurationError(
            "OpenAI SDK configuration failed; no agent was called."
        ) from None
    return True


def run_structured_agent(
    *,
    runner: SynchronousRunner,
    agent: Agent[Any],
    prompt: str,
    output_type: type[StructuredOutput],
    role: str,
) -> StructuredOutput:
    """Run exactly one agent turn and sanitize provider/internal failures."""
    try:
        result = runner.run_sync(agent, prompt, max_turns=1)
    except Exception as exc:
        raise AgentExecutionError(
            f"{role} failed; the workflow stopped safely."
        ) from exc
    output = result.final_output
    if not isinstance(output, output_type):
        raise AgentExecutionError(
            f"{role} returned an unexpected structured output type."
        )
    return output
