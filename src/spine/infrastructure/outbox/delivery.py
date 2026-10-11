"""PostgreSQL outbox delivery persistence for the narrow dispatcher role."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import or_, select, update
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncSession
from spine.application.persistence.delivery import (
    ClaimedOutboxDelivery,
    OutboxDelivery,
    OutboxDeliveryRouteRegistry,
    OutboxDeliveryStatus,
    StaleDeliveryLeaseError,
    UnresolvedOutboxIntent,
)
from spine.application.outbox.dispatch import (
    OutboxDeliveryFinalization,
    validate_outbox_delivery_audit,
)
from spine.application.diagnostics.audit import AuditEventRegistry
from spine.domain.audit import AuditEvent
from spine.application.persistence.outbox import (
    OpaqueObjectReference,
    OutboxEventRegistry,
    OutboxIntent,
    UnsupportedOutboxEventError,
)
from spine.infrastructure.db.engine import SessionFactory
from spine.infrastructure.persistence.postgresql_errors import (
    translate_persistence_error,
)
from spine.infrastructure.persistence.postgresql_mappings import (
    outbox_deliveries as _DELIVERIES,
    outbox_intents as _INTENTS,
)


_INTENT_HEADER_COLUMNS = (
    _INTENTS.c.event_id,
    _INTENTS.c.workspace_id,
    _INTENTS.c.environment_id,
    _INTENTS.c.event_type,
    _INTENTS.c.event_schema_version,
    _INTENTS.c.aggregate_type,
    _INTENTS.c.aggregate_id,
    _INTENTS.c.aggregate_schema_version,
    _INTENTS.c.producer_deduplication_id,
    _INTENTS.c.trace_id,
    _INTENTS.c.correlation_id,
    _INTENTS.c.causation_id,
    _INTENTS.c.created_at,
)


def _intent_from_row(
    row: object,
    registry: OutboxEventRegistry,
) -> OutboxIntent | UnresolvedOutboxIntent:
    try:
        payload_type = registry.resolve(row.event_type, row.event_schema_version)
    except UnsupportedOutboxEventError:
        return UnresolvedOutboxIntent(
            event_id=row.event_id,
            workspace_id=row.workspace_id,
            environment_id=row.environment_id,
            event_type=row.event_type,
            schema_version=row.event_schema_version,
            trace_id=row.trace_id,
            correlation_id=row.correlation_id,
            causation_id=row.causation_id,
        )
    payload_snapshot = row.payload
    if not isinstance(payload_snapshot, Mapping):
        raise ValueError("Persisted outbox payload is invalid.")
    payload = payload_type.model_validate(dict(payload_snapshot))
    aggregate = None
    if row.aggregate_id is not None:
        aggregate = OpaqueObjectReference(
            object_type=row.aggregate_type,
            object_id=row.aggregate_id,
            schema_version=row.aggregate_schema_version,
        )
    return registry.build_intent(
        event_id=row.event_id,
        workspace_id=row.workspace_id,
        environment_id=row.environment_id,
        event_type=row.event_type,
        schema_version=row.event_schema_version,
        payload=payload,
        aggregate=aggregate,
        producer_deduplication_id=row.producer_deduplication_id,
        trace_id=row.trace_id,
        correlation_id=row.correlation_id,
        causation_id=row.causation_id,
    )


def _delivery_from_row(row: object) -> OutboxDelivery:
    return OutboxDelivery(
        event_id=row.event_id,
        workspace_id=row.workspace_id,
        environment_id=row.environment_id,
        consumer_name=row.consumer_name,
        status=OutboxDeliveryStatus(row.status),
        attempt_count=row.attempt_count,
        next_attempt_at=row.next_attempt_at,
        lease_owner=row.lease_owner,
        lease_token=row.lease_token,
        lease_expires_at=row.lease_expires_at,
        last_error_code=row.last_error_code,
        delivered_at=row.delivered_at,
        quarantined_at=row.quarantined_at,
        version=row.version,
    )


class PostgreSQLOutboxDeliveryRepository:
    """Atomic claim and lease transitions over immutable outbox intents."""

    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        outbox_events: OutboxEventRegistry,
        delivery_routes: OutboxDeliveryRouteRegistry,
        lease_token_factory: Callable[[], UUID] = uuid4,
        audit_events: AuditEventRegistry | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._outbox_events = outbox_events
        delivery_routes.require_sealed()
        self._delivery_routes = delivery_routes
        self._lease_token_factory = lease_token_factory
        self._audit_events = audit_events

    async def _materialize_with_session(
        self,
        session: AsyncSession,
        *,
        limit: int,
        materialized_at: datetime,
    ) -> tuple[OutboxDelivery, ...]:
        missing = await session.execute(
            select(*_INTENT_HEADER_COLUMNS)
            .outerjoin(
                _DELIVERIES,
                _DELIVERIES.c.event_id == _INTENTS.c.event_id,
            )
            .where(_DELIVERIES.c.event_id.is_(None))
            .order_by(_INTENTS.c.created_at, _INTENTS.c.event_id)
            .limit(limit)
        )
        materialized: list[OutboxDelivery] = []
        for row in missing.mappings():
            route = self._delivery_routes.resolve_optional(
                row.event_type,
                row.event_schema_version,
            )
            inserted = await session.execute(
                postgresql_insert(_DELIVERIES)
                .values(
                    event_id=row.event_id,
                    workspace_id=row.workspace_id,
                    environment_id=row.environment_id,
                    consumer_name=(
                        route.consumer_name if route is not None else None
                    ),
                    status=OutboxDeliveryStatus.PENDING.value,
                    attempt_count=0,
                    next_attempt_at=materialized_at,
                    version=0,
                    created_at=materialized_at,
                    updated_at=materialized_at,
                )
                .on_conflict_do_nothing(index_elements=[_DELIVERIES.c.event_id])
                .returning(_DELIVERIES)
            )
            delivery_row = inserted.mappings().one_or_none()
            if delivery_row is not None:
                materialized.append(_delivery_from_row(delivery_row))
        return tuple(materialized)

    async def materialize(
        self,
        *,
        limit: int,
        materialized_at: datetime,
    ) -> tuple[OutboxDelivery, ...]:
        if isinstance(limit, bool) or limit <= 0 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000.")
        session = self._session_factory()
        try:
            async with session.begin():
                return await self._materialize_with_session(
                    session,
                    limit=limit,
                    materialized_at=materialized_at,
                )
        except BaseException as error:
            if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            raise translate_persistence_error(error) from None
        finally:
            await session.close()

    async def claim_due(
        self,
        *,
        limit: int,
        worker_identity: str,
        claimed_at: datetime,
        lease_duration: timedelta,
    ) -> tuple[ClaimedOutboxDelivery, ...]:
        if isinstance(limit, bool) or limit <= 0 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000.")
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive.")
        session = self._session_factory()
        try:
            async with session.begin():
                await self._materialize_with_session(
                    session,
                    limit=limit,
                    materialized_at=claimed_at,
                )

                due = await session.execute(
                    select(_DELIVERIES)
                    .where(
                        or_(
                            (
                                _DELIVERIES.c.status.in_(
                                    (
                                        OutboxDeliveryStatus.PENDING.value,
                                        OutboxDeliveryStatus.RETRY_SCHEDULED.value,
                                    )
                                )
                                & (_DELIVERIES.c.next_attempt_at <= claimed_at)
                            ),
                            (
                                _DELIVERIES.c.status
                                == OutboxDeliveryStatus.IN_FLIGHT.value
                            )
                            & (_DELIVERIES.c.lease_expires_at <= claimed_at),
                        )
                    )
                    .order_by(
                        _DELIVERIES.c.next_attempt_at.asc().nulls_last(),
                        _DELIVERIES.c.lease_expires_at.asc().nulls_last(),
                        _DELIVERIES.c.event_id,
                    )
                    .limit(limit)
                    .with_for_update(skip_locked=True, of=_DELIVERIES)
                )
                claims: list[ClaimedOutboxDelivery] = []
                lease_expires_at = claimed_at + lease_duration
                for due_row in due.mappings():
                    lease_token = self._lease_token_factory()
                    attempt_number = due_row.attempt_count + 1
                    version = due_row.version + 1
                    updated = await session.execute(
                        update(_DELIVERIES)
                        .where(
                            _DELIVERIES.c.event_id == due_row.event_id,
                            _DELIVERIES.c.version == due_row.version,
                        )
                        .values(
                            status=OutboxDeliveryStatus.IN_FLIGHT.value,
                            attempt_count=attempt_number,
                            next_attempt_at=None,
                            lease_owner=worker_identity,
                            lease_token=lease_token,
                            lease_expires_at=lease_expires_at,
                            last_error_code=None,
                            delivered_at=None,
                            quarantined_at=None,
                            version=version,
                            updated_at=claimed_at,
                        )
                        .returning(_DELIVERIES)
                    )
                    delivery_row = updated.mappings().one()
                    header_result = await session.execute(
                        select(*_INTENT_HEADER_COLUMNS).where(
                            _INTENTS.c.event_id == delivery_row.event_id
                        )
                    )
                    header = header_result.mappings().one()
                    try:
                        self._outbox_events.resolve(
                            header.event_type,
                            header.event_schema_version,
                        )
                    except UnsupportedOutboxEventError:
                        intent = _intent_from_row(header, self._outbox_events)
                    else:
                        intent_result = await session.execute(
                            select(_INTENTS).where(
                                _INTENTS.c.event_id == delivery_row.event_id
                            )
                        )
                        intent = _intent_from_row(
                            intent_result.mappings().one(),
                            self._outbox_events,
                        )
                    claims.append(
                        ClaimedOutboxDelivery(
                            intent=intent,
                            consumer_name=delivery_row.consumer_name,
                            worker_identity=worker_identity,
                            lease_token=lease_token,
                            claimed_at=claimed_at,
                            lease_expires_at=lease_expires_at,
                            attempt_number=attempt_number,
                            delivery_version=version,
                        )
                    )
            return tuple(claims)
        except BaseException as error:
            if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            raise translate_persistence_error(error) from None
        finally:
            await session.close()

    async def resolve(self, event_id: UUID) -> OutboxDelivery | None:
        if not isinstance(event_id, UUID) or event_id.int == 0:
            raise ValueError("event_id must be a non-zero UUID.")
        session = self._session_factory()
        try:
            result = await session.execute(
                select(_DELIVERIES).where(_DELIVERIES.c.event_id == event_id)
            )
            row = result.mappings().one_or_none()
            return _delivery_from_row(row) if row is not None else None
        except BaseException as error:
            if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            raise translate_persistence_error(error) from None
        finally:
            await session.close()

    async def _transition(
        self,
        claim: ClaimedOutboxDelivery,
        *,
        transition_at: datetime,
        values: Mapping[str, object],
    ) -> OutboxDelivery:
        event_id = claim.intent.event_id
        if event_id is None:
            raise StaleDeliveryLeaseError("Outbox delivery lease is not active.")
        session = self._session_factory()
        try:
            async with session.begin():
                result = await session.execute(
                    update(_DELIVERIES)
                    .where(
                        _DELIVERIES.c.event_id == event_id,
                        _DELIVERIES.c.workspace_id == claim.intent.workspace_id,
                        (
                            _DELIVERIES.c.environment_id.is_(None)
                            if claim.intent.environment_id is None
                            else _DELIVERIES.c.environment_id
                            == claim.intent.environment_id
                        ),
                        _DELIVERIES.c.status
                        == OutboxDeliveryStatus.IN_FLIGHT.value,
                        _DELIVERIES.c.lease_owner == claim.worker_identity,
                        _DELIVERIES.c.lease_token == claim.lease_token,
                        _DELIVERIES.c.attempt_count == claim.attempt_number,
                        _DELIVERIES.c.version == claim.delivery_version,
                        _DELIVERIES.c.lease_expires_at > transition_at,
                    )
                    .values(**values)
                    .returning(_DELIVERIES)
                )
                row = result.mappings().one_or_none()
                if row is None:
                    raise StaleDeliveryLeaseError(
                        "Outbox delivery lease is not active."
                    )
                return _delivery_from_row(row)
        except BaseException as error:
            if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            raise translate_persistence_error(error) from None
        finally:
            await session.close()

    async def acknowledge(
        self,
        claim: ClaimedOutboxDelivery,
        *,
        completed_at: datetime,
    ) -> OutboxDelivery:
        return await self._transition(
            claim,
            transition_at=completed_at,
            values={
                "status": OutboxDeliveryStatus.DELIVERED.value,
                "lease_owner": None,
                "lease_token": None,
                "lease_expires_at": None,
                "last_error_code": None,
                "delivered_at": completed_at,
                "version": claim.delivery_version + 1,
                "updated_at": completed_at,
            },
        )

    async def reschedule(
        self,
        claim: ClaimedOutboxDelivery,
        *,
        failed_at: datetime,
        next_attempt_at: datetime,
        error_code: str,
    ) -> OutboxDelivery:
        if next_attempt_at <= failed_at:
            raise ValueError("next_attempt_at must follow failed_at.")
        return await self._transition(
            claim,
            transition_at=failed_at,
            values={
                "status": OutboxDeliveryStatus.RETRY_SCHEDULED.value,
                "next_attempt_at": next_attempt_at,
                "lease_owner": None,
                "lease_token": None,
                "lease_expires_at": None,
                "last_error_code": error_code,
                "version": claim.delivery_version + 1,
                "updated_at": failed_at,
            },
        )

    async def quarantine(
        self,
        claim: ClaimedOutboxDelivery,
        *,
        quarantined_at: datetime,
        error_code: str,
    ) -> OutboxDelivery:
        return await self._transition(
            claim,
            transition_at=quarantined_at,
            values={
                "status": OutboxDeliveryStatus.QUARANTINED.value,
                "lease_owner": None,
                "lease_token": None,
                "lease_expires_at": None,
                "last_error_code": error_code,
                "quarantined_at": quarantined_at,
                "version": claim.delivery_version + 1,
                "updated_at": quarantined_at,
            },
        )

    async def renew(
        self,
        claim: ClaimedOutboxDelivery,
        *,
        renewed_at: datetime,
        lease_duration: timedelta,
    ) -> ClaimedOutboxDelivery:
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive.")
        lease_token = self._lease_token_factory()
        lease_expires_at = renewed_at + lease_duration
        renewed = await self._transition(
            claim,
            transition_at=renewed_at,
            values={
                "lease_token": lease_token,
                "lease_expires_at": lease_expires_at,
                "version": claim.delivery_version + 1,
                "updated_at": renewed_at,
            },
        )
        return ClaimedOutboxDelivery(
            intent=claim.intent,
            consumer_name=renewed.consumer_name,
            worker_identity=claim.worker_identity,
            lease_token=lease_token,
            claimed_at=renewed_at,
            lease_expires_at=lease_expires_at,
            attempt_number=claim.attempt_number,
            delivery_version=renewed.version,
        )

    async def finalize(
        self,
        claim: ClaimedOutboxDelivery,
        *,
        finalization: OutboxDeliveryFinalization,
        audit_event: AuditEvent,
    ) -> OutboxDelivery:
        if self._audit_events is None:
            raise RuntimeError("Audit Event registry is required for finalization.")
        canonical_audit = validate_outbox_delivery_audit(
            claim,
            finalization,
            audit_event,
            registry=self._audit_events,
        )
        event_id = claim.intent.event_id
        assert event_id is not None
        assert canonical_audit.audit_event_id is not None
        assert canonical_audit.service_principal_id is not None
        session = self._session_factory()
        try:
            async with session.begin():
                result = await session.execute(
                    text(
                        "SELECT * FROM spine.finalize_outbox_delivery("
                        ":event_id, :workspace_id, :environment_id, "
                        ":worker_identity, :lease_token, "
                        ":attempt_number, :delivery_version, :status, "
                        ":occurred_at, :next_attempt_at, :error_code, "
                        ":audit_event_id, :service_principal_id)"
                    ),
                    {
                        "event_id": event_id,
                        "workspace_id": claim.intent.workspace_id,
                        "environment_id": claim.intent.environment_id,
                        "worker_identity": claim.worker_identity,
                        "lease_token": claim.lease_token,
                        "attempt_number": claim.attempt_number,
                        "delivery_version": claim.delivery_version,
                        "status": finalization.status.value,
                        "occurred_at": finalization.occurred_at,
                        "next_attempt_at": finalization.next_attempt_at,
                        "error_code": finalization.error_code,
                        "audit_event_id": canonical_audit.audit_event_id,
                        "service_principal_id": canonical_audit.service_principal_id,
                    },
                )
                row = result.mappings().one_or_none()
                if row is None:
                    raise StaleDeliveryLeaseError(
                        "Outbox delivery lease is not active."
                    )
                return _delivery_from_row(row)
        except BaseException as error:
            if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            raise translate_persistence_error(error) from None
        finally:
            await session.close()


__all__ = ["PostgreSQLOutboxDeliveryRepository"]
