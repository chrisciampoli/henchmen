"""Unit tests for the interactive prompt primitives used by ``henchmen init``."""

from __future__ import annotations

import io

import pytest

from henchmen.cli.prompts import (
    Choice,
    ConsolePrompter,
    PromptAbortedError,
    ScriptedPrompter,
    mask_secret,
)

# ---------------------------------------------------------------------------
# mask_secret
# ---------------------------------------------------------------------------


class TestMaskSecret:
    def test_empty_is_not_set(self):
        assert mask_secret("") == "(not set)"

    def test_short_values_fully_masked(self):
        assert mask_secret("abc") == "****"
        assert mask_secret("12345678") == "****"

    def test_long_values_keep_last_four(self):
        assert mask_secret("sk-ant-api03-abcdefghijklmnop3f9a") == "****3f9a"

    def test_never_leaks_more_than_four_chars(self):
        value = "xoxb-1234567890-abcdefghij"
        masked = mask_secret(value)
        assert value[:-4] not in masked


# ---------------------------------------------------------------------------
# ScriptedPrompter (the test double used by the wizard tests)
# ---------------------------------------------------------------------------


class TestScriptedPrompter:
    def test_text_returns_scripted_answer(self):
        p = ScriptedPrompter(["hello"])
        assert p.text("Name") == "hello"

    def test_text_empty_answer_uses_default(self):
        p = ScriptedPrompter([""])
        assert p.text("Name", default="dflt") == "dflt"

    def test_text_validator_reprompts_on_error(self):
        p = ScriptedPrompter(["bad", "good"])

        def validator(value: str) -> str | None:
            return None if value == "good" else "must be good"

        assert p.text("Value", validator=validator) == "good"
        assert any("must be good" in line for line in p.output)

    def test_secret_returns_answer_and_records_prompt(self):
        p = ScriptedPrompter(["s3cret"])
        assert p.secret("Token") == "s3cret"
        assert p.transcript == ["Token"]

    def test_secret_empty_keeps_default(self):
        p = ScriptedPrompter([""])
        assert p.secret("Token", default="existing") == "existing"

    def test_choice_accepts_key(self):
        p = ScriptedPrompter(["b"])
        assert p.choice("Pick", [Choice("a", "A"), Choice("b", "B")]) == "b"

    def test_choice_accepts_one_based_index(self):
        p = ScriptedPrompter(["2"])
        assert p.choice("Pick", [Choice("a", "A"), Choice("b", "B")]) == "b"

    def test_choice_empty_uses_default(self):
        p = ScriptedPrompter([""])
        assert p.choice("Pick", [Choice("a", "A"), Choice("b", "B")], default="b") == "b"

    def test_choice_invalid_then_valid(self):
        p = ScriptedPrompter(["zzz", "a"])
        assert p.choice("Pick", [Choice("a", "A"), Choice("b", "B")]) == "a"

    def test_confirm_accepts_bool_and_strings(self):
        p = ScriptedPrompter([True, "n", "yes", ""])
        assert p.confirm("Q1") is True
        assert p.confirm("Q2") is False
        assert p.confirm("Q3") is True
        assert p.confirm("Q4", default=True) is True

    def test_exhausted_answers_raise(self):
        p = ScriptedPrompter([])
        with pytest.raises(AssertionError, match="no scripted answer"):
            p.text("Name")

    def test_abort_sentinel_raises_prompt_aborted(self):
        p = ScriptedPrompter([ScriptedPrompter.ABORT])
        with pytest.raises(PromptAbortedError):
            p.text("Name")

    def test_output_helpers_are_captured(self):
        p = ScriptedPrompter([])
        p.info("plain")
        p.ok("good")
        p.warn("careful")
        p.fail("bad")
        joined = "\n".join(p.output)
        assert "plain" in joined and "good" in joined and "careful" in joined and "bad" in joined


# ---------------------------------------------------------------------------
# ConsolePrompter (real stdin/stdout implementation, driven by fakes)
# ---------------------------------------------------------------------------


def _console(answers: list[str], secrets: list[str] | None = None) -> tuple[ConsolePrompter, io.StringIO]:
    out = io.StringIO()
    answers_iter = iter(answers)
    secrets_iter = iter(secrets or [])

    def fake_input(prompt: str = "") -> str:
        out.write(prompt)
        return next(answers_iter)

    def fake_getpass(prompt: str = "") -> str:
        out.write(prompt)
        return next(secrets_iter)

    return ConsolePrompter(input_fn=fake_input, getpass_fn=fake_getpass, out=out), out


class TestConsolePrompter:
    def test_text_shows_default_and_returns_it_on_empty(self):
        p, out = _console([""])
        assert p.text("Region", default="us-central1") == "us-central1"
        assert "[us-central1]" in out.getvalue()

    def test_text_validator_reprompts(self):
        p, out = _console(["", "ok"])
        result = p.text("Required", validator=lambda v: None if v else "value is required")
        assert result == "ok"
        assert "value is required" in out.getvalue()

    def test_secret_uses_getpass_and_never_echoes(self):
        p, out = _console([], secrets=["xoxb-secret-value"])
        assert p.secret("Bot token") == "xoxb-secret-value"
        assert "xoxb-secret-value" not in out.getvalue()

    def test_secret_empty_keeps_existing_and_shows_mask_only(self):
        p, out = _console([], secrets=[""])
        assert p.secret("Bot token", default="xoxb-1234567890abcd") == "xoxb-1234567890abcd"
        text = out.getvalue()
        assert "****abcd" in text
        assert "xoxb-1234567890abcd" not in text

    def test_choice_prints_numbered_menu_and_accepts_number(self):
        p, out = _console(["2"])
        result = p.choice("Provider", [Choice("a", "Anthropic", "hosted"), Choice("o", "OpenAI")])
        assert result == "o"
        text = out.getvalue()
        assert "1)" in text and "2)" in text and "Anthropic" in text and "hosted" in text

    def test_choice_accepts_key_and_marks_default(self):
        p, out = _console(["a"])
        result = p.choice("Provider", [Choice("a", "Anthropic"), Choice("o", "OpenAI")], default="o")
        assert result == "a"
        assert "[2]" in out.getvalue() or "default" in out.getvalue().lower()

    def test_choice_rejects_invalid_then_accepts(self):
        p, out = _console(["9", "x", "1"])
        result = p.choice("Provider", [Choice("a", "Anthropic"), Choice("o", "OpenAI")])
        assert result == "a"
        assert "1-2" in out.getvalue() or "choose" in out.getvalue().lower()

    def test_confirm_defaults(self):
        p, _ = _console(["", "", "n", "Y"])
        assert p.confirm("Q", default=True) is True
        assert p.confirm("Q", default=False) is False
        assert p.confirm("Q", default=True) is False
        assert p.confirm("Q", default=False) is True

    def test_keyboard_interrupt_becomes_prompt_aborted(self):
        def raising_input(prompt: str = "") -> str:
            raise KeyboardInterrupt

        p = ConsolePrompter(input_fn=raising_input, getpass_fn=raising_input, out=io.StringIO())
        with pytest.raises(PromptAbortedError):
            p.text("Name")
        with pytest.raises(PromptAbortedError):
            p.secret("Token")

    def test_eof_becomes_prompt_aborted(self):
        def eof_input(prompt: str = "") -> str:
            raise EOFError

        p = ConsolePrompter(input_fn=eof_input, getpass_fn=eof_input, out=io.StringIO())
        with pytest.raises(PromptAbortedError):
            p.confirm("Q")
