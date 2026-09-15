"""Build Settings from a configuration file the way the next run-mode start will.

``henchmen serve`` seeds defaults such as ``HENCHMEN_PROVIDER=local`` into its
own ``os.environ`` when neither the environment nor the dotenv file defines
them. The process environment outranks the dotenv file, so validating a file
the Console just wrote inside that same process would let the seeded value
hide what the file says. :func:`settings_problems` removes the seeded keys from
the environment source and re-applies each seeded default only where the file
leaves that key out -- which is what the restarted process does.

On a desktop install a run mode whose effective Dispatch API token is empty or
the seeded placeholder is a problem too (the task API would refuse every
request), including when a blank environment variable shadows a token the file
sets.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from dotenv import dotenv_values
from pydantic import AliasChoices, ValidationError
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, EnvSettingsSource, PydanticBaseSettingsSource

from henchmen.config.paths import is_desktop_install
from henchmen.config.settings import Settings

#: The only key ``overrides`` may set (see :func:`settings_problems`). Apply uses
#: this to validate a pending Dispatch API token before it is written to the
#: file (ruling P6); nothing else may be overridden this way.
DISPATCH_API_TOKEN_KEY = "HENCHMEN_DISPATCH_API_TOKEN"
_ALLOWED_OVERRIDE_KEYS = frozenset({DISPATCH_API_TOKEN_KEY})


class _MaskedEnvSource(PydanticBaseSettingsSource):
    """The environment source with the seeded keys removed.

    The inner source resolves an aliased field to the *first* alias the
    environment sets, so removing a seeded ``HENCHMEN_GITHUB_TOKEN`` would also
    lose a genuine bare ``GITHUB_TOKEN``; ``fallbacks`` put those other aliases
    back.
    """

    def __init__(
        self,
        settings_cls: type[BaseSettings],
        inner: PydanticBaseSettingsSource,
        masked: frozenset[str],
        fallbacks: Mapping[str, str],
    ) -> None:
        super().__init__(settings_cls)
        self._inner = inner
        self._masked = masked
        self._fallbacks = dict(fallbacks)

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return None, field_name, False

    def _set_current_state(self, state: dict[str, Any]) -> None:
        super()._set_current_state(state)
        self._inner._set_current_state(state)

    def _set_settings_sources_data(self, states: dict[str, dict[str, Any]]) -> None:
        super()._set_settings_sources_data(states)
        self._inner._set_settings_sources_data(states)

    def __call__(self) -> dict[str, Any]:
        data = {key: value for key, value in self._inner().items() if key.lower() not in self._masked}
        data.update(self._fallbacks)
        return data


class _FileLevelSource(PydanticBaseSettingsSource):
    """Fixed values that rank exactly where the dotenv file does: below the environment, above the file."""

    def __init__(self, settings_cls: type[BaseSettings], values: Mapping[str, str]) -> None:
        super().__init__(settings_cls)
        self._values = dict(values)

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        return dict(self._values)


def _field_name(env_key: str) -> str:
    return env_key.upper().removeprefix("HENCHMEN_").lower()


def _alias_choices(field_name: str) -> tuple[str, ...]:
    field = Settings.model_fields.get(field_name)
    alias = field.validation_alias if field is not None else None
    if isinstance(alias, AliasChoices):
        return tuple(choice for choice in alias.choices if isinstance(choice, str))
    return (alias,) if isinstance(alias, str) else ()


def _masked_source_keys(env_keys: frozenset[str]) -> frozenset[str]:
    """The environment-source keys (lowercased) that carry exactly ``env_keys``.

    A field without a validation alias is reported under its field name; an
    aliased field (``AliasChoices("HENCHMEN_GITHUB_TOKEN", "GITHUB_TOKEN")``) is
    reported under the alias the environment actually used. Only the seeded
    alias itself is masked, never the field as a whole (see
    :func:`_other_alias_values`).
    """
    return frozenset(key.lower() if _alias_choices(_field_name(key)) else _field_name(key) for key in env_keys)


def _other_alias_values(env_keys: frozenset[str]) -> dict[str, str]:
    """For each seeded aliased field, the first *other* alias the environment genuinely sets.

    A seeded ``HENCHMEN_GITHUB_TOKEN`` must not hide a real bare ``GITHUB_TOKEN``:
    that value is kept (under its own alias) and the seeded default is not
    re-applied for the field.
    """
    environment = EnvSettingsSource(Settings).env_vars
    found: dict[str, str] = {}
    for key in env_keys:
        for choice in _alias_choices(_field_name(key)):
            if choice.upper() == key.upper():
                continue
            value = environment.get(choice.lower(), environment.get(choice))
            if value is not None:
                found[choice] = value
                break
    return found


def _settings_class(
    masked_env_keys: frozenset[str], file_level: Mapping[str, str], fallbacks: Mapping[str, str]
) -> type[Settings]:
    if not masked_env_keys and not file_level:
        return Settings
    masked = _masked_source_keys(masked_env_keys)

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
                _MaskedEnvSource(settings_cls, env_settings, masked, fallbacks) if masked else env_settings,
                _FileLevelSource(settings_cls, file_level),
                dotenv_settings,
                file_secret_settings,
            )

    return _FileFirstSettings


def dispatch_api_token_problem(settings: Settings) -> str | None:
    """A problem naming the fix when a desktop install would run with no usable Dispatch API token."""
    if not is_desktop_install() or settings.dispatch_api_token.strip():
        return None
    return (
        f"{DISPATCH_API_TOKEN_KEY} is empty or a placeholder, so the task API would refuse every request. "
        f"Remove any blank or placeholder {DISPATCH_API_TOKEN_KEY} (or DISPATCH_API_TOKEN) environment variable "
        "from the Henchmen container, then apply setup again so a token is generated in the configuration file."
    )


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

    ``overrides`` are applied exactly as if they were already written to the
    file -- above the file, below the environment -- used by apply to validate
    a pending Dispatch API token before it is written (ruling P6). A blank
    environment variable therefore still shadows it here, as it would after the
    restart. Only :data:`DISPATCH_API_TOKEN_KEY` may be set this way; any other
    key raises ``ValueError`` rather than silently validating the wrong thing.

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
    fallbacks = _other_alias_values(frozenset(seeded))
    fallback_fields = {
        _field_name(key) for key in seeded if any(choice in fallbacks for choice in _alias_choices(_field_name(key)))
    }
    defaults = {
        _field_name(key): value
        for key, value in seeded.items()
        if key not in file_keys and _field_name(key) not in fallback_fields
    }
    file_level: dict[str, str] = {}
    if overrides:
        unknown = {key.upper() for key in overrides} - _ALLOWED_OVERRIDE_KEYS
        if unknown:
            raise ValueError(
                f"settings_problems overrides may only set {sorted(_ALLOWED_OVERRIDE_KEYS)}; refused {sorted(unknown)}"
            )
        file_level = {key.upper(): value for key, value in overrides.items()}
    settings_cls = _settings_class(frozenset(seeded), file_level, fallbacks)
    try:
        settings = settings_cls(_env_file=tuple(env_files), **defaults)  # type: ignore[call-arg,arg-type]
    except ValidationError as exc:
        return None, [
            f"{'.'.join(str(part) for part in error['loc']) or 'configuration'}: {error['msg']}"
            for error in exc.errors(include_url=False, include_input=False)
        ]
    except ValueError as exc:
        return None, [str(exc)]
    problems = settings.validate_for_runtime()
    token_problem = dispatch_api_token_problem(settings)
    if token_problem is not None:
        problems.append(token_problem)
    return settings, problems
