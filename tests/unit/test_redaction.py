"""Unit tests for ``henchmen.utils.redaction``.

Covers the AWS ARN / account-id patterns, OpenAI key shapes, URL userinfo,
PEM private-key blocks, JWTs, GitHub installation tokens, Atlassian API tokens,
the ``*_TOKEN``/``*_API_KEY``/``*_SECRET``/``*_PRIVATE_KEY`` key-name rules,
and a timing guard for every pattern. Other token shapes are also exercised
wherever they are logged (``test_internal_auth.py``, ``test_ci_runner.py``, ...).
"""

from __future__ import annotations

import json
import time
from functools import lru_cache

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from henchmen.utils.redaction import _PATTERNS, REDACTED, redact

# Generous bound (not a tight benchmark) so this stays non-flaky on a loaded
# CI runner while still catching a genuinely quadratic pattern -- every case
# below runs in single-digit milliseconds on a linear implementation.
_TIMING_BOUND_SECONDS = 0.5

_SIGNING_SECRET = "test-signing-secret-at-least-32-bytes!"


@lru_cache(maxsize=1)
def _pem() -> str:
    """A throwaway EC private key generated at test time (never a committed key)."""
    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()
    ).decode()


def _pem_body_line() -> str:
    return _pem().splitlines()[1]


class TestAwsRedaction:
    def test_iam_arn_in_access_denied_message_hides_the_account_id(self):
        message = (
            "AccessDeniedException: User: arn:aws:iam::123456789012:user/henchmen "
            "is not authorized to perform: bedrock:ListFoundationModels"
        )
        result = redact(message)
        assert "123456789012" not in result
        assert REDACTED in result
        # The scheme/service prefix and the trailing context are still useful.
        assert "AccessDeniedException" in result
        assert "bedrock:ListFoundationModels" in result

    def test_bedrock_arn_is_redacted(self):
        result = redact("arn:aws:bedrock:us-east-1:123456789012:inference-profile/us.anthropic.claude")
        assert "123456789012" not in result
        assert REDACTED in result

    def test_govcloud_and_china_partitions_are_covered(self):
        assert "123456789012" not in redact("arn:aws-us-gov:iam::123456789012:role/x")
        assert "123456789012" not in redact("arn:aws-cn:iam::123456789012:role/x")

    def test_bare_account_id_after_the_word_account_is_redacted(self):
        assert "123456789012" not in redact("Request originated from account 123456789012")
        assert "123456789012" not in redact("(Account: 123456789012)")

    def test_ordinary_twelve_digit_numbers_are_left_alone(self):
        """Only a 12-digit run immediately after the word "account" is an AWS account id."""
        text = "Order number 123456789012 was placed on 2026-09-15"
        assert redact(text) == text

    def test_arn_pattern_does_not_match_a_plain_url(self):
        text = "See https://console.aws.amazon.com/bedrock/home?region=us-east-1"
        assert redact(text) == text


class TestOpenAiKeyRedaction:
    """OpenAI's newer project/service-account/admin key shapes embed a hyphen right after the
    ``sk-`` prefix, which the generic ``sk-[A-Za-z0-9]{32,}`` pattern cannot match."""

    def test_project_scoped_key_is_redacted(self):
        result = redact("Authorization: Bearer sk-proj-abcDEF0123456789abcDEF0123456789")
        assert "sk-proj-" not in result
        assert REDACTED in result

    def test_service_account_key_is_redacted(self):
        assert "sk-svcacct-" not in redact("key=sk-svcacct-abcDEF0123456789abcDEF0123456789")

    def test_admin_key_is_redacted(self):
        assert "sk-admin-" not in redact("key=sk-admin-abcDEF0123456789abcDEF0123456789")


class TestUrlUserinfoRedaction:
    def test_basic_auth_credentials_are_stripped_from_a_url(self):
        result = redact("cannot reach http://admin:hunter2@localhost:11434 (connection refused)")
        assert "admin:hunter2" not in result
        assert REDACTED in result
        # The scheme and host survive so the message is still useful.
        assert "http://" in result
        assert "localhost:11434" in result

    def test_url_without_credentials_is_left_alone(self):
        text = "reachable at http://localhost:11434 (3 models pulled)"
        assert redact(text) == text

    def test_bearer_style_token_with_no_password_is_stripped(self):
        result = redact("fetch https://token@host/api failed")
        assert "token@" not in result
        assert REDACTED in result
        assert "https://" in result
        assert "host/api" in result

    def test_empty_username_with_a_password_is_stripped(self):
        result = redact("connect to redis://:secret@host:6379 timed out")
        assert "secret" not in result
        assert REDACTED in result
        assert "redis://" in result
        assert "host:6379" in result

    def test_ssh_git_remote_identity_is_left_alone(self):
        """SSH has no password-in-URL mechanism; its userinfo is a login identity, not a secret."""
        text = "clone failed: ssh://git@github.com:org/r"
        assert redact(text) == text

    def test_plain_email_address_is_left_alone(self):
        text = "notify user@mail.com when the run finishes"
        assert redact(text) == text

    def test_git_plus_ssh_remote_identity_is_left_alone(self):
        """``git+ssh://`` (pip's VCS URL form) has the same no-password-in-URL property as ssh://."""
        text = "resolved from git+ssh://git@github.com/org/r"
        assert redact(text) == text


