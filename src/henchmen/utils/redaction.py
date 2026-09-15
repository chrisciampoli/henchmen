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

# HTTP Basic auth, as Jira's ``Authorization: Basic <base64(email:token)>`` header
# would appear in a logged request or an httpx exception's request repr.
# Unlike every other scheme word this module matches, "basic" is also an
# ordinary English word ("a basic misunderstanding"), so the value alone
# being 16+ base64-alphabet characters is not enough -- an all-lowercase
# word like "misunderstanding" satisfies that too. The lookahead requires at
# least one character that plain lowercase prose essentially never has (an
# uppercase letter, a digit, ``+``, ``/`` or ``=``), which real base64 almost
# always does. Only "basic" itself is matched case-insensitively (the scoped
# ``(?i: ...)`` group); the lookahead's ``[A-Z0-9+/=]`` stays case-sensitive
# on purpose, or a global ``(?i)`` would fold it back down to
# ``[A-Za-z0-9+/=]`` and make the lookahead match any lowercase word too.
# Same linearity argument as ``_BEARER_PATTERN``: the lookahead's inner ``*``
# excludes whitespace, so it can never run past the next space/tab, bounding
# every attempt to the single token that follows "basic " -- a run of
# "basic " has nowhere to backtrack into beyond that one token.
_BASIC_AUTH_PATTERN = re.compile(r"\b(?i:(basic))[ \t]+(?=[A-Za-z0-9+/=]*[A-Z0-9+/=])[A-Za-z0-9+/=]{16,}")

# A PEM private-key block (PKCS#1 ``RSA PRIVATE KEY``, PKCS#8 ``PRIVATE KEY``,
# ``ENCRYPTED PRIVATE KEY``, ``EC``/``OPENSSH`` ...) -- the GitHub App key, or
# a manifest-conversion body's ``pem`` field that ended up in an exception or
# a logged response. It spans lines, and in a JSON/dict repr its line breaks
# are the two characters ``\n``, so the body is "anything that does not start
# another ``-----`` run" rather than a base64 alphabet (legacy encrypted keys
# also carry ``Proc-Type:``/``DEK-Info:`` header lines with single hyphens).
#
# Linear by construction: the header label is bounded (``{0,40}``); the body
# is one greedy loop in which every character is matched by exactly one
# branch (a non-hyphen, or a hyphen not followed by four more), so it never
# backtracks; and the footer is *optional*. An unterminated block (an error
# message cut at 200 characters) is therefore redacted from its header up to
# the next ``-----`` or, when there is none, to the end of the text -- which
# can take ordinary text after the key with it (deliberate: fail closed). A
# run of headers with no footer (``"-----BEGIN PRIVATE KEY-----" * N``) ends
# each match at the next ``-----`` instead of rescanning to the end of the
# input from every header.
_PEM_PRIVATE_KEY_PATTERN = re.compile(
    r"-----BEGIN [A-Z ]{0,40}PRIVATE KEY-----(?:[^-]|-(?!----))*(?:-----END [A-Z ]{0,40}PRIVATE KEY-----)?"
)

# A JSON Web Token: three base64url segments separated by dots, the header
# starting ``eyJ`` (base64 of ``{"``). GitHub App JWTs are sent as
# ``Authorization: Bearer <jwt>``, which ``_BEARER_PATTERN`` covers, but a JWT
# can also appear bare (a traceback, a dict repr of request headers). The
# lookbehind (rather than ``\b``) stops a match from starting *inside* a run
# of base64url characters: ``\b`` fires between ``-`` and ``e``, so
# ``"-eyJ" * N`` would restart the unbounded segment scan at every ``eyJ`` in
# one run, which is quadratic. With the lookbehind a match only starts at the
# beginning of a run, and each run is scanned by at most three starts.
_JWT_PATTERN = re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")

# Atlassian (Jira Cloud) API tokens: ``ATATT3x...`` followed by a long
# base64-ish body and an ``=``-separated checksum.
_ATLASSIAN_TOKEN_PATTERN = re.compile(r"(?<![A-Za-z0-9_=-])ATATT[A-Za-z0-9_=-]{20,}")

# Key-name endings that mark a credential in an assignment or a key/value
# pair. Kept in step with ``henchmen.cli.envfile.is_secret_key``, the
# secret-name classifier used for display masking.
_SECRET_KEY_NAME = r"[A-Za-z][A-Za-z0-9_]*(?:_TOKEN|_API_KEY|_SECRET|_PRIVATE_KEY|_PASSWORD)"

