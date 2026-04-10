"""Tiny greeter CLI used as a feature fixture.

Current behaviour: ``python greeter.py --name Alice`` prints
``Hello, Alice!`` exactly once.

Target behaviour after the agent adds ``--verbose``: when the verbose
flag is set, the greeting is printed **twice** (once per line). The
existing behaviour without ``--verbose`` must remain unchanged.
"""

from __future__ import annotations

import argparse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Greet someone by name.")
    parser.add_argument("--name", required=True, help="Name to greet")
    return parser


def greet(name: str, verbose: bool = False) -> list[str]:
    """Return the greeting lines (one or two depending on ``verbose``)."""
    line = f"Hello, {name}!"
    if verbose:
        return [line, line]
    return [line]


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    for line in greet(args.name):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
