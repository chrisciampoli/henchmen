"""Tests for the henchmen CLI."""

from unittest.mock import patch

from henchmen.cli import main


def test_cli_no_args_exits(capsys):
    with patch("sys.argv", ["henchmen"]):
        try:
            main()
        except SystemExit:
            pass
    captured = capsys.readouterr()
    assert "usage" in captured.out.lower() or "henchmen" in captured.out.lower()


def test_cli_serve_help(capsys):
    with patch("sys.argv", ["henchmen", "serve", "--help"]):
        try:
            main()
        except SystemExit:
            pass
    captured = capsys.readouterr()
    assert "host" in captured.out.lower()
