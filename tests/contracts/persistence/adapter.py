from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import AsyncContextManager, Awaitable
from uuid import UUID

from spine.application.diagnostics.audit import AuditEvent, AuditEventRegistry
from spine.application.persistence.context import PersistenceOperation, TrustedPersistenceContext
from spine.application.persistence.outbox import OutboxEventRegistry
from spine.application.persistence.delivery import (
    OutboxDeliveryRepository,
    OutboxDeliveryRouteRegistry,
)
from spine.application.persistence.unit_of_work import UnitOfWorkFactory


@dataclass(frozen=True)
class PersistenceAdapter:
    """Adapter-neutral inputs consumed by the shared persistence contract suite."""

    uow_factory: UnitOfWorkFactory
    workspace_context: Callable[[UUID], TrustedPersistenceContext]
    worker_workspace_context: Callable[[UUID], TrustedPersistenceContext]
    environment_context: Callable[[UUID, UUID], TrustedPersistenceContext]
    hold_transactions: Callable[[], AsyncContextManager[None]]
    outbox_events: OutboxEventRegistry
    source_context: Callable[[UUID, UUID], TrustedPersistenceContext] | None = field(
        default=None, kw_only=True
    )
    ingestion_context: Callable[[UUID, UUID, str], TrustedPersistenceContext] | None = field(
        default=None, kw_only=True
    )
    governance_context: Callable[
        [UUID, UUID, UUID, str], TrustedPersistenceContext
    ] | None = field(default=None, kw_only=True)
    interactive_environment_context: Callable[
        [UUID, UUID, PersistenceOperation, UUID], TrustedPersistenceContext
    ] | None = field(default=None, kw_only=True)


@dataclass(frozen=True)
class AuditPersistenceAdapter(PersistenceAdapter):
    """In-memory audit capabilities owned by Issue #63 contract tests."""

    audit_events: AuditEventRegistry
    read_audit_event: Callable[
        [TrustedPersistenceContext, UUID], Awaitable[AuditEvent | None]
    ]
    fail_next_audit_append: Callable[[], None]


@dataclass(frozen=True)
class DeliveryPersistenceAdapter(AuditPersistenceAdapter):
    """Adapter-neutral delivery capabilities owned by Issue #65 tests."""

    delivery_repository: OutboxDeliveryRepository
    delivery_routes: OutboxDeliveryRouteRegistry
