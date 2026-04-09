"""AWS SNS implementation of MessageBroker."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from henchmen.config.settings import Settings


class SNSMessageBroker:
    """MessageBroker backed by AWS Simple Notification Service."""

    def __init__(self, settings: Settings) -> None:
        import boto3

        self._region = getattr(settings, "aws_region", "us-east-1")
        self._account_id = getattr(settings, "aws_account_id", "")
        self._prefix = getattr(settings, "aws_resource_prefix", "henchmen")
        self._client: Any = boto3.client("sns", region_name=self._region)

    def _topic_arn(self, topic: str) -> str:
        """Build SNS topic ARN from topic name."""
        return f"arn:aws:sns:{self._region}:{self._account_id}:{self._prefix}-{topic}"

    async def publish(
        self,
        topic: str,
        data: bytes,
        ordering_key: str | None = None,
        **attributes: str,
    ) -> str:
        """Publish a message to an SNS topic. Returns message ID."""
        topic_arn = self._topic_arn(topic)
        kwargs: dict[str, Any] = {
            "TopicArn": topic_arn,
            "Message": data.decode("utf-8", errors="replace"),
        }
        if attributes:
            kwargs["MessageAttributes"] = {k: {"DataType": "String", "StringValue": v} for k, v in attributes.items()}
        if ordering_key:
            kwargs["MessageGroupId"] = ordering_key

        response = await asyncio.to_thread(self._client.publish, **kwargs)
        return str(response.get("MessageId", ""))

    async def pull_dlq(
        self,
        subscription_name: str,
        max_messages: int = 10,
    ) -> list[dict[str, Any]]:
        """DLQ pull is not implemented for the AWS SNS broker.

        On AWS, SNS-to-SQS fan-out means the DLQ is a separate SQS queue,
        and pulling from it requires an SQS client rather than the SNS
        client used here.  Add an SQS-backed implementation (or a dedicated
        ``SQSMessageBroker``) when DLQ monitoring is needed on AWS.
        """
        raise NotImplementedError(
            "pull_dlq is not implemented for the AWS SNS broker. "
            "Add an SQS-backed DLQ client to use the DLQ monitor on AWS."
        )