class TestPemPrivateKeyRedaction:
    def test_multi_line_pem_block_is_redacted_and_the_surroundings_kept(self):
        result = redact(f"conversion failed: {_pem()} (retry)")
        assert "PRIVATE KEY" not in result
        assert _pem_body_line() not in result
        assert result.startswith("conversion failed: " + REDACTED)
        assert result.endswith("(retry)")

    def test_pem_inside_a_json_body_is_redacted(self):
        result = redact(json.dumps({"id": 1, "pem": _pem(), "slug": "henchmen"}))
        assert "PRIVATE KEY" not in result
        assert _pem_body_line() not in result
        assert '"slug": "henchmen"' in result

    def test_truncated_pem_without_a_footer_is_redacted(self):
        truncated = f"GitHub said: {_pem()}"[:120]
        result = redact(truncated)
        assert "PRIVATE KEY" not in result
        assert _pem_body_line()[:20] not in result

    def test_pkcs8_encrypted_rsa_and_openssh_labels_are_covered(self):
        body = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7"
        for label in ("PRIVATE KEY", "ENCRYPTED PRIVATE KEY", "RSA PRIVATE KEY", "OPENSSH PRIVATE KEY"):
            block = f"-----BEGIN {label}-----\n{body}\n-----END {label}-----"
            assert redact(block) == REDACTED

    def test_legacy_encrypted_pem_headers_are_inside_the_redaction(self):
        block = (
            "-----BEGIN RSA PRIVATE KEY-----\nProc-Type: 4,ENCRYPTED\nDEK-Info: AES-128-CBC,0011AABB\n\n"
            "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcw\n-----END RSA PRIVATE KEY-----"
        )
        assert redact(f"x {block} y") == f"x {REDACTED} y"

    def test_public_key_blocks_are_left_alone(self):
        text = "-----BEGIN PUBLIC KEY-----\nMFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE\n-----END PUBLIC KEY-----"
        assert redact(text) == text


class TestJwtRedaction:
    def test_bare_jwt_is_redacted(self):
        token = jwt.encode({"iss": "4242", "iat": 1, "exp": 2}, _SIGNING_SECRET, algorithm="HS256")
        result = redact(f"headers={{'X-Assertion': '{token}'}} rejected")
        assert token not in result
        assert "eyJ" not in result
        assert "rejected" in result

    def test_jwt_after_bearer_is_redacted(self):
        token = jwt.encode({"iss": "4242"}, _SIGNING_SECRET, algorithm="HS256")
        assert "eyJ" not in redact(f"Authorization: Bearer {token}")

    def test_short_dotted_eyj_text_is_left_alone(self):
        text = "eyJ.a.b is not a token"
        assert redact(text) == text


class TestGitHubInstallationTokenRedaction:
    def test_ghs_token_is_redacted(self):
        result = redact("push to https://github.com/a/b with ghs_AbCdEf0123456789AbCdEf0123456789AbCd failed")
        assert "ghs_AbCdEf" not in result
        assert REDACTED in result


class TestAtlassianTokenRedaction:
    def test_atatt_token_is_redacted(self):
        token = "ATATT3xFfGF0T4kE_n-0123456789abcdefABCDEF0123456789=A1B2C3D4"
        result = redact(f"Jira rejected {token} for chris@example.com")
        assert token not in result
        assert "ATATT" not in result
        assert "chris@example.com" in result

    def test_short_atatt_word_is_left_alone(self):
        assert redact("ATATT3x") == "ATATT3x"


