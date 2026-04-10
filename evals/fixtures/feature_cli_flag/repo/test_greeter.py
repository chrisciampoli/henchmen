"""Tests for greeter.py. The --verbose test fails until the flag is added."""

from __future__ import annotations

from greeter import build_parser, greet


def test_default_greet_one_line() -> None:
    assert greet("Alice") == ["Hello, Alice!"]


def test_verbose_greet_two_lines() -> None:
    # greet() already supports `verbose` as a kwarg so this passes today.
    assert greet("Alice", verbose=True) == ["Hello, Alice!", "Hello, Alice!"]


def test_verbose_prints_greeting_twice(capsys) -> None:
    """Running `greeter --name Alice --verbose` should print two lines.

    This test fails today because the CLI parser does not accept
    ``--verbose``. Adding the flag + wiring it into ``main()`` is the
    feature task.
    """
    from greeter import main

    rc = main(["--name", "Alice", "--verbose"])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out.count("Hello, Alice!") == 2


def test_parser_has_verbose_flag() -> None:
    parser = build_parser()
    args = parser.parse_args(["--name", "Bob", "--verbose"])
    assert getattr(args, "verbose", False) is True
