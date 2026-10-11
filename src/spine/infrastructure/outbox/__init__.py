"""Transactional outbox publishing and delivery persistence."""

from spine.infrastructure.outbox.delivery import (
    PostgreSQLOutboxDeliveryRepository,
)

__all__ = ["PostgreSQLOutboxDeliveryRepository"]
