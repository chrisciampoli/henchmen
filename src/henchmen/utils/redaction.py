"""Process-wide secret redaction for log records.

Henchmen passes GitHub, Slack and LLM tokens through git URLs, subprocess
output and exception text, so any of them can end up in a log line. This
module installs the redaction at the *record factory* level rather than as a
``logging.Filter`` on the root logger: a filter attached to a logger only runs
for records emitted through that exact logger, so ``logging.getLogger(
"henchmen.mastermind.agent")`` records never reach a root filter, and
``logger.info("token %s", token)`` leaves the secret in ``record.args`` where
a filter that only rewrites ``record.msg`` cannot see it.

Call :func:`install_secret_redaction` once per process, as early as possible.
"""

from __future__ import annotations

import logging
import re
from typing import Any

REDACTED = "***REDACTED***"

_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),  # GitHub personal access tokens
    re.compile(r"ghs_[A-Za-z0-9]{20,}"),  # GitHub server-to-server tokens
    re.compile(r"gho_[A-Za-z0-9]{20,}"),  # GitHub OAuth tokens
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),  # GitHub fine-grained PATs
    re.compile(r"xox[baprs]-[A-Za-z0-9-]+"),  # Slack bot/user/app tokens
    re.compile(r"xapp-[A-Za-z0-9-]+"),  # Slack app-level tokens
    re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),  # Anthropic API keys
    re.compile(r"sk-[A-Za-z0-9]{32,}"),  # OpenAI / generic secret keys
    re.compile(r"AIza[A-Za-z0-9_-]{30,}"),  # Google API keys
    re.compile(r"x-access-token:[^@\s]+"),  # Authenticated git clone URLs
)


def redact(text: str) -> str:
    """Return *text* with every known secret pattern replaced."""
    for pattern in _PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


_default_factory = logging.getLogRecordFactory()


def _redacting_factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
    record = _default_factory(*args, **kwargs)
    try:
        message = record.getMessage()
    except Exception:  # pragma: no cover - malformed %-format args
        message = str(record.msg)
    record.msg = redact(message)
    # The arguments are already interpolated into ``msg`` above; clearing them
    # prevents a second (unredacted) interpolation inside Formatter.format.
    record.args = ()
    return record


def install_secret_redaction() -> None:
    """Install the redacting log-record factory (idempotent)."""
    if logging.getLogRecordFactory() is not _redacting_factory:
        logging.setLogRecordFactory(_redacting_factory)


__all__ = ["REDACTED", "install_secret_redaction", "redact"]
