"""`henchmen console-link` — print a fresh one-time sign-in link for the Console.

The setup token is consumed when it is exchanged for a session and rotated on
every start, so the launcher (or an engineer running
``docker exec henchmen henchmen console-link``) asks for a new link instead of
reusing an old one. Rotating invalidates every link printed earlier.
"""

from __future__ import annotations

import argparse
import sys

from henchmen.config import paths

_DEFAULT_PORT = 8000


def add_console_link_arguments(parser: argparse.ArgumentParser) -> None:
    """Register the ``console-link`` flags."""
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Port the browser reaches the Console on (default: HENCHMEN_LOCAL_SERVE_PORT from the configuration, else 8000)",
    )


def _configured_port() -> int:
    from henchmen.config.settings import Settings

    try:
        return int(Settings(_env_file=paths.env_files()).local_serve_port)  # type: ignore[call-arg]
    except (ValueError, OSError) as exc:  # pydantic ValidationError subclasses ValueError
        print(
            f"WARNING: could not read the configured port ({exc}); falling back to {_DEFAULT_PORT}. "
            "Pass --port to use a different one.",
            file=sys.stderr,
        )
        return _DEFAULT_PORT


def run_console_link_cli(args: argparse.Namespace) -> int:
    """Rotate the setup token and print the sign-in URL. Returns the exit code."""
    from henchmen.cli.serve import console_url
    from henchmen.console.auth import SETUP_TOKEN_FILE_NAME, SetupTokenStore

    secrets_dir = paths.secrets_dir()
    if secrets_dir is None:
        print(
            f"ERROR: henchmen console-link needs {paths.DATA_DIR_ENV}; run it inside the Henchmen container.",
            file=sys.stderr,
        )
        return 2
    port = args.port if args.port else _configured_port()
    if port <= 0:
        print(f"ERROR: --port must be greater than 0, got {port}.", file=sys.stderr)
        return 2
    try:
        token = SetupTokenStore(secrets_dir / SETUP_TOKEN_FILE_NAME).rotate()
    except OSError as exc:
        print(f"ERROR: could not write the sign-in token in {secrets_dir}: {exc}", file=sys.stderr)
        return 2
    print(console_url(port, token))
    return 0
