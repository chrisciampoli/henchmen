"""AWS SNS implementation of MessageBroker with SQS-backed DLQ pull."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from henchmen.config.settings import Settings

logger = logging.getLogger(__name__)


class SNSMessageBroker:
    """MessageBroker backed by AWS Simple Notification Service.

    Publish goes through SNS; ``pull_dlq`` is backed by SQS (the dead-letter
    queue for an SNS subscription is an SQS queue, typically provisioned
    alongside the main subscription). The SQS client is lazily instantiated
    the first time ``pull_dlq`` is called so the common publish-only code
    path doesn't pay for the extra client.
    """

    def __init__(self, settings: Settings) -> None:
        import boto3

        self._region = settings.aws_region
        self._account_id = settings.aws_account_id
        self._prefix = settings.aws_resource_prefix
        self._client: Any = boto3.client("sns", region_name=self._region)
        self._sqs_client: Any | None = None

    def _get_sqs_client(self) -> Any:
        """Lazy-init the SQS client for DLQ pulls."""
        if self._sqs_client is None:
            import boto3

            self._sqs_client = boto3.client("sqs", region_name=self._region)
        return self._sqs_client

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
        """Publish a message to an SNS topic. Returns message ID.

        ``ordering_key`` maps to a FIFO message group. Standard (non-FIFO)
        topics reject ``MessageGroupId`` with InvalidParameter, so it is only
        sent for a ``.fifo`` topic; on a standard topic the key is ignored
        (SNS has no ordering there to request).
        """
        topic_arn = self._topic_arn(topic)
        kwargs: dict[str, Any] = {
            "TopicArn": topic_arn,
            "Message": data.decode("utf-8", errors="replace"),
        }
        if attributes:
            kwargs["MessageAttributes"] = {k: {"DataType": "String", "StringValue": v} for k, v in attributes.items()}
        if ordering_key:
            if topic_arn.endswith(".fifo"):
                kwargs["MessageGroupId"] = ordering_key
            else:
                logger.debug("Ignoring ordering_key on standard SNS topic %s", topic_arn)

        response = await asyncio.to_thread(self._client.publish, **kwargs)
        return str(response.get("MessageId", ""))

    async def pull_dlq(
        self,
        subscription_name: str,
        max_messages: int = 10,
    ) -> list[dict[str, Any]]:
        """Pull and acknowledge dead-lettered messages from an SQS queue.

        ``subscription_name`` is resolved with ``sqs.get_queue_url`` so
        callers don't need the account ID or full ARN — it must therefore be
        the *SQS queue name* on AWS, not the GCP-style subscription
        short-name (``...-dead-letter-sub``) that Mastermind currently
        derives; a name with no matching queue raises ``RuntimeError``.

        Returned messages match the shape of the GCP Pub/Sub
        implementation so downstream consumers can treat both providers
        uniformly: ``{"data": <utf-8 decoded body>, "message_id":
        <MessageId>, "attributes": <flat dict of attribute StringValue>}``.

        Raises ``RuntimeError`` with a clear message when the queue does
        not exist — callers can distinguish "no queue configured" from
        "queue is empty" (the latter returns ``[]``).
        """
        from botocore.exceptions import ClientError

        sqs = self._get_sqs_client()

        try:
            queue_url_response = await asyncio.to_thread(sqs.get_queue_url, QueueName=subscription_name)
        except ClientError as exc:
            code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
            if code in (
                "AWS.SimpleQueueService.NonExistentQueue",
                "QueueDoesNotExist",
            ):
                raise RuntimeError(
                    f"SQS DLQ queue {subscription_name!r} does not exist in region {self._region!r}. "
                    "Ensure the dead-letter queue is provisioned before invoking pull_dlq."
                ) from exc
            raise

        queue_url = queue_url_response["QueueUrl"]

        receive_response = await asyncio.to_thread(
            sqs.receive_message,
            QueueUrl=queue_url,
            MaxNumberOfMessages=max_messages,
            WaitTimeSeconds=0,
            MessageAttributeNames=["All"],
        )

        raw_messages = receive_response.get("Messages", []) or []
        if not raw_messages:
            return []

        messages: list[dict[str, Any]] = []
        delete_entries: list[dict[str, str]] = []
        for idx, msg in enumerate(raw_messages):
            body = msg.get("Body", "")
            raw_attrs = msg.get("MessageAttributes") or {}
            attributes = {key: attr.get("StringValue", "") for key, attr in raw_attrs.items() if isinstance(attr, dict)}
            messages.append(
                {
                    "data": body,
                    "message_id": msg.get("MessageId", ""),
                    "attributes": attributes,
                }
            )
            receipt = msg.get("ReceiptHandle")
            if receipt:
                delete_entries.append({"Id": str(idx), "ReceiptHandle": receipt})

        if delete_entries:
            try:
                await asyncio.to_thread(
                    sqs.delete_message_batch,
                    QueueUrl=queue_url,
                    Entries=delete_entries,
                )
            except ClientError as exc:
                logger.warning(
                    "pull_dlq: delete_message_batch failed for queue %s: %s",
                    subscription_name,
                    exc,
                )

        return messages
