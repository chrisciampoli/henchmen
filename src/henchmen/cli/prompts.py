"""Prompt primitives for interactive CLI commands (``henchmen init``).

A :class:`Prompter` abstracts stdin/stdout so the setup wizard can be driven
by a scripted list of answers in tests. :class:`ConsolePrompter` is the real
implementation (``input()`` for text, ``getpass`` for secrets, numbered menus
for choices). :class:`ScriptedPrompter` replays canned answers and records
every prompt and output line so tests can assert on them.

No third-party dependency: this keeps the base install lean, which is the
project's stated policy for the CLI.
"""

from __future__ import annotations

import getpass
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol, TextIO

Validator = Callable[[str], str | None]
"""Return an error message for an invalid value, or ``None`` when valid."""


class PromptAbortedError(Exception):
    """Raised when the user cancels a prompt (Ctrl-C or EOF)."""


@dataclass(frozen=True)
class Choice:
    """One selectable option in a :meth:`Prompter.choice` menu."""

    key: str
    label: str
    hint: str = ""


def mask_secret(value: str) -> str:
    """Return a display-safe form of a secret: ``(not set)``, ``****`` or ``****<last 4>``."""
    if not value:
        return "(not set)"
    if len(value) <= 8:
        return "****"
    return "****" + value[-4:]


class Prompter(Protocol):
    """Interface the wizard talks to. Implemented by console and scripted prompters."""

    def text(self, prompt: str, default: str = "", *, validator: Validator | None = None) -> str: ...

    def secret(self, prompt: str, default: str = "") -> str: ...

    def choice(self, prompt: str, choices: Sequence[Choice], default: str | None = None) -> str: ...

    def confirm(self, prompt: str, default: bool = False) -> bool: ...

    def info(self, message: str) -> None: ...

    def ok(self, message: str) -> None: ...

    def warn(self, message: str) -> None: ...

    def fail(self, message: str) -> None: ...


_YES = ("y", "yes", "true", "1")
_NO = ("n", "no", "false", "0")


def _parse_confirm(answer: str, default: bool) -> bool | None:
    """Map a yes/no answer to a bool; ``None`` means unrecognised."""
    normalized = answer.strip().lower()
    if not normalized:
        return default
    if normalized in _YES:
        return True
    if normalized in _NO:
        return False
    return None


def _resolve_choice(answer: str, choices: Sequence[Choice], default: str | None) -> str | None:
    """Map a typed answer (1-based index or key) to a choice key; ``None`` if invalid."""
    normalized = answer.strip()
    if not normalized:
        return default
    if normalized.isdigit():
        index = int(normalized)
        if 1 <= index <= len(choices):
            return choices[index - 1].key
        return None
    for choice in choices:
        if normalized.lower() == choice.key.lower():
            return choice.key
    return None


