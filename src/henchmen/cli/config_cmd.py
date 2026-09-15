"""`henchmen config` — print the effective Settings with secrets masked.

Settings merge ``os.environ``, ``.env.local`` and ``.env`` (in that order of
precedence) over the field defaults, which makes "what value is actually in
effect?" hard to answer by reading files. This command builds ``Settings``
exactly as the services do and prints every field as the ``HENCHMEN_`` variable
that sets it, masking credentials so the output is safe to paste into an issue.
"""

from __future__ import annotations

import argparse
import json
from typing import TYPE_CHECKING

from henchmen.cli.envfile import is_secret_key
from henchmen.cli.prompts import mask_secret

if TYPE_CHECKING:
    from henchmen.config.settings import Settings

__all__ = ["add_config_arguments", "is_secret_field", "render_settings", "run_config_cli"]


def is_secret_field(name: str) -> bool:
    """True when the Settings field ``name`` holds a credential that must never be printed.

    Delegates to :func:`henchmen.cli.envfile.is_secret_key`, the single
    secret-name classifier, so ``henchmen config``'s masking agrees with the
    Console's ``ConfigStore.masked()`` on every field -- suffix-only match on
    ``_TOKEN``/``_API_KEY``/``_PRIVATE_KEY``/``_SECRET``/``_PASSWORD``, case
    insensitive, prefix-independent (so a bare field name like ``github_token``
    and its dotenv form ``HENCHMEN_GITHUB_TOKEN`` are both recognised).
    """
    return is_secret_key(name)


def _render_value(value: object) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True)


def render_settings(settings: Settings, *, only_set: bool = False) -> list[str]:
    """Return ``HENCHMEN_<FIELD>=<value>`` lines, sorted by name, credentials masked.

    With ``only_set`` the fields still at their declared default are omitted.
    """
    dumped = settings.model_dump(mode="json")
    fields = type(settings).model_fields
    lines: list[str] = []
    for name in sorted(dumped):
        value = dumped[name]
        if only_set and name in fields and fields[name].default == getattr(settings, name):
            continue
        shown = mask_secret(str(value or "")) if is_secret_field(name) else _render_value(value)
        lines.append(f"HENCHMEN_{name.upper()}={shown}")
    return lines


def add_config_arguments(parser: argparse.ArgumentParser) -> None:
    """Register ``henchmen config`` flags."""
    parser.add_argument(
        "--only-set",
        action="store_true",
        help="Only show values that differ from the built-in defaults",
    )


def run_config_cli(args: argparse.Namespace | None = None) -> int:
    """Print the effective configuration. Returns 0, or 2 when Settings fail to validate."""
    import sys

    from henchmen.cli.doctor import load_settings

    settings, result = load_settings()
    if settings is None:
        print(f"ERROR: {result.message}", file=sys.stderr)
        if result.hint:
            print(f"Hint: {result.hint}", file=sys.stderr)
        return 2
    for line in render_settings(settings, only_set=bool(getattr(args, "only_set", False))):
        print(line)
    return 0
