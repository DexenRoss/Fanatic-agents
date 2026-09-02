"""Central application settings loaded from environment and local dotenv files."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ApplicationSettings(BaseSettings):
    """Secret-safe settings with environment variables taking precedence."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    openai_api_key: SecretStr | None = Field(
        default=None,
        validation_alias="OPENAI_API_KEY",
        exclude=True,
        repr=False,
    )
    fanatic_agents_model: str | None = Field(
        default=None,
        validation_alias="FANATIC_AGENTS_MODEL",
    )

    @field_validator("openai_api_key", "fanatic_agents_model", mode="before")
    @classmethod
    def empty_strings_are_unset(cls, value: Any) -> Any:
        """Treat blank dotenv/environment values as unconfigured."""
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return value

    @property
    def has_openai_api_key(self) -> bool:
        """Report key availability without revealing its value."""
        return self.openai_api_key is not None

    def openai_api_key_value(self) -> str | None:
        """Return the key only for the provider configuration boundary."""
        if self.openai_api_key is None:
            return None
        return self.openai_api_key.get_secret_value()


def get_settings(*, env_file: str | Path | None = None) -> ApplicationSettings:
    """Load settings from the environment and an explicit or local dotenv file."""
    if env_file is None:
        return ApplicationSettings()
    return ApplicationSettings(_env_file=Path(env_file))
