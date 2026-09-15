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
