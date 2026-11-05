from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest

from spine.application.canonical_tombstone_selectors import CanonicalTombstoneSelectorError
from spine.application.tombstone_authorization import (
    IssueTombstoneAuthorizationCommand,
    TombstoneAuthorizationOutcome,
    TombstoneAuthorizationService,
)
from spine.application.diagnostics.audit import AuditEventRegistry
from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
)
from spine.application.persistence.errors import CanonicalEligibilityError
from spine.domain.audit import AuditOutcome, CommandAuditPayload
from spine.domain.sources.canonical_tombstone_selector import (
    CanonicalTombstoneWriterMode,
    TrustedCanonicalTombstoneVersionSelector,
)
from spine.infrastructure.persistence.postgresql_canonical_tombstone_selectors import (
    PostgreSQLCanonicalTombstoneSelectorRepository,
)
from spine.infrastructure.persistence.postgresql_canonical_tombstone import (
    PostgreSQLCurrentTombstoneAuthorizationAuthority,
)
from tests.application.test_tombstone_authorization import NOW
from tests.integration.postgresql.test_postgresql_tombstone_authorization import (
    _admitted_case,
)
from tests.integration.postgresql.test_postgresql_unit_of_work import (
    PersistenceAdapter,
    PostgreSQLContractDatabase,
    postgresql_adapter,
    postgresql_contract_database,
    synthetic_uuid,
)

pytestmark = [pytest.mark.postgresql, pytest.mark.asyncio(loop_scope="session")]


