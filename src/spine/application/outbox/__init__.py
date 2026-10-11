"""Framework-independent outbox dispatch contracts and orchestration."""

from spine.application.outbox.dispatch import (
    DispatchOutcomeCode,
    DispatchReport,
    DuplicateOutboxConsumerError,
    OutboxConsumer,
    OutboxConsumerRegistry,
    OutboxConsumerSuccess,
    OutboxDeliveryFinalization,
    OutboxDispatchRepository,
    OutboxDispatcher,
    RetryableOutboxConsumerFailure,
    TerminalOutboxConsumerFailure,
)

__all__ = [
    "DispatchOutcomeCode",
    "DispatchReport",
    "DuplicateOutboxConsumerError",
    "OutboxConsumer",
    "OutboxConsumerRegistry",
    "OutboxConsumerSuccess",
    "OutboxDeliveryFinalization",
    "OutboxDispatchRepository",
    "OutboxDispatcher",
    "RetryableOutboxConsumerFailure",
    "TerminalOutboxConsumerFailure",
]