class ConsolePrompter:
    """Interactive prompter backed by ``input()``/``getpass`` and a text stream."""

    def __init__(
        self,
        *,
        input_fn: Callable[[str], str] = input,
        getpass_fn: Callable[[str], str] = getpass.getpass,
        out: TextIO | None = None,
    ) -> None:
        self._input = input_fn
        self._getpass = getpass_fn
        self._out: TextIO = out if out is not None else sys.stdout

    # -- output ------------------------------------------------------------

    def _write(self, line: str) -> None:
        self._out.write(line + "\n")
        self._out.flush()

    def info(self, message: str) -> None:
        self._write(message)

    def ok(self, message: str) -> None:
        self._write(f"  [OK]   {message}")

    def warn(self, message: str) -> None:
        self._write(f"  [WARN] {message}")

    def fail(self, message: str) -> None:
        self._write(f"  [FAIL] {message}")

    # -- input -------------------------------------------------------------

    def _ask(self, prompt: str, *, hidden: bool = False) -> str:
        try:
            if hidden:
                return self._getpass(prompt)
            return self._input(prompt)
        except (KeyboardInterrupt, EOFError) as exc:
            self._write("")
            raise PromptAbortedError() from exc

    def text(self, prompt: str, default: str = "", *, validator: Validator | None = None) -> str:
        suffix = f" [{default}]" if default else ""
        while True:
            answer = self._ask(f"{prompt}{suffix}: ").strip()
            value = answer or default
            if validator is not None:
                error = validator(value)
                if error:
                    self._write(f"  {error}")
                    continue
            return value

    def secret(self, prompt: str, default: str = "") -> str:
        suffix = f" [Enter keeps {mask_secret(default)}]" if default else ""
        answer = self._ask(f"{prompt}{suffix} (input hidden): ", hidden=True).strip()
        return answer or default

    def choice(self, prompt: str, choices: Sequence[Choice], default: str | None = None) -> str:
        if not choices:
            raise ValueError("choice() requires at least one option")
        self._write(prompt)
        default_index: int | None = None
        for index, option in enumerate(choices, start=1):
            marker = ""
            if default is not None and option.key == default:
                default_index = index
                marker = "  (default)"
            hint = f"  - {option.hint}" if option.hint else ""
            self._write(f"  {index}) {option.label}{hint}{marker}")
        suffix = f" [{default_index}]" if default_index is not None else ""
        while True:
            answer = self._ask(f"Choose 1-{len(choices)}{suffix}: ")
            resolved = _resolve_choice(answer, choices, default)
            if resolved is not None:
                return resolved
            self._write(f"  Please choose a number between 1-{len(choices)} or type an option name.")

    def confirm(self, prompt: str, default: bool = False) -> bool:
        suffix = "[Y/n]" if default else "[y/N]"
        while True:
            answer = self._ask(f"{prompt} {suffix} ")
            parsed = _parse_confirm(answer, default)
            if parsed is not None:
                return parsed
            self._write("  Please answer y or n.")


class ScriptedPrompter:
    """Prompter that replays canned answers. Used to drive the wizard in tests.

    ``answers`` are consumed in order. Pass :attr:`ABORT` as an answer to
    simulate Ctrl-C at that prompt. Every prompt text is appended to
    :attr:`transcript` and every output line to :attr:`output`.
    """

    ABORT = object()

    def __init__(self, answers: Sequence[object]) -> None:
        self._answers = list(answers)
        self.transcript: list[str] = []
        self.output: list[str] = []

    def _next(self, prompt: str) -> object:
        self.transcript.append(prompt)
        if not self._answers:
            raise AssertionError(f"ScriptedPrompter: no scripted answer for prompt {prompt!r}")
        answer = self._answers.pop(0)
        if answer is self.ABORT:
            raise PromptAbortedError()
        return answer

    # -- output ------------------------------------------------------------

    def info(self, message: str) -> None:
        self.output.append(message)

    def ok(self, message: str) -> None:
        self.output.append(f"[OK] {message}")

    def warn(self, message: str) -> None:
        self.output.append(f"[WARN] {message}")

    def fail(self, message: str) -> None:
        self.output.append(f"[FAIL] {message}")

    # -- input -------------------------------------------------------------

    def text(self, prompt: str, default: str = "", *, validator: Validator | None = None) -> str:
        while True:
            answer = str(self._next(prompt)).strip()
            value = answer or default
            if validator is not None:
                error = validator(value)
                if error:
                    self.output.append(error)
                    continue
            return value

    def secret(self, prompt: str, default: str = "") -> str:
        answer = str(self._next(prompt)).strip()
        return answer or default

    def choice(self, prompt: str, choices: Sequence[Choice], default: str | None = None) -> str:
        while True:
            answer = str(self._next(prompt))
            resolved = _resolve_choice(answer, choices, default)
            if resolved is not None:
                return resolved
            self.output.append(f"invalid choice {answer!r}")

    def confirm(self, prompt: str, default: bool = False) -> bool:
        while True:
            answer = self._next(prompt)
            if isinstance(answer, bool):
                return answer
            parsed = _parse_confirm(str(answer), default)
            if parsed is not None:
                return parsed
            self.output.append(f"invalid confirm answer {answer!r}")
