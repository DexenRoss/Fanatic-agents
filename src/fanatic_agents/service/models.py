"""Strict models for managed-service metadata and read-only reports."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from fanatic_agents.core.project import NonEmptyStrictString, StrictModel


class ManagedServiceReceipt(StrictModel):
    """Tamper-evident public metadata for one explicitly installed service."""

    schema_version: Literal[1] = 1
    service_name: NonEmptyStrictString
    manager: Literal["systemd_user"] = "systemd_user"
    repository: NonEmptyStrictString
    github_repository: NonEmptyStrictString
    main_branch: NonEmptyStrictString
    config_path: NonEmptyStrictString
    config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    image: NonEmptyStrictString
    deliver_authorized: bool = False
    executable: NonEmptyStrictString
    env_file_path: NonEmptyStrictString | None = None
    unit_path: NonEmptyStrictString
    unit_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    installed_at: datetime
    enabled_at: datetime | None = None
    started_at: datetime | None = None
    updated_at: datetime
    installation_status: Literal["installed"] = "installed"

    @model_validator(mode="after")
    def timestamps_are_timezone_aware(self) -> "ManagedServiceReceipt":
        for value in (
            self.installed_at,
            self.enabled_at,
            self.started_at,
            self.updated_at,
        ):
            if value is not None and value.utcoffset() is None:
                raise ValueError("service receipt timestamps must be timezone-aware")
        return self


class PlatformCheck(StrictModel):
    """Structured read-only systemd user-manager capability report."""

    platform: NonEmptyStrictString
    systemd_available: bool
    systemctl_available: bool
    user_manager_reachable: bool
    supported: bool
    wsl_detected: bool | None = None
    reason: NonEmptyStrictString | None = None


class ManagedServiceStatus(StrictModel):
    """Read-only state for one installed managed service."""

    installed: bool
    enabled: bool | None = None
    active: bool | None = None
    systemd_state: str | None = None
    service_name: str | None = None
    manager: Literal["systemd_user"] | None = None
    repository: str
    config_drift: bool | None = None
    unit_drift: bool | None = None
    scheduler_state: str | None = None