class TestSecretKeyNameRedaction:
    def test_api_key_secret_private_key_and_password_assignments_are_redacted(self):
        for name in (
            "HENCHMEN_ANTHROPIC_API_KEY",
            "HENCHMEN_GITHUB_WEBHOOK_SECRET",
            "HENCHMEN_GITHUB_APP_PRIVATE_KEY",
            "HENCHMEN_JIRA_PASSWORD",
            "HENCHMEN_OPERATIVE_TASK_TOKEN",
        ):
            result = redact(f"docker run -e {name}=0f1e2d3c4b5a69788796a5b4c3d2e1f0 image")
            assert result == f"docker run -e {name}={REDACTED} image"

    def test_quoted_secret_keys_are_redacted(self):
        body = json.dumps({"client_secret": "cs-123", "webhook_secret": "0f1e2d3c", "ANTHROPIC_API_KEY": "k-1"})
        result = redact(body)
        for value in ("cs-123", "0f1e2d3c", "k-1"):
            assert value not in result
        assert '"client_secret": "' + REDACTED + '"' in result

    def test_non_secret_names_are_left_alone(self):
        text = (
            "HENCHMEN_GITHUB_APP_PRIVATE_KEY_PATH=/data/secrets/github-app.pem "
            "HENCHMEN_OPERATIVE_MAX_OUTPUT_TOKENS=4000 HENCHMEN_JIRA_PROJECT_KEY=ENG"
        )
        assert redact(text) == text


# Worst cases for the patterns added with the GitHub App provider: a header
# with no footer repeated (each match must stop at the next "-----"), "eyJ"
# inside one long base64url run (a start must not rescan the run), and long
# runs of key-name characters with no "=".
_NEW_PATTERN_PATHOLOGICAL_INPUTS = (
    "-----BEGIN " * 20000,
    "-----BEGIN PRIVATE KEY-----" * 20000,
    "-----BEGIN PRIVATE KEY-----" + "A" * 100_000,
    "-----BEGIN " + "A " * 50000,
    "eyJ." * 20000,
    "-eyJ" * 20000,
    "eyJaaaaaaaa." * 20000,
    "-ATATT" * 20000,
    "A_SECRET" * 20000,
    "'x_api_key':" * 20000,
)


class TestRedactionPerformance:
    """Regression coverage for a quadratic ``_URL_USERINFO_PATTERN``.

    An earlier version anchored the scheme with ``\\b[a-z][a-z0-9+.-]*://``:
    ``\\b`` re-attempts the unbounded scheme scan from every word boundary in
    a long run of scheme-like characters that never reaches "://", making a
    40k-character run of ``"a."`` take about 4.4s and 100k take about 28s.
    ``redact`` runs on every log record in every service and on CI gate
    output, so a pathological line (or a long, harmless one) must never make
    it slow.
    """

    def _assert_fast(self, text: str) -> None:
        start = time.perf_counter()
        redact(text)
        elapsed = time.perf_counter() - start
        assert elapsed < _TIMING_BOUND_SECONDS, f"redact() took {elapsed:.3f}s on {len(text)} chars"

    def test_a_dot_run_is_fast(self):
        self._assert_fast("a." * 20000)

    def test_a_plus_run_is_fast(self):
        self._assert_fast("a+" * 20000)

    def test_a_dash_run_is_fast(self):
        self._assert_fast("a-" * 20000)

    def test_repeated_bearer_word_is_fast(self):
        self._assert_fast("bearer " * 20000)

    def test_a_long_no_match_string_is_fast(self):
        self._assert_fast("x" * 100_000)

    def test_repeated_pem_begin_is_fast(self):
        self._assert_fast("-----BEGIN " * 20000)

    def test_repeated_pem_headers_without_a_footer_are_fast(self):
        self._assert_fast("-----BEGIN PRIVATE KEY-----" * 20000)

    def test_repeated_eyj_dot_is_fast(self):
        self._assert_fast("eyJ." * 20000)

    def test_eyj_inside_one_long_base64url_run_is_fast(self):
        self._assert_fast("-eyJ" * 20000)

    def test_repeated_atatt_is_fast(self):
        self._assert_fast("-ATATT" * 20000)

    def test_long_secret_like_key_names_are_fast(self):
        self._assert_fast("A_SECRET" * 20000)
        self._assert_fast("'x_api_key':" * 20000)

    def test_every_pattern_individually_is_fast_on_every_pathological_input(self):
        """Guards every entry in _PATTERNS, not just the URL-userinfo one that regressed."""
        pathological_inputs = (
            "a." * 20000,
            "a+" * 20000,
            "a-" * 20000,
            "bearer " * 20000,
            "x" * 100_000,
            *_NEW_PATTERN_PATHOLOGICAL_INPUTS,
        )
        for text in pathological_inputs:
            for pattern, replacement in _PATTERNS:
                start = time.perf_counter()
                pattern.sub(replacement, text)
                elapsed = time.perf_counter() - start
                assert elapsed < _TIMING_BOUND_SECONDS, (
                    f"{pattern.pattern!r} took {elapsed:.3f}s on a {len(text)}-char pathological input"
                )
