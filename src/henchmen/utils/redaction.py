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

_BEARER_PATTERN = re.compile(r"(?i)\b(bearer)[ \t]+[A-Za-z0-9._~+/=-]{16,}")

# Any environment-variable-style ``SOMETHING_TOKEN=value`` assignment. This is
# the shape a Docker `-e` command line, an ``os.environ`` dump, or a crash
# traceback prints a secret in -- notably ``HENCHMEN_OPERATIVE_TASK_TOKEN``,
# which is a bare HMAC-SHA256 hex digest with no recognizable prefix pattern
# of its own, unlike the GitHub/Slack/OpenAI tokens above. The key name is
# kept in the output (case preserved); only the value is redacted. This
# pattern alone does not cover a dict/JSON repr (``'X_TOKEN': 'value'`` or
# ``"X_TOKEN": "value"``, no ``=``), which is why ``_TOKEN_QUOTED_KV_PATTERN``
# below exists as a second, independent shape for the same key-ending-in-
# ``_TOKEN`` idea.
_TOKEN_ENV_VAR_PATTERN = re.compile(r"\b([A-Za-z][A-Za-z0-9_]*_TOKEN)=(\S+)", re.IGNORECASE)

# The same ``*_TOKEN`` idea, but for a quoted key: value pair as it would
# appear in a Python dict repr (single quotes) or JSON (double quotes) --
# e.g. an OperativeConfig/env dict logged via ``%r`` or ``json.dumps``. The
# quote characters around the key and around the value are each captured and
# reused verbatim (backreferences \1 and \4) so mixed single/double-quote
# style is preserved and the two quote pairs need not match each other. Every
# quantifier here is bounded by a negated character class (`[^'"]*`) rather
# than nested/overlapping ``.*`` groups, so matching stays linear in input
# length like the other patterns in this module.
_TOKEN_QUOTED_KV_PATTERN = re.compile(r"(['\"])([A-Za-z][A-Za-z0-9_]*_TOKEN)\1(\s*:\s*)(['\"])[^'\"]*\4", re.IGNORECASE)

# AWS ARNs embed the 12-digit account id as their 5th colon-separated field
# (e.g. an IAM AccessDenied message: "User: arn:aws:iam::123456789012:user/x
# is not authorized ..."). The whole ARN is replaced, which necessarily takes
# the account id with it; the trailing ``\S+`` is a single bounded quantifier
# (stops at the next whitespace) so this stays linear in input length.
_AWS_ARN_PATTERN = re.compile(r"arn:aws[a-z-]*:[a-z0-9-]*:[a-z0-9-]*:\d{12}:\S+")

# A bare account id outside an ARN, as AWS error messages often print it
# ("... (Account: 123456789012)" / "account 123456789012"). Anchored on the
# word "account" so an ordinary 12-digit number elsewhere in a message is
# left alone; the "account" word itself is kept (captured and replayed via
# \1) so the redacted text still reads "Account: ***REDACTED***".
_AWS_ACCOUNT_ID_PATTERN = re.compile(r"(?i)(\baccount\b\s*:?\s*)\d{12}\b")

# (pattern, replacement) pairs, applied in order. Every pattern but the bearer
# one replaces the whole match outright; the bearer pattern captures the word
# itself (case preserved, whatever whitespace separated it from the token) so
# a redacted line still reads "Bearer ***REDACTED***" rather than losing the
# scheme entirely. Both `\bbearer\b` and the fixed-length character class that
# follows are anchored, bounded quantifiers -- no nested unbounded groups -- so
# matching stays linear in the input length.
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"ghp_[A-Za-z0-9]{20,}"), REDACTED),  # GitHub personal access tokens
    (re.compile(r"ghs_[A-Za-z0-9]{20,}"), REDACTED),  # GitHub server-to-server tokens
    (re.compile(r"gho_[A-Za-z0-9]{20,}"), REDACTED),  # GitHub OAuth tokens
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), REDACTED),  # GitHub fine-grained PATs
    (re.compile(r"xox[baprs]-[A-Za-z0-9-]+"), REDACTED),  # Slack bot/user/app tokens
    (re.compile(r"xapp-[A-Za-z0-9-]+"), REDACTED),  # Slack app-level tokens
    (re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"), REDACTED),  # Anthropic API keys
    (re.compile(r"sk-[A-Za-z0-9]{32,}"), REDACTED),  # OpenAI / generic secret keys
    (re.compile(r"AIza[A-Za-z0-9_-]{30,}"), REDACTED),  # Google API keys
    (re.compile(r"x-access-token:[^@\s]+"), REDACTED),  # Authenticated git clone URLs
    (_AWS_ARN_PATTERN, REDACTED),  # AWS ARNs (carries the 12-digit account id)
    (_AWS_ACCOUNT_ID_PATTERN, r"\1" + REDACTED),  # bare AWS account id after "account"
    # Internal push / task / API bearer tokens: case-insensitive scheme name,
    # any run of whitespace, value redacted -- "Bearer"/"bearer"/"BEARER" all
    # match and the captured word is kept in the output.
    (_BEARER_PATTERN, r"\1 " + REDACTED),
    (re.compile(r"(?<=setup_token=)[^&#\s\"']+"), REDACTED),  # Console sign-in token (value only)
    (_TOKEN_ENV_VAR_PATTERN, r"\1=" + REDACTED),  # *_TOKEN=value env assignments (key name kept)
    (_TOKEN_QUOTED_KV_PATTERN, r"\1\2\1\3\4" + REDACTED + r"\4"),  # quoted "*_TOKEN": "value" (dict/JSON reprs)
)

# Loggers whose formatter reads ``record.args`` itself and so cannot have them
# cleared: uvicorn's AccessFormatter unpacks (client, method, path, version,
# status). Their string arguments are redacted one by one instead.
_ARGS_PRESERVING_LOGGERS = frozenset({"uvicorn.access"})


def redact(text: str) -> str:
    """Return *text* with every known secret pattern replaced."""
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


_default_factory = logging.getLogRecordFactory()


def _redacting_factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
    record = _default_factory(*args, **kwargs)
    if record.name in _ARGS_PRESERVING_LOGGERS and isinstance(record.args, tuple):
        record.msg = redact(str(record.msg))
        record.args = tuple(redact(arg) if isinstance(arg, str) else arg for arg in record.args)
        return record
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
