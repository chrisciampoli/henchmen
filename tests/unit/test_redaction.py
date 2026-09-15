"""Unit tests for the AWS ARN / account-id redaction patterns.

``henchmen.utils.redaction.redact`` also covers GitHub/Slack/Anthropic/
OpenAI/Google tokens and bearer/``*_TOKEN`` shapes; those are exercised
indirectly wherever they are logged (``test_internal_auth.py``,
``test_ci_runner.py``, ...). This file covers only the AWS patterns added
alongside the Bedrock check.
"""

from __future__ import annotations

from henchmen.utils.redaction import REDACTED, redact


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
