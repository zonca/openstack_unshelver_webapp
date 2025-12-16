from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from pydantic import AnyUrl, BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


DEFAULT_CONFIG_PATH = "config.yaml"
CONFIG_ENV_VAR = "UNSHELVER_CONFIG"


class AppSettings(BaseModel):
    """Application-level settings."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(default="OpenStack Unshelver")
    secret_key: str = Field(min_length=16)
    poll_interval_seconds: int = Field(default=10, ge=1)
    http_probe_timeout: float = Field(default=5.0, gt=0)
    http_probe_attempts: int = Field(default=12, ge=1)
    control_token: str = Field(min_length=16, description="Shared secret for /control and admin endpoints")
    manual_shelve_path: str = Field(
        default="/admin-shelve",
        description="Hidden path that exposes manual shelve controls",
        pattern=r"^/[-/a-zA-Z0-9_]+$",
    )


class OpenStackSettings(BaseModel):
    """OpenStack credential and connection details."""

    model_config = ConfigDict(extra="allow")  # Allow additional auth parameters

    auth_url: AnyUrl
    username: Optional[str] = None
    password: Optional[str] = None
    project_name: Optional[str] = None
    user_domain_name: Optional[str] = None
    project_domain_name: Optional[str] = None
    region_name: Optional[str] = None
    interface: Optional[str] = None
    application_credential_id: Optional[str] = None
    application_credential_secret: Optional[str] = None
    verify: Optional[bool | str] = True

    @model_validator(mode="after")
    def validate_credentials(self) -> "OpenStackSettings":
        basic_fields = [self.username, self.password, self.project_name]
        application_fields = [self.application_credential_id, self.application_credential_secret]
        if all(basic_fields):
            return self
        if all(application_fields):
            return self
        raise ValueError(
            "OpenStack configuration must specify either username/password/project_name or "
            "application_credential_id/application_credential_secret"
        )


class ButtonSettings(BaseModel):
    """Defines a UI button and the instance it controls."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$", description="Unique identifier used in routes")
    label: str
    instance_name: str
    description: Optional[str] = None
    preferred_networks: Optional[List[str]] = None
    url_scheme: str = Field(default="http", pattern=r"^[a-zA-Z][a-zA-Z0-9+.-]*$")
    port: Optional[int] = Field(default=None, ge=1, le=65535)
    healthcheck_path: str = Field(default="/")
    launch_path: Optional[str] = None
    http_probe_attempts: Optional[int] = Field(default=None, ge=1)
    http_probe_interval_seconds: Optional[int] = Field(default=None, ge=1)
    verify_tls: bool = True

    @field_validator("healthcheck_path")
    @classmethod
    def normalise_healthcheck_path(cls, value: str) -> str:
        return value if value.startswith("/") else f"/{value}"

    @field_validator("launch_path")
    @classmethod
    def normalise_launch_path(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        return value if value.startswith("/") else f"/{value}"


class Settings(BaseModel):
    """Full application configuration model."""

    model_config = ConfigDict(extra="forbid")

    app: AppSettings
    openstack: OpenStackSettings
    buttons: List[ButtonSettings]
    activity_log_path: str = Field(
        description="Path to the Caddy JSON access log used for idle detection",
    )
    idle_timeout_minutes: int = Field(default=60, ge=5)
    idle_poll_interval_seconds: int = Field(default=30, ge=5)
    caddy_upstream_label: str = Field(default="gpu", description="Value matched against log upstream.name")
    local_event_log: str = Field(default="logs/unshelver-events.jsonl")
    swift_event_container: Optional[str] = Field(default=None)
    swift_event_prefix: str = Field(default="unshelver-events")

    @model_validator(mode="after")
    def validate_buttons(self) -> "Settings":
        if len(self.buttons) != 1:
            raise ValueError("Exactly one button must be configured for the controller deployment")
        ids = [button.id for button in self.buttons]
        if len(ids) != len(set(ids)):
            raise ValueError("Button IDs must be unique")
        instance_names = [button.instance_name for button in self.buttons]
        if len(instance_names) != len(set(instance_names)):
            raise ValueError("Instance names must be unique across buttons")
        return self


class ConfigurationError(RuntimeError):
    """Raised when configuration loading fails."""


def _load_yaml(path: Path) -> Dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return yaml.safe_load(handle) or {}
    except FileNotFoundError as exc:
        raise ConfigurationError(f"Configuration file not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"Failed to parse YAML configuration: {exc}") from exc


def load_settings(path: Optional[str] = None) -> Settings:
    """Load settings from a YAML file, defaulting to UNSHELVER_CONFIG or config.yaml."""

    resolved_path = Path(path or os.environ.get(CONFIG_ENV_VAR, DEFAULT_CONFIG_PATH))
    raw_config = _load_yaml(resolved_path)
    try:
        return Settings.model_validate(raw_config)
    except ValidationError as exc:
        errors = exc.errors(include_url=False)
        raise ConfigurationError(f"Configuration validation error: {errors}") from exc