async def test_tombstone_selector_publication_is_immutable_and_cas_serialized(
    postgresql_adapter: PersistenceAdapter,
    postgresql_contract_database: PostgreSQLContractDatabase,
) -> None:
    case, ingestion_context, _, authorization_repository, _, approval, _, authorization, _, _ = await _admitted_case(
        postgresql_adapter, postgresql_contract_database, 736200
    )
    boundary = postgresql_adapter.uow_factory._context_verifier  # type: ignore[attr-defined]

    def context_factory(_operation: str, workspace_id: UUID, environment_id: UUID):
        return boundary.interactive(
            scope=EnvironmentScope(
                workspace_id=workspace_id, environment_id=environment_id
            ),
            acting_subject_id=approval.approver_human_id,
            purpose=PersistencePurpose("canonical_tombstone_selector"),
            operation=PersistenceOperation("canonical_tombstone_selector.publish"),
            trace_id=synthetic_uuid(736201),
        )

    context = context_factory(
        "canonical_tombstone_selector.publish",
        case.source.workspace_id,
        case.source.environment_id,
    )
    repository = PostgreSQLCanonicalTombstoneSelectorRepository(
        uow_factory=postgresql_adapter.uow_factory,
        context_factory=context_factory,
    )
    registry = AuditEventRegistry.with_default_families()
    assert case.source.connection_id is not None

    def selector(
        selector_id: UUID,
        version: int,
        predecessor_id: UUID | None,
        audit_id: UUID,
        key: str,
        mode: CanonicalTombstoneWriterMode = CanonicalTombstoneWriterMode.V3_REQUIRED,
    ) -> TrustedCanonicalTombstoneVersionSelector:
        return TrustedCanonicalTombstoneVersionSelector(
            selector_id=selector_id,
            selector_series_id=synthetic_uuid(736202),
            selector_version=version,
            workspace_id=case.source.workspace_id,
            environment_id=case.source.environment_id,
            source_object_id=case.source.source_object_id,
            source_contract_id=approval.source_contract_id,
            source_contract_version=approval.source_contract_version,
            provider_kind=authorization.provider_kind,
            connection_id=case.source.connection_id,
            resource_scope_reference=approval.resource_scope_reference,
            mode=mode,
            approval_reference=approval.approval_id,
            predecessor_selector_id=predecessor_id,
            audit_reference=audit_id,
            idempotency_identity=key,
            valid_from=NOW,
            valid_until=NOW + timedelta(days=1),
            created_at=NOW,
        )

    def audit(audit_id: UUID):
        return registry.build_event(
            audit_event_id=audit_id,
            workspace_id=case.source.workspace_id,
            environment_id=case.source.environment_id,
            event_type="command.outcome",
            schema_version=1,
            payload=CommandAuditPayload(
                command_type="canonical_tombstone_selector.publish"
            ),
            origin=context.origin,
            acting_subject_id=context.acting_subject_id,
            service_principal_id=context.service_principal_id,
            trace_id=context.trace_id,
            occurred_at=NOW,
            outcome=AuditOutcome.ACCEPTED,
            reason="tombstone_selector_published",
        )

    first = selector(
        synthetic_uuid(736203),
        1,
        None,
        synthetic_uuid(736204),
        "tombstone:first",
        CanonicalTombstoneWriterMode.LEGACY_V1_ALLOWED,
    )
    stored = await repository.publish(
        first,
        expected_current_selector_id=None,
        expected_cas_version=None,
        recorded_at=NOW,
        audit_event=audit(first.audit_reference),
    )
    replay = await repository.publish(
        first,
        expected_current_selector_id=None,
        expected_cas_version=None,
        recorded_at=NOW,
        audit_event=audit(first.audit_reference),
    )
    assert stored.replayed is False
    assert replay.replayed is True
    legacy_command = SimpleNamespace(
        source=case.source,
        tombstone_authorization=None,
    )
    async with postgresql_adapter.uow_factory(ingestion_context) as uow:
        await PostgreSQLCurrentTombstoneAuthorizationAuthority(
            clock=lambda: NOW
        ).revalidate(legacy_command, uow)  # type: ignore[arg-type]
        await uow.rollback()
    unrelated_command = SimpleNamespace(
        source=case.source.model_copy(
            update={"source_object_id": synthetic_uuid(736299)}
        ),
        tombstone_authorization=None,
    )
    async with postgresql_adapter.uow_factory(ingestion_context) as uow:
        with pytest.raises(CanonicalEligibilityError):
            await PostgreSQLCurrentTombstoneAuthorizationAuthority(
                clock=lambda: NOW
            ).revalidate(unrelated_command, uow)  # type: ignore[arg-type]
        await uow.rollback()
    issuance = await TombstoneAuthorizationService(
        authorization_repository,
        authorization_repository,
        clock=lambda: NOW,
    ).issue(IssueTombstoneAuthorizationCommand(authorization), ingestion_context)
    assert issuance.outcome is TombstoneAuthorizationOutcome.ISSUED
    second = selector(
        synthetic_uuid(736205),
        2,
        first.selector_id,
        synthetic_uuid(736206),
        "tombstone:second",
    )
    await repository.publish(
        second,
        expected_current_selector_id=first.selector_id,
        expected_cas_version=1,
        recorded_at=NOW,
        audit_event=audit(second.audit_reference),
    )
    async with postgresql_adapter.uow_factory(ingestion_context) as uow:
        with pytest.raises(CanonicalEligibilityError):
            await PostgreSQLCurrentTombstoneAuthorizationAuthority(
                clock=lambda: NOW
            ).revalidate(legacy_command, uow)  # type: ignore[arg-type]
        await uow.rollback()
    left = selector(
        synthetic_uuid(736207),
        3,
        second.selector_id,
        synthetic_uuid(736208),
        "tombstone:left",
    )
    right = selector(
        synthetic_uuid(736209),
        3,
        second.selector_id,
        synthetic_uuid(736210),
        "tombstone:right",
    )
    outcomes = await asyncio.gather(
        repository.publish(
            left,
            expected_current_selector_id=second.selector_id,
            expected_cas_version=2,
            recorded_at=NOW,
            audit_event=audit(left.audit_reference),
        ),
        repository.publish(
            right,
            expected_current_selector_id=second.selector_id,
            expected_cas_version=2,
            recorded_at=NOW,
            audit_event=audit(right.audit_reference),
        ),
        return_exceptions=True,
    )
    assert sum(not isinstance(value, Exception) for value in outcomes) == 1
    assert sum(
        isinstance(value, CanonicalTombstoneSelectorError) for value in outcomes
    ) == 1