# Any environment-variable-style ``SOMETHING_TOKEN=value`` assignment (or
# ``*_API_KEY`` / ``*_SECRET`` / ``*_PRIVATE_KEY`` / ``*_PASSWORD``). This is
# the shape a Docker `-e` command line, an ``os.environ`` dump, or a crash
# traceback prints a secret in -- notably ``HENCHMEN_OPERATIVE_TASK_TOKEN``,
# which is a bare HMAC-SHA256 hex digest with no recognizable prefix pattern
# of its own, unlike the GitHub/Slack/OpenAI tokens above, and the unprefixed
# ``HENCHMEN_GITHUB_WEBHOOK_SECRET``. The key name is kept in the output (case
# preserved); only the value is redacted. ``HENCHMEN_GITHUB_APP_PRIVATE_KEY_PATH``
# (a path, not a secret) does not end in a secret suffix and is left alone.
# This pattern alone does not cover a dict/JSON repr (``'X_TOKEN': 'value'``
# or ``"X_TOKEN": "value"``, no ``=``), which is why
# ``_SECRET_QUOTED_KV_PATTERN`` below exists as a second, independent shape
# for the same key-name idea. ``\b`` cannot fire inside a run of word
# characters, so each run is scanned from at most one start.
_SECRET_ENV_VAR_PATTERN = re.compile(rf"\b({_SECRET_KEY_NAME})=(\S+)", re.IGNORECASE)

# The same key-name idea, but for a quoted key: value pair as it would
# appear in a Python dict repr (single quotes) or JSON (double quotes) --
# e.g. an OperativeConfig/env dict logged via ``%r`` or ``json.dumps``, or a
# GitHub App manifest conversion body's ``"client_secret"`` /
# ``"webhook_secret"``. The quote characters around the key and around the
# value are each captured and reused verbatim (backreferences \1 and \4) so
# mixed single/double-quote style is preserved and the two quote pairs need
# not match each other. Every quantifier here is bounded by a negated
# character class (`[^'"]*`) rather than nested/overlapping ``.*`` groups, so
# matching stays linear in input length like the other patterns in this module.
_SECRET_QUOTED_KV_PATTERN = re.compile(rf"(['\"])({_SECRET_KEY_NAME})\1(\s*:\s*)(['\"])[^'\"]*\4", re.IGNORECASE)

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

# Credentials embedded in a URL's userinfo ("scheme://user:pass@host/...",
# "scheme://token@host/...", "scheme://:pass@host/..."), as a misconfigured
# Jira base URL, an Ollama address, or a bearer-style API URL can carry. The
# scheme is kept (captured and replayed via \1) so the redacted text still
# reads "https://***REDACTED***@host/..." rather than losing the URL
# entirely. The two alternatives cover "user:pass" (or an empty user,
# ":pass") and a single bearer-style token with no colon; both stop at the
# next "@", "/" or whitespace.
#
# ``ssh://`` and ``git+ssh://`` are excluded (the negative lookahead): SSH has
# no password-in-URL mechanism, so its userinfo is always a login identity,
# not a secret -- e.g. ``ssh://git@github.com:org/repo`` must survive
# untouched. A bare email address (``user@mail.com``) never matches either
# alternative, since neither contains a "scheme://" prefix.
#
# The scheme is bounded (`{0,31}`, not `*`) and anchored by a lookbehind
# rather than `\b`: an earlier version used `\b[a-z][a-z0-9+.-]*://`, which is
# quadratic on a long run of scheme-like characters with no "://" anywhere
# (e.g. 40k characters of "a." takes ~4.4s) -- `\b` re-attempts the unbounded
# scheme scan from every word boundary in the run. The lookbehind plus a
# bounded quantifier makes the worst-case work at each position O(32), so the
# whole match stays linear in input length -- this matters because `redact`
# runs on every log record in every service and on CI gate output.
_URL_USERINFO_PATTERN = re.compile(
    r"(?i)(?<![a-z0-9+.-])(?!(?:git\+)?ssh://)([a-z][a-z0-9+.-]{0,31}://)(?:[^\s@/:]*:[^\s@/]*|[^\s@/:]+)@"
)

# The one-time GitHub App manifest ``code`` in the conversion URL
# (``POST /app-manifests/<code>/conversions``, as httpx logs a request line).
# The prefix is kept; the segment stops at the next ``/`` or whitespace. Every
# match starts at the fixed literal and the single negated-class quantifier
# cannot overlap it, so matching is linear.
_MANIFEST_CODE_PATH_PATTERN = re.compile(r"(/app-manifests/)[^/\s]+")

# OAuth-style ``code`` and ``state`` query parameters (the GitHub manifest and
# installation callbacks, as uvicorn's access log records the request line).
# A match can only start at ``?`` or ``&`` and the value class excludes ``&``,
# so each run is scanned once: linear.
_CALLBACK_QUERY_PATTERN = re.compile(r"([?&](?:code|state)=)[^&\s\"']+")

