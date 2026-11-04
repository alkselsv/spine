"""Tenant-scoped PostgreSQL repository for durable email sync checkpoints."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID

from sqlalchemy import insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from spine.application.email_sync import (
    EmailSyncAttempt,
    EmailSyncConcurrentUpdateError,
    EmailSyncPageState,
    EmailSyncRepository,
    EmailSyncStatus,
    EmailSyncEffectReceipt,
    EmailSyncStateObservation,
    EmailSyncStateObservationRepository,
    EmailSyncStateObservedOutboxPayload,
    EMAIL_SYNC_STATE_OBSERVED_EVENT_TYPE,
    EMAIL_SYNC_STATE_OBSERVED_SCHEMA_VERSION,
    register_email_sync_outbox_event,
)
from spine.application.persistence.context import PersistenceOperation
from spine.application.persistence.outbox import OpaqueObjectReference, OutboxEventRegistry
from spine.domain.sources.email_lifecycle import EmailLifecycleReceipt
from spine.infrastructure.persistence.postgresql_mappings import (
    email_sync_attempts,
    email_sync_effect_receipts,
    email_sync_pages,
    email_sync_state_observations,
)


class PostgreSQLEmailSyncStateObservationRepository(EmailSyncStateObservationRepository):
    """Append a state observation and its publication intent in one UoW."""

    def __init__(
        self,
        uow_factory: Callable,
        context_factory: Callable,
        outbox_events: OutboxEventRegistry,
    ) -> None:
        self._uow_factory = uow_factory
        self._context_factory = context_factory
        self._outbox_events = outbox_events
        register_email_sync_outbox_event(outbox_events)

    async def record(self, observation: EmailSyncStateObservation) -> UUID:
        context = self._context_factory(
            PersistenceOperation("email_sync_state_observation"),
            observation.observation_id,
        )
        if (
            getattr(context.scope, "workspace_id", None) != observation.workspace_id
            or getattr(context.scope, "environment_id", None) != observation.environment_id
        ):
            raise ValueError("email sync state persistence scope is invalid")
        async with self._uow_factory(context) as uow:
            await uow._execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": str(observation.observation_id)},
            )
            result = await uow._execute(
                select(email_sync_state_observations).where(
                    email_sync_state_observations.c.workspace_id == observation.workspace_id,
                    email_sync_state_observations.c.environment_id == observation.environment_id,
                    email_sync_state_observations.c.observation_id == observation.observation_id,
                )
            )
            existing = result.mappings().one_or_none()
            if existing is not None:
                stored = EmailSyncStateObservation.model_validate(existing["record"])
                if stored != observation:
                    raise ValueError("email sync state observation conflicts")
                await uow.rollback()
                return observation.observation_id
            intent = self._outbox_events.build_intent(
                event_id=observation.outbox_event_id,
                workspace_id=observation.workspace_id,
                environment_id=observation.environment_id,
                event_type=EMAIL_SYNC_STATE_OBSERVED_EVENT_TYPE,
                schema_version=EMAIL_SYNC_STATE_OBSERVED_SCHEMA_VERSION,
                payload=EmailSyncStateObservedOutboxPayload(
                    observation_id=observation.observation_id,
                    connection_id=observation.connection_id,
                    mailbox_binding_id=observation.mailbox_binding_id,
                    state_kind=observation.state_kind,
                    provider_event_digest=observation.provider_event_digest,
                ),
                trace_id=context.trace_id,
                aggregate=OpaqueObjectReference(
                    object_type="mailbox_binding",
                    object_id=observation.mailbox_binding_id,
                    schema_version=1,
                ),
                producer_deduplication_id=f"email-sync-state:{observation.observation_id}",
            )
            event_id = await uow.outbox.append(intent)
            if event_id != observation.outbox_event_id:
                raise ValueError("email sync state outbox identity conflicts")
            await uow._execute(
                insert(email_sync_state_observations).values(
                    workspace_id=observation.workspace_id,
                    environment_id=observation.environment_id,
                    observation_id=observation.observation_id,
                    connection_id=observation.connection_id,
                    mailbox_binding_id=observation.mailbox_binding_id,
                    state_kind=observation.state_kind,
                    provider_event_digest=observation.provider_event_digest,
                    outbox_event_id=observation.outbox_event_id,
                    record=observation.model_dump(mode="json"),
                )
            )
            await uow.commit()
            return observation.observation_id


class PostgreSQLEmailSyncRepository(EmailSyncRepository):
    """Persist a sync aggregate with a revision CAS and page receipt ledger."""

    def __init__(self, uow_factory: Callable, context_factory: Callable) -> None:
        self._uow_factory = uow_factory
        self._context_factory = context_factory

    async def get(
        self, attempt_id: UUID, workspace_id: UUID, environment_id: UUID
    ) -> EmailSyncAttempt | None:
        async with self._uow("email_sync_read", attempt_id, workspace_id, environment_id) as uow:
            result = await uow._execute(
                select(email_sync_attempts).where(
                    email_sync_attempts.c.attempt_id == attempt_id,
                    email_sync_attempts.c.workspace_id == workspace_id,
                    email_sync_attempts.c.environment_id == environment_id,
                )
            )
            row = result.mappings().one_or_none()
            if row is None:
                await uow.rollback()
                return None
            pages_result = await uow._execute(
                select(email_sync_pages).where(
                    email_sync_pages.c.attempt_id == attempt_id,
                    email_sync_pages.c.workspace_id == workspace_id,
                    email_sync_pages.c.environment_id == environment_id,
                ).order_by(email_sync_pages.c.page_id)
            )
            receipt_result = await uow._execute(
                select(email_sync_effect_receipts).where(
                    email_sync_effect_receipts.c.attempt_id == attempt_id,
                    email_sync_effect_receipts.c.workspace_id == workspace_id,
                    email_sync_effect_receipts.c.environment_id == environment_id,
                ).order_by(
                    email_sync_effect_receipts.c.page_id,
                    email_sync_effect_receipts.c.effect_key,
                )
            )
            receipt_rows = receipt_result.mappings().all()
            receipt_index: dict[str, list[EmailSyncEffectReceipt]] = {}
            for receipt in receipt_rows:
                receipt_index.setdefault(receipt["page_id"], []).append(
                    EmailSyncEffectReceipt(
                        effect_key=receipt["effect_key"],
                        receipt_reference=receipt["receipt_reference"],
                        durable=receipt["durable"],
                    )
                )
            pages = tuple(
                EmailSyncPageState.model_validate(
                    {
                        **page["record"],
                        "committed": page["committed"],
                        "effect_receipts": receipt_index.get(page["page_id"], []),
                    }
                )
                for page in pages_result.mappings().all()
            )
            await uow.rollback()
            return EmailSyncAttempt.model_validate({**row["record"], "pages": pages})

    async def save(self, attempt: EmailSyncAttempt) -> EmailSyncAttempt:
        async with self._uow(
            "email_sync_write",
            attempt.attempt_id,
            attempt.admission.workspace_id,
            attempt.admission.environment_id,
        ) as uow:
            scope = {
                "workspace_id": attempt.admission.workspace_id,
                "environment_id": attempt.admission.environment_id,
                "attempt_id": attempt.attempt_id,
            }
            current_result = await uow._execute(
                select(email_sync_attempts)
                .where(*[column == value for column, value in (
                    (email_sync_attempts.c.workspace_id, scope["workspace_id"]),
                    (email_sync_attempts.c.environment_id, scope["environment_id"]),
                    (email_sync_attempts.c.attempt_id, scope["attempt_id"]),
                )])
                .with_for_update()
            )
            current = current_result.mappings().one_or_none()
            if current is None:
                values = self._attempt_values(attempt, revision=attempt.revision)
                created = await uow._execute(
                    pg_insert(email_sync_attempts)
                    .values(**values)
                    .on_conflict_do_nothing()
                    .returning(email_sync_attempts.c.attempt_id)
                )
                if created.scalar_one_or_none() is None:
                    # An identical create may race with another worker. Read its committed
                    # result after the conflict and let the caller continue from it.
                    await uow.rollback()
                    existing = await self.get(
                        attempt.attempt_id,
                        attempt.admission.workspace_id,
                        attempt.admission.environment_id,
                    )
                    if (
                        existing is None
                        or existing.admission != attempt.admission
                        or existing.attempt_key != attempt.attempt_key
                    ):
                        raise EmailSyncConcurrentUpdateError()
                    return existing
                await self._persist_pages(uow, attempt)
                await uow.commit()
                return attempt

            if (
                current["revision"] != attempt.revision
                or current["authorization_generation"] != attempt.admission.authorization_generation
                or current["configuration_generation"] != attempt.admission.configuration_generation
                or current["scope_generation"] != attempt.admission.scope_generation
                or current["attempt_key"] != attempt.attempt_key
            ):
                raise EmailSyncConcurrentUpdateError()
            next_revision = current["revision"] + 1
            await self._persist_pages(uow, attempt)
            updated = await uow._execute(
                update(email_sync_attempts)
                .where(
                    email_sync_attempts.c.workspace_id == scope["workspace_id"],
                    email_sync_attempts.c.environment_id == scope["environment_id"],
                    email_sync_attempts.c.attempt_id == scope["attempt_id"],
                    email_sync_attempts.c.revision == attempt.revision,
                    email_sync_attempts.c.authorization_generation == attempt.admission.authorization_generation,
                    email_sync_attempts.c.configuration_generation == attempt.admission.configuration_generation,
                    email_sync_attempts.c.scope_generation == attempt.admission.scope_generation,
                )
                .values(**self._attempt_values(attempt, revision=next_revision))
                .returning(email_sync_attempts.c.attempt_id)
            )
            if updated.scalar_one_or_none() is None:
                raise EmailSyncConcurrentUpdateError()
            await uow.commit()
            return attempt.model_copy(update={"revision": next_revision})

    async def fence_for_reconciliation(
        self, attempt: EmailSyncAttempt, *, failure_code: str
    ) -> EmailSyncAttempt:
        """Atomically fence the latest revision and restore the detected cursor."""

        workspace_id = attempt.admission.workspace_id
        environment_id = attempt.admission.environment_id
        async with self._uow(
            "email_sync_fence",
            attempt.attempt_id,
            workspace_id,
            environment_id,
        ) as uow:
            result = await uow._execute(
                select(email_sync_attempts)
                .where(
                    email_sync_attempts.c.workspace_id == workspace_id,
                    email_sync_attempts.c.environment_id == environment_id,
                    email_sync_attempts.c.attempt_id == attempt.attempt_id,
                )
                .with_for_update()
            )
            row = result.mappings().one_or_none()
            if row is None:
                raise KeyError("email sync attempt not found")
            current = EmailSyncAttempt.model_validate(row["record"])
            if current.admission != attempt.admission:
                raise KeyError("email sync attempt not found")
            if current.status in {EmailSyncStatus.SUSPENDED, EmailSyncStatus.FAILED}:
                await uow.rollback()
                stored = await self.get(
                    attempt.attempt_id,
                    workspace_id,
                    environment_id,
                )
                if stored is None:
                    raise KeyError("email sync attempt not found")
                return stored
            next_revision = row["revision"] + 1
            fenced = current.model_copy(
                update={
                    "status": EmailSyncStatus.RECONCILIATION_REQUIRED,
                    "revision": next_revision,
                    "committed_checkpoint": attempt.committed_checkpoint,
                    "failure_code": failure_code,
                    "updated_at": attempt.updated_at,
                }
            )
            await uow._execute(
                update(email_sync_attempts)
                .where(
                    email_sync_attempts.c.workspace_id == workspace_id,
                    email_sync_attempts.c.environment_id == environment_id,
                    email_sync_attempts.c.attempt_id == attempt.attempt_id,
                )
                .values(**self._attempt_values(fenced, revision=next_revision))
            )
            await uow.commit()
        stored = await self.get(
            attempt.attempt_id,
            workspace_id,
            environment_id,
        )
        if stored is None:
            raise KeyError("email sync attempt not found")
        return stored

    async def resolve_lifecycle_receipt(
        self, receipt_id: UUID, workspace_id: UUID, environment_id: UUID
    ) -> EmailLifecycleReceipt | None:
        async with self._uow(
            "email_sync_lifecycle_receipt_read",
            receipt_id,
            workspace_id,
            environment_id,
        ) as uow:
            result = await uow._execute(
                select(
                    email_sync_pages.c.record,
                    email_sync_pages.c.committed,
                ).where(
                    email_sync_pages.c.workspace_id == workspace_id,
                    email_sync_pages.c.environment_id == environment_id,
                )
            )
            resolved: EmailLifecycleReceipt | None = None
            for record, committed in result.all():
                if not committed:
                    continue
                for payload in record.get("lifecycle_receipts", ()):
                    candidate = EmailLifecycleReceipt.model_validate(payload)
                    if candidate.receipt_id != receipt_id:
                        continue
                    if resolved is not None and resolved != candidate:
                        raise EmailSyncConcurrentUpdateError()
                    resolved = candidate
            await uow.rollback()
            return resolved

    async def _persist_pages(self, uow: Any, attempt: EmailSyncAttempt) -> None:
        workspace_id = attempt.admission.workspace_id
        environment_id = attempt.admission.environment_id
        for page in attempt.pages:
            page_values = {
                "workspace_id": workspace_id,
                "environment_id": environment_id,
                "attempt_id": attempt.attempt_id,
                "page_id": page.page_id,
                "page_digest": page.page_digest,
                "committed": page.committed,
                "record": page.model_dump(mode="json", exclude={"effect_receipts", "committed"}),
            }
            result = await uow._execute(
                pg_insert(email_sync_pages)
                .values(**page_values)
                .on_conflict_do_update(
                    index_elements=["workspace_id", "environment_id", "attempt_id", "page_id"],
                    set_={"committed": page.committed, "record": page_values["record"]},
                    where=email_sync_pages.c.page_digest == page.page_digest,
                )
                .returning(email_sync_pages.c.page_id)
            )
            if result.scalar_one_or_none() is None:
                raise EmailSyncConcurrentUpdateError()
            for receipt in page.effect_receipts:
                key = (
                    email_sync_effect_receipts.c.workspace_id == workspace_id,
                    email_sync_effect_receipts.c.environment_id == environment_id,
                    email_sync_effect_receipts.c.attempt_id == attempt.attempt_id,
                    email_sync_effect_receipts.c.page_id == page.page_id,
                    email_sync_effect_receipts.c.effect_key == receipt.effect_key,
                )
                existing_result = await uow._execute(
                    select(email_sync_effect_receipts).where(*key).with_for_update()
                )
                existing = existing_result.mappings().one_or_none()
                if existing is None:
                    await uow._execute(
                        insert(email_sync_effect_receipts).values(
                            workspace_id=workspace_id,
                            environment_id=environment_id,
                            attempt_id=attempt.attempt_id,
                            page_id=page.page_id,
                            effect_key=receipt.effect_key,
                            receipt_reference=receipt.receipt_reference,
                            durable=receipt.durable,
                        )
                    )
                elif existing["receipt_reference"] != receipt.receipt_reference:
                    raise EmailSyncConcurrentUpdateError()
                elif not existing["durable"] and receipt.durable:
                    await uow._execute(
                        update(email_sync_effect_receipts).where(*key).values(durable=receipt.durable)
                    )

    @staticmethod
    def _attempt_values(attempt: EmailSyncAttempt, *, revision: int) -> dict[str, Any]:
        admission = attempt.admission
        return {
            "workspace_id": admission.workspace_id,
            "environment_id": admission.environment_id,
            "attempt_id": attempt.attempt_id,
            "attempt_key": attempt.attempt_key,
            "revision": revision,
            "authorization_generation": admission.authorization_generation,
            "configuration_generation": admission.configuration_generation,
            "scope_generation": admission.scope_generation,
            "committed_checkpoint": attempt.committed_checkpoint,
            "record": attempt.model_dump(mode="json", exclude={"pages", "revision"})
            | {"revision": revision},
            "created_at": attempt.created_at,
            "updated_at": attempt.updated_at,
        }

    def _uow(self, operation: str, identity: UUID, workspace_id: UUID, environment_id: UUID):
        context = self._context_factory(PersistenceOperation(operation), identity)
        if getattr(context.scope, "workspace_id", None) != workspace_id or getattr(
            context.scope, "environment_id", None
        ) != environment_id:
            raise ValueError("email sync persistence scope is invalid")
        return self._uow_factory(context)


__all__ = [
    "PostgreSQLEmailSyncRepository",
    "PostgreSQLEmailSyncStateObservationRepository",
]
