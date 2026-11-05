"""PostgreSQL history and CAS publication for tombstone writer selectors."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import insert, select, text, update
from sqlalchemy.exc import IntegrityError

from spine.application.canonical_tombstone_selectors import CanonicalTombstoneSelectorError
from spine.application.persistence.context import PersistenceOperation, PersistencePurpose
from spine.domain.audit import AuditEvent
from spine.domain.common import ContextOrigin
from spine.domain.sources.canonical_tombstone_selector import (
    CanonicalTombstoneSelectorHead,
    TrustedCanonicalTombstoneVersionSelector,
)
from spine.infrastructure.persistence.postgresql_mappings import (
    current_source_deletion_policy_approvals,
    current_trusted_canonical_tombstone_selectors,
    source_deletion_policy_approvals,
    trusted_canonical_tombstone_selector_versions,
)


@dataclass(frozen=True, slots=True)
class TombstoneSelectorPublicationResult:
    selector: TrustedCanonicalTombstoneVersionSelector
    committed_cas_version: int
    replayed: bool


class PostgreSQLCanonicalTombstoneSelectorRepository:
    """Publish immutable versions and serialize one current head per exact scope."""

    def __init__(self, *, uow_factory: Callable[..., object], context_factory: Callable[[str, UUID, UUID], object]) -> None:
        self._uow_factory = uow_factory
        self._context_factory = context_factory

    async def list_current(self, *, workspace_id: UUID, environment_id: UUID):
        context = self._context_factory("canonical_tombstone_selector.read", workspace_id, environment_id)
        versions = trusted_canonical_tombstone_selector_versions
        heads = current_trusted_canonical_tombstone_selectors
        async with self._uow_factory(context) as uow:
            result = await uow._execute(select(versions.c.record, heads.c.current_selector_id, heads.c.cas_version).select_from(
                heads.join(versions, (heads.c.workspace_id == versions.c.workspace_id)
                    & (heads.c.environment_id == versions.c.environment_id)
                    & (heads.c.selector_series_id == versions.c.selector_series_id)
                    & (heads.c.current_selector_id == versions.c.selector_id))
            ).where(heads.c.workspace_id == workspace_id, heads.c.environment_id == environment_id))
            rows = tuple((TrustedCanonicalTombstoneVersionSelector.model_validate(row.record),
                CanonicalTombstoneSelectorHead(workspace_id=workspace_id, environment_id=environment_id,
                    selector_series_id=TrustedCanonicalTombstoneVersionSelector.model_validate(row.record).selector_series_id,
                    current_selector_id=row.current_selector_id, cas_version=row.cas_version)) for row in result.mappings())
            await uow.rollback()
            return rows

    async def publish(self, selector: TrustedCanonicalTombstoneVersionSelector, *, expected_current_selector_id: UUID | None,
                      expected_cas_version: int | None, recorded_at: datetime, audit_event: AuditEvent) -> TombstoneSelectorPublicationResult:
        if (expected_current_selector_id is None) != (expected_cas_version is None):
            raise ValueError("expected selector ID and CAS version must be supplied together")
        context = self._context_factory("canonical_tombstone_selector.publish", selector.workspace_id, selector.environment_id)
        if (context.origin is not ContextOrigin.INTERACTIVE or context.acting_subject_id is None
                or context.purpose != PersistencePurpose("canonical_tombstone_selector")
                or context.operation != PersistenceOperation("canonical_tombstone_selector.publish")):
            raise CanonicalTombstoneSelectorError("Only an authorized interactive context may publish tombstone selectors.")
        versions = trusted_canonical_tombstone_selector_versions
        heads = current_trusted_canonical_tombstone_selectors
        try:
            async with self._uow_factory(context) as uow:
                replay = (await uow._execute(select(versions.c.record).where(
                    versions.c.workspace_id == selector.workspace_id,
                    versions.c.environment_id == selector.environment_id,
                    versions.c.idempotency_identity == selector.idempotency_identity))).scalar_one_or_none()
                if replay is not None:
                    existing = TrustedCanonicalTombstoneVersionSelector.model_validate(replay)
                    if existing != selector:
                        raise CanonicalTombstoneSelectorError("Selector idempotency identity conflicts with history.")
                    await uow.rollback()
                    return TombstoneSelectorPublicationResult(existing, existing.selector_version, True)
                await uow._execute(text("SELECT pg_advisory_xact_lock_shared(hashtextextended(:lock, 0))"), {"lock": str(selector.connection_id)})
                authority = (await uow._execute(select(source_deletion_policy_approvals.c.approval_id).select_from(
                    current_source_deletion_policy_approvals.join(source_deletion_policy_approvals,
                        (current_source_deletion_policy_approvals.c.workspace_id == source_deletion_policy_approvals.c.workspace_id)
                        & (current_source_deletion_policy_approvals.c.environment_id == source_deletion_policy_approvals.c.environment_id)
                        & (current_source_deletion_policy_approvals.c.current_approval_id == source_deletion_policy_approvals.c.approval_id)))
                    .where(current_source_deletion_policy_approvals.c.workspace_id == selector.workspace_id,
                           current_source_deletion_policy_approvals.c.environment_id == selector.environment_id,
                           current_source_deletion_policy_approvals.c.source_contract_id == selector.source_contract_id,
                           current_source_deletion_policy_approvals.c.source_contract_version == selector.source_contract_version,
                           current_source_deletion_policy_approvals.c.connection_id == selector.connection_id,
                           current_source_deletion_policy_approvals.c.resource_scope_reference == selector.resource_scope_reference,
                           current_source_deletion_policy_approvals.c.current_approval_id == selector.approval_reference)
                    .with_for_update(of=current_source_deletion_policy_approvals))).scalar_one_or_none()
                if authority is None:
                    raise CanonicalTombstoneSelectorError("Current deletion-policy approval does not authorize selector.")
                if (audit_event.audit_event_id != selector.audit_reference or audit_event.workspace_id != selector.workspace_id
                        or audit_event.environment_id != selector.environment_id or audit_event.event_type != "command.outcome"
                        or audit_event.outcome.value != "accepted" or audit_event.origin is not context.origin
                        or audit_event.acting_subject_id != context.acting_subject_id
                        or audit_event.service_principal_id != context.service_principal_id):
                    raise CanonicalTombstoneSelectorError("Selector Audit Event is invalid.")
                await uow.audit.append(audit_event)
                current = (await uow._execute(select(heads).where(heads.c.workspace_id == selector.workspace_id,
                    heads.c.environment_id == selector.environment_id, heads.c.selector_series_id == selector.selector_series_id).with_for_update())).mappings().one_or_none()
                actual = None if current is None else (current.current_selector_id, current.cas_version)
                expected = None if expected_current_selector_id is None else (expected_current_selector_id, expected_cas_version)
                if actual != expected:
                    raise CanonicalTombstoneSelectorError("Tombstone selector head changed.")
                version = 1 if current is None else current.cas_version + 1
                predecessor = None if current is None else current.current_selector_id
                if selector.selector_version != version or selector.predecessor_selector_id != predecessor:
                    raise CanonicalTombstoneSelectorError("Tombstone selector successor is invalid.")
                if current is not None:
                    prior = (await uow._execute(select(versions.c.record).where(versions.c.workspace_id == selector.workspace_id,
                        versions.c.environment_id == selector.environment_id, versions.c.selector_id == current.current_selector_id))).scalar_one_or_none()
                    if prior is None or TrustedCanonicalTombstoneVersionSelector.model_validate(prior).series_identity != selector.series_identity:
                        raise CanonicalTombstoneSelectorError("Tombstone selector series identity cannot change.")
                await uow._execute(insert(versions).values(selector_id=selector.selector_id, selector_series_id=selector.selector_series_id,
                    selector_version=selector.selector_version, workspace_id=selector.workspace_id, environment_id=selector.environment_id,
                    source_object_id=selector.source_object_id,
                    source_contract_id=selector.source_contract_id, source_contract_version=selector.source_contract_version,
                    provider_kind=selector.provider_kind, connection_id=selector.connection_id,
                    resource_scope_reference=selector.resource_scope_reference, mode=selector.mode.value,
                    approval_reference=selector.approval_reference, predecessor_selector_id=selector.predecessor_selector_id,
                    audit_reference=selector.audit_reference, idempotency_identity=selector.idempotency_identity,
                    valid_from=selector.valid_from, valid_until=selector.valid_until, record=selector.model_dump(mode="json"), created_at=recorded_at))
                if current is None:
                    await uow._execute(insert(heads).values(workspace_id=selector.workspace_id, environment_id=selector.environment_id,
                        selector_series_id=selector.selector_series_id, current_selector_id=selector.selector_id, cas_version=1, updated_at=recorded_at))
                else:
                    changed = await uow._execute(update(heads).where(heads.c.workspace_id == selector.workspace_id,
                        heads.c.environment_id == selector.environment_id, heads.c.selector_series_id == selector.selector_series_id,
                        heads.c.current_selector_id == expected_current_selector_id, heads.c.cas_version == expected_cas_version)
                        .values(current_selector_id=selector.selector_id, cas_version=version, updated_at=recorded_at))
                    if changed.rowcount != 1:
                        raise CanonicalTombstoneSelectorError("Tombstone selector head changed.")
                await uow.commit()
                return TombstoneSelectorPublicationResult(selector, version, False)
        except IntegrityError as error:
            raise CanonicalTombstoneSelectorError("Tombstone selector conflicts with immutable history.") from error


__all__ = ["PostgreSQLCanonicalTombstoneSelectorRepository", "TombstoneSelectorPublicationResult"]