# (pattern, replacement) pairs, applied in order. Every pattern but the bearer
# one replaces the whole match outright; the bearer pattern captures the word
# itself (case preserved, whatever whitespace separated it from the token) so
# a redacted line still reads "Bearer ***REDACTED***" rather than losing the
# scheme entirely. Both `\bbearer\b` and the fixed-length character class that
# follows are anchored, bounded quantifiers -- no nested unbounded groups -- so
# matching stays linear in the input length.
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # First: a PEM block contains spaces and hyphens that would otherwise let a
    # later rule (``*_PRIVATE_KEY=\S+``) redact only its first word.
    (_PEM_PRIVATE_KEY_PATTERN, REDACTED),  # PEM private-key blocks (multi-line, truncated too)
    (_JWT_PATTERN, REDACTED),  # JSON Web Tokens (GitHub App JWTs)
    (_ATLASSIAN_TOKEN_PATTERN, REDACTED),  # Atlassian / Jira Cloud API tokens
    (re.compile(r"ghp_[A-Za-z0-9]{20,}"), REDACTED),  # GitHub personal access tokens
    (re.compile(r"ghs_[A-Za-z0-9]{20,}"), REDACTED),  # GitHub server-to-server tokens
    (re.compile(r"gho_[A-Za-z0-9]{20,}"), REDACTED),  # GitHub OAuth tokens
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), REDACTED),  # GitHub fine-grained PATs
    # Slack's token-rotation refresh/exchange tokens ("xoxe.xoxb-...", "xoxe.xoxp-...")
    # embed a bot/user token after the dot; matched whole, before the bare-token rule
    # below would otherwise redact only the part after "xoxe.", leaving that prefix
    # (harmless on its own, but the point is one clean REDACTED, not a partial one).
    (re.compile(r"xoxe\.xox[bp]-[A-Za-z0-9-]+"), REDACTED),  # Slack token-rotation refresh/exchange tokens
    (re.compile(r"xox[abeprs]-[A-Za-z0-9-]+"), REDACTED),  # Slack bot/user/app/enterprise/rotation tokens
    (re.compile(r"xapp-[A-Za-z0-9-]+"), REDACTED),  # Slack app-level tokens
    (re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"), REDACTED),  # Anthropic API keys
    (re.compile(r"sk-proj-[A-Za-z0-9_-]{20,}"), REDACTED),  # OpenAI project-scoped keys
    (re.compile(r"sk-svcacct-[A-Za-z0-9_-]{20,}"), REDACTED),  # OpenAI service-account keys
    (re.compile(r"sk-admin-[A-Za-z0-9_-]{20,}"), REDACTED),  # OpenAI admin keys
    (re.compile(r"sk-[A-Za-z0-9]{32,}"), REDACTED),  # OpenAI / generic secret keys
    (re.compile(r"AIza[A-Za-z0-9_-]{30,}"), REDACTED),  # Google API keys
    (re.compile(r"x-access-token:[^@\s]+"), REDACTED),  # Authenticated git clone URLs
    (_AWS_ARN_PATTERN, REDACTED),  # AWS ARNs (carries the 12-digit account id)
    (_AWS_ACCOUNT_ID_PATTERN, r"\1" + REDACTED),  # bare AWS account id after "account"
    # Internal push / task / API bearer tokens: case-insensitive scheme name,
    # any run of whitespace, value redacted -- "Bearer"/"bearer"/"BEARER" all
    # match and the captured word is kept in the output.
    (_BEARER_PATTERN, r"\1 " + REDACTED),
    (_BASIC_AUTH_PATTERN, r"\1 " + REDACTED),
    (re.compile(r"(?<=setup_token=)[^&#\s\"']+"), REDACTED),  # Console sign-in token (value only)
    (_MANIFEST_CODE_PATH_PATTERN, r"\1" + REDACTED),  # GitHub App manifest code in a conversion URL
    (_CALLBACK_QUERY_PATTERN, r"\1" + REDACTED),  # ?code= / &state= callback query values
    (_SECRET_ENV_VAR_PATTERN, r"\1=" + REDACTED),  # *_TOKEN=/*_API_KEY=/*_SECRET=... (key name kept)
    (_SECRET_QUOTED_KV_PATTERN, r"\1\2\1\3\4" + REDACTED + r"\4"),  # quoted "*_SECRET": "value" (dict/JSON reprs)
    (_URL_USERINFO_PATTERN, r"\1" + REDACTED + "@"),  # scheme://user:pass@ basic-auth URLs
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
