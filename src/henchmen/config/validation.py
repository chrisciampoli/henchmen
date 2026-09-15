"""Build Settings from a configuration file the way the next run-mode start will.

``henchmen serve`` seeds defaults such as ``HENCHMEN_PROVIDER=local`` into its
own ``os.environ`` when neither the environment nor the dotenv file defines
them. The process environment outranks the dotenv file, so validating a file
the Console just wrote inside that same process would let the seeded value
hide what the file says. :func:`settings_problems` removes the seeded keys from
the environment source and re-applies each seeded default only where the file
leaves that key out -- which is what the restarted process does.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from dotenv import dotenv_values
from pydantic import ValidationError
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

from henchmen.config.settings import Settings

#: The only key ``overrides`` may set (see :func:`settings_problems`). Apply uses
#: this to validate a pending Dispatch API token before it is written to the
#: file (ruling P6); nothing else may be overridden this way.
DISPATCH_API_TOKEN_KEY = "HENCHMEN_DISPATCH_API_TOKEN"
_ALLOWED_OVERRIDE_KEYS = frozenset({DISPATCH_API_TOKEN_KEY})


class _MaskedEnvSource(PydanticBaseSettingsSource):
    """The environment source with some keys removed."""

    def __init__(
        self, settings_cls: type[BaseSettings], inner: PydanticBaseSettingsSource, masked: frozenset[str]
    ) -> None:
        super().__init__(settings_cls)
        self._inner = inner
        self._masked = masked

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return None, field_name, False

    def _set_current_state(self, state: dict[str, Any]) -> None:
        super()._set_current_state(state)
        self._inner._set_current_state(state)

    def _set_settings_sources_data(self, states: dict[str, dict[str, Any]]) -> None:
        super()._set_settings_sources_data(states)
        self._inner._set_settings_sources_data(states)

    def __call__(self) -> dict[str, Any]:
        return {key: value for key, value in self._inner().items() if key.lower() not in self._masked}


def _field_name(env_key: str) -> str:
    return env_key.upper().removeprefix("HENCHMEN_").lower()


def _settings_class(masked_env_keys: frozenset[str]) -> type[Settings]:
    if not masked_env_keys:
        return Settings
    masked = frozenset({_field_name(key) for key in masked_env_keys} | {key.lower() for key in masked_env_keys})

    class _FileFirstSettings(Settings):
        @classmethod
        def settings_customise_sources(
            cls,
            settings_cls: type[BaseSettings],
            init_settings: PydanticBaseSettingsSource,
            env_settings: PydanticBaseSettingsSource,
            dotenv_settings: PydanticBaseSettingsSource,
            file_secret_settings: PydanticBaseSettingsSource,
        ) -> tuple[PydanticBaseSettingsSource, ...]:
            return (
                init_settings,
                _MaskedEnvSource(settings_cls, env_settings, masked),
                dotenv_settings,
                file_secret_settings,
            )

    return _FileFirstSettings


def settings_problems(
    env_files: Sequence[str],
    *,
    seeded_env: Mapping[str, str] | None = None,
    overrides: Mapping[str, str] | None = None,
) -> tuple[Settings | None, list[str]]:
    """Return ``(settings, runtime problems)``, or ``(None, build problems)`` when Settings cannot be built.

    ``seeded_env`` are the defaults a running process (``henchmen serve``) wrote
    into its own ``os.environ``; they are masked out of the environment source
    here and re-applied only where the file leaves the key out, so the file
    decides exactly as it will on the next real start (D-P8, amendment A5).

    ``overrides`` are applied unconditionally, regardless of what the file or
    environment say -- used by apply to validate a pending Dispatch API token
    before it is written, "as if" it were already in the file (ruling P6). Only
    :data:`DISPATCH_API_TOKEN_KEY` may be set this way; any other key raises
    ``ValueError`` rather than silently validating the wrong thing.

    Messages carry only the field and the reason, never the rejected value,
    which may be a credential.
    """
    seeded = {key.upper(): value for key, value in (seeded_env or {}).items()}
    file_keys: set[str] = set()
    for env_file in env_files:
        try:
            file_keys.update(str(key).upper() for key in dotenv_values(env_file))
        except OSError:
            continue
    defaults = {_field_name(key): value for key, value in seeded.items() if key not in file_keys}
    if overrides:
        unknown = {key.upper() for key in overrides} - _ALLOWED_OVERRIDE_KEYS
        if unknown:
            raise ValueError(
                f"settings_problems overrides may only set {sorted(_ALLOWED_OVERRIDE_KEYS)}; refused {sorted(unknown)}"
            )
        defaults.update({_field_name(key): value for key, value in overrides.items()})
    settings_cls = _settings_class(frozenset(seeded))
    try:
        settings = settings_cls(_env_file=tuple(env_files), **defaults)  # type: ignore[call-arg,arg-type]
    except ValidationError as exc:
        return None, [
            f"{'.'.join(str(part) for part in error['loc']) or 'configuration'}: {error['msg']}"
            for error in exc.errors(include_url=False, include_input=False)
        ]
    except ValueError as exc:
        return None, [str(exc)]
    return settings, settings.validate_for_runtime()
