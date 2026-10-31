"""Production composition for authorized disclosure-hold commands."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable
from uuid import UUID

from sqlalchemy import insert, select

from spine.application.disclosure_holds import (
    DisclosureHoldAuthorityError,
    DisclosureHoldService,
)
from spine.application.lifecycle import CurrentLifecycleEligibilityReader
from spine.application.persistence.context import TrustedPersistenceContext
from spine.domain.common import ContextOrigin
from spine.domain.sources.disclosure_holds import DisclosureHold, DisclosureHoldRelease
from spine.infrastructure.persistence.postgresql_disclosure_holds import (
    PostgreSQLDisclosureHoldRepository,
)
from spine.infrastructure.persistence.postgresql_mappings import (
    audit_events,
    current_lifecycle_authorities,
    lifecycle_decisions,
    outbox_intents,
)


@runtime_checkable
class _PersistenceTransaction(Protocol):
    async def _execute(self, statement: object, parameters: object | None = None) -> object: ...


class PostgreSQLCurrentDisclosureHoldAuthority:
    """Verify exact current source-governance authority in the hold UoW."""

    async def authorize_create(
        self,
        hold: DisclosureHold,
        context: TrustedPersistenceContext,
        transaction: object | None,
    ) -> None:
        await self._authorize(
            hold=hold,
            authority_reference=hold.authority_reference,
            authority_version_id=hold.authority_version_id,
            authority_digest=hold.authority_digest,
            actor_id=hold.actor_id,
            context=context,
            transaction=transaction,
            operation="disclosure_hold.create",
        )

    async def authorize_release(
        self,
        release: DisclosureHoldRelease,
        hold: DisclosureHold,
        context: TrustedPersistenceContext,
        transaction: object | None,
    ) -> None:
        if (
            release.workspace_id,
            release.environment_id,
            release.source_object_id,
            release.hold_id,
        ) != (
            hold.workspace_id,
            hold.environment_id,
            hold.source_object_id,
            hold.hold_id,
        ):
            raise DisclosureHoldAuthorityError()
        await self._authorize(
            hold=hold,
            authority_reference=release.authority_reference,
            authority_version_id=release.authority_version_id,
            authority_digest=release.authority_digest,
            actor_id=release.actor_id,
            context=context,
            transaction=transaction,
            operation="disclosure_hold.release",
        )

    async def _authorize(
        self,
        *,
        hold: DisclosureHold,
        authority_reference: UUID,
        authority_version_id: UUID,
        authority_digest: str,
        actor_id: UUID,
        context: TrustedPersistenceContext,
        transaction: object | None,
        operation: str,
    ) -> None:
        if (
            transaction is None
            or context.origin is not ContextOrigin.INTERACTIVE
            or context.acting_subject_id != actor_id
            or context.purpose.value != "source_governance"
            or context.operation.value != operation
        ):
            raise DisclosureHoldAuthorityError()
        assert isinstance(transaction, _PersistenceTransaction)
        result = await transaction._execute(
            select(
                lifecycle_decisions.c.authority_reference,
                lifecycle_decisions.c.authority_version_id,
                lifecycle_decisions.c.record["authority_digest"]
                .as_string()
                .label("authority_digest"),
                lifecycle_decisions.c.actor_id,
            )
            .select_from(
                current_lifecycle_authorities.join(
                    lifecycle_decisions,
                    (lifecycle_decisions.c.workspace_id == current_lifecycle_authorities.c.workspace_id)
                    & (lifecycle_decisions.c.environment_id == current_lifecycle_authorities.c.environment_id)
                    & (lifecycle_decisions.c.source_object_id == current_lifecycle_authorities.c.source_object_id)
                    & (lifecycle_decisions.c.decision_id == current_lifecycle_authorities.c.current_decision_id)
                    & (lifecycle_decisions.c.generation == current_lifecycle_authorities.c.generation),
                )
            )
            .where(
                current_lifecycle_authorities.c.workspace_id == hold.workspace_id,
                current_lifecycle_authorities.c.environment_id == hold.environment_id,
                current_lifecycle_authorities.c.source_object_id == hold.source_object_id,
                current_lifecycle_authorities.c.current_decision_id == hold.review_decision_id,
            )
            .with_for_update(of=current_lifecycle_authorities)
        )
        current = result.mappings().one_or_none()  # type: ignore[attr-defined]
        if current is None or (
            current["authority_reference"],
            current["authority_version_id"],
            current["authority_digest"],
            current["actor_id"],
        ) != (
            authority_reference,
            authority_version_id,
            authority_digest,
            actor_id,
        ):
            raise DisclosureHoldAuthorityError()


class PostgreSQLDisclosureHoldEffectsWriter:
    """Append safe audit and outbox facts inside the hold transaction."""

    async def persist_hold(
        self,
        hold: DisclosureHold,
        context: TrustedPersistenceContext,
        transaction: object,
    ) -> tuple[UUID, UUID]:
        await self._persist(
            audit_event_id=hold.audit_event_id,
            outbox_event_id=hold.outbox_event_id,
            hold_id=hold.hold_id,
            occurred_at=hold.effective_from,
            reason=hold.reason_code.value,
            transition="created",
            context=context,
            transaction=transaction,
        )
        return hold.audit_event_id, hold.outbox_event_id

    async def persist_release(
        self,
        release: DisclosureHoldRelease,
        context: TrustedPersistenceContext,
        transaction: object,
    ) -> tuple[UUID, UUID]:
        await self._persist(
            audit_event_id=release.audit_event_id,
            outbox_event_id=release.outbox_event_id,
            hold_id=release.hold_id,
            occurred_at=release.released_at,
            reason=release.reason_code.value,
            transition="released",
            context=context,
            transaction=transaction,
        )
        return release.audit_event_id, release.outbox_event_id

    async def _persist(
        self,
        *,
        audit_event_id: UUID,
        outbox_event_id: UUID,
        hold_id: UUID,
        occurred_at: object,
        reason: str,
        transition: str,
        context: TrustedPersistenceContext,
        transaction: object,
    ) -> None:
        if not isinstance(transaction, _PersistenceTransaction):
            raise DisclosureHoldAuthorityError()
        payload = {"hold_id": str(hold_id), "transition": transition}
        await transaction._execute(
            insert(audit_events).values(
                audit_event_id=audit_event_id,
                workspace_id=context.scope.workspace_id,
                environment_id=context.scope.environment_id,
                event_type=f"source.disclosure_hold.{transition}",
                schema_version=1,
                origin=context.origin.value,
                acting_subject_id=context.acting_subject_id,
                service_principal_id=context.service_principal_id,
                trace_id=context.trace_id,
                correlation_id=None,
                causation_id=None,
                occurred_at=occurred_at,
                appended_at=occurred_at,
                target_type="disclosure_hold",
                target_id=hold_id,
                target_schema_version=1,
                outcome="committed",
                reason=reason,
                producer_deduplication_id=f"disclosure-hold:{transition}:{audit_event_id}",
                payload=payload,
            )
        )
        await transaction._execute(
            insert(outbox_intents).values(
                event_id=outbox_event_id,
                workspace_id=context.scope.workspace_id,
                environment_id=context.scope.environment_id,
                event_type=f"source.disclosure_hold.{transition}",
                event_schema_version=1,
                aggregate_type="disclosure_hold",
                aggregate_id=hold_id,
                aggregate_schema_version=1,
                producer_deduplication_id=f"disclosure-hold:{transition}:{outbox_event_id}",
                payload=payload,
                trace_id=context.trace_id,
                correlation_id=None,
                causation_id=None,
                created_at=occurred_at,
            )
        )


def compose_disclosure_hold_service(
    *,
    uow_factory: Callable[[TrustedPersistenceContext], object],
    lifecycle: CurrentLifecycleEligibilityReader,
) -> DisclosureHoldService:
    """Build the production service from server-bound authority and SQL effects."""

    repository = PostgreSQLDisclosureHoldRepository(
        uow_factory=uow_factory,
        authority=PostgreSQLCurrentDisclosureHoldAuthority(),
        effects=PostgreSQLDisclosureHoldEffectsWriter(),
    )
    return DisclosureHoldService(repository=repository, lifecycle=lifecycle)


__all__ = [
    "PostgreSQLCurrentDisclosureHoldAuthority",
    "PostgreSQLDisclosureHoldEffectsWriter",
    "compose_disclosure_hold_service",
]
