"""Resolve service base URLs fresh from the configuration, without building ``Settings`` (C14).

The Console needs the GitHub base URLs (``github_api_url``, ``github_web_url``)
while setup is still in progress, when the full ``Settings`` may not validate
yet, and it needs what the configuration says *now*: ``get_settings()`` is a
process-wide cache, so an edit made through the Console would otherwise be
invisible until a restart. A silent fallback to github.com is not acceptable
either -- a user pointing Henchmen at a fake or at GitHub Enterprise must never
have a manifest code or a browser sent somewhere else.

:func:`resolve_github_endpoints` therefore reads only those two fields, on
every call, through a small pydantic-settings model with the same names,
defaults and validation as ``Settings``:

* sources rank as they do at runtime -- the process environment above the
  configuration file above the default; a blank value means the default;
* keys ``henchmen serve`` seeded into its own environment (``seeded_env``) are
  ignored whenever the file sets them, exactly as
  :func:`henchmen.config.validation.settings_problems` masks them, so the file
  decides as it will after the next restart;
* each value must pass :func:`henchmen.config.settings.require_secure_github_url`;
  an invalid value raises :class:`EndpointError` naming the field, never the
  value, and nothing falls back.

Phase 2C's other configurable endpoints (C14) belong in this module too.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, ValidationError, ValidationInfo, field_validator
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from henchmen.config.settings import Settings, require_secure_github_url

__all__ = ["GITHUB_ENDPOINT_FIELDS", "EndpointError", "GitHubEndpoints", "resolve_github_endpoints"]

GITHUB_ENDPOINT_FIELDS: tuple[str, ...] = ("github_api_url", "github_web_url")


class EndpointError(ValueError):
    """A configured endpoint URL is unusable; ``field`` names the setting (the value is never included)."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(f"{field}: {message}")
        self.field = field
        self.reason = message


class GitHubEndpoints(BaseModel):
    """Where the GitHub REST API and web UI live, both without a trailing slash."""

    model_config = ConfigDict(frozen=True)

    api_url: str = Field(..., description="REST API base URL, no trailing slash")
    web_url: str = Field(..., description="Web base URL, no trailing slash")


def _default(field: str) -> str:
    return str(Settings.model_fields[field].default)


class _MaskedEnvironment(PydanticBaseSettingsSource):
    """The environment source without the seeded keys the configuration file overrides."""

    def __init__(self, settings_cls: type[BaseSettings], inner: PydanticBaseSettingsSource, masked: frozenset[str]):
        super().__init__(settings_cls)
        self._inner = inner
        self._masked = masked

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        return {key: value for key, value in self._inner().items() if key.lower() not in self._masked}


def _endpoint_settings_class(masked_fields: frozenset[str]) -> type[BaseSettings]:
    class _GitHubEndpointSettings(BaseSettings):
        model_config = SettingsConfigDict(
            env_prefix="HENCHMEN_",
            env_file_encoding="utf-8",
            case_sensitive=False,
            extra="ignore",
            hide_input_in_errors=True,
        )

        github_api_url: str = Field(default=_default("github_api_url"))
        github_web_url: str = Field(default=_default("github_web_url"))

        @field_validator("github_api_url", "github_web_url", mode="after")
        @classmethod
        def _secure(cls, value: str, info: ValidationInfo) -> str:
            if not value.strip():
                return _default(info.field_name or "")
            return require_secure_github_url(value)

        @classmethod
        def settings_customise_sources(
            cls,
            settings_cls: type[BaseSettings],
            init_settings: PydanticBaseSettingsSource,
            env_settings: PydanticBaseSettingsSource,
            dotenv_settings: PydanticBaseSettingsSource,
            file_secret_settings: PydanticBaseSettingsSource,
        ) -> tuple[PydanticBaseSettingsSource, ...]:
            if not masked_fields:
                return init_settings, env_settings, dotenv_settings
            return init_settings, _MaskedEnvironment(settings_cls, env_settings, masked_fields), dotenv_settings

    return _GitHubEndpointSettings


def resolve_github_endpoints(
    config_file: Path | None, *, seeded_env: Mapping[str, str] | None = None
) -> GitHubEndpoints:
    """The GitHub endpoints the configuration names right now (uncached); :class:`EndpointError` if invalid."""
    file_keys: set[str] = set()
    try:
        if config_file is not None and config_file.is_file():
            file_keys = {str(key).upper() for key in dotenv_values(config_file)}
    except (OSError, UnicodeDecodeError) as exc:
        raise EndpointError(
            GITHUB_ENDPOINT_FIELDS[0], f"could not be read from the configuration file ({type(exc).__name__})"
        ) from None
    seeded = {key.upper() for key in (seeded_env or {})}
    masked = frozenset(field for field in GITHUB_ENDPOINT_FIELDS if f"HENCHMEN_{field.upper()}" in seeded & file_keys)
    settings_cls = _endpoint_settings_class(masked)
    env_file = str(config_file) if config_file is not None else None
    try:
        resolved = settings_cls(_env_file=env_file)
    except (OSError, UnicodeDecodeError) as exc:
        raise EndpointError(
            GITHUB_ENDPOINT_FIELDS[0], f"could not be read from the configuration file ({type(exc).__name__})"
        ) from None
    except ValidationError as exc:
        error = exc.errors(include_url=False, include_input=False)[0]
        field = str(error["loc"][0]) if error["loc"] else GITHUB_ENDPOINT_FIELDS[0]
        message = str(error["msg"]).removeprefix("Value error, ")
        raise EndpointError(field, message) from None
    values = resolved.model_dump()
    return GitHubEndpoints(
        api_url=str(values["github_api_url"]).strip().rstrip("/"),
        web_url=str(values["github_web_url"]).strip().rstrip("/"),
    )
