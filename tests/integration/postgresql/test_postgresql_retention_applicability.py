from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import insert, select, update

from spine.application.persistence.errors import UnexpectedPersistenceError
from spine.application.retention import (
    RetentionApplicabilityConflictError,
    RetentionApplicabilityError,
    RetentionSourceLineage,
)
from spine.domain.sources.retention import (
    RetentionApplicability,
    RetentionArtifactClass,
    RetentionAuthorityKind,
    RetentionTargetKind,
)
from spine.infrastructure.persistence.postgresql_mappings import (
    audit_events,
    current_retention_applicabilities,
    outbox_intents,
    retention_applicabilities,
)
from spine.infrastructure.persistence.postgresql_retention import (
    PostgreSQLRetentionApplicabilityRepository,
)
from tests.integration.postgresql.test_postgresql_unit_of_work import (
    PersistenceAdapter,
    postgresql_adapter,
    postgresql_contract_database,
    prepare_canonical_case,
    synthetic_uuid,
)
from tests.contracts.persistence.test_source_observation_contract import connector_source


pytestmark = [pytest.mark.postgresql, pytest.mark.asyncio(loop_scope="session")]
NOW = datetime(2026, 10, 31, tzinfo=timezone.utc)


class _Authority:
    async def verify(self, applicability, context, transaction) -> None:
        assert transaction is not None
        assert applicability.actor_id == context.service_principal_id

    async def is_current(self, applicability, context) -> bool:
        return applicability.actor_id == context.service_principal_id


class _Effects:
    async def persist(self, applicability, context, transaction):
        await transaction.execute_statement(
            insert(audit_events).values(
                audit_event_id=applicability.audit_event_id,
                workspace_id=applicability.workspace_id,
                environment_id=applicability.environment_id,
                event_type="source.retention.applicability",
                schema_version=1,
                origin=context.origin.value,
                acting_subject_id=context.acting_subject_id,
                service_principal_id=context.service_principal_id,
                trace_id=context.trace_id,
                correlation_id=None,
                causation_id=None,
                occurred_at=applicability.approved_at,
                appended_at=applicability.approved_at,
                target_type="source_retention",
                target_id=applicability.target_reference,
                target_schema_version=1,
                outcome="committed",
                reason="retention_applicability_bound",
                producer_deduplication_id=f"retention-{applicability.applicability_id}",
                payload={"generation": applicability.generation},
            )
        )
        await transaction.execute_statement(
            insert(outbox_intents).values(
                event_id=applicability.outbox_event_id,
                workspace_id=applicability.workspace_id,
                environment_id=applicability.environment_id,
                event_type="source.retention.applicability",
                event_schema_version=1,
                aggregate_type="source_retention",
                aggregate_id=applicability.target_reference,
                aggregate_schema_version=1,
                producer_deduplication_id=f"retention-{applicability.applicability_id}",
                payload={"generation": applicability.generation},
                trace_id=context.trace_id,
                correlation_id=None,
                causation_id=None,
                created_at=applicability.approved_at,
            )
        )
        return applicability.audit_event_id, applicability.outbox_event_id


def _applicability(case, context, *, identifier: int = 734100):
    return RetentionApplicability(
        applicability_id=synthetic_uuid(identifier),
        workspace_id=case.source.workspace_id,
        environment_id=case.source.environment_id,
        target_kind=RetentionTargetKind.EXACT_SOURCE,
        source_object_id=case.source.source_object_id,
        artifact_classes=(RetentionArtifactClass.ORIGINAL, RetentionArtifactClass.PARSED_CONTENT),
        retention_policy_id=synthetic_uuid(identifier + 1),
        retention_policy_version_id=synthetic_uuid(identifier + 2),
        retention_policy_digest="a" * 64,
        applicability_evidence_reference=synthetic_uuid(identifier + 3),
        applicability_evidence_digest="b" * 64,
        approving_authority_reference=synthetic_uuid(identifier + 4),
        approving_authority_version_id=synthetic_uuid(identifier + 5),
        approving_authority_digest="c" * 64,
        approving_authority_kind=RetentionAuthorityKind.DATA_PORTAL,
        effective_from=NOW,
        effective_until=NOW + timedelta(days=365),
        exception_references=(),
        legal_hold_references=(synthetic_uuid(identifier + 6),),
        expected_current_applicability_id=None,
        expected_current_generation=0,
        generation=1,
        actor_id=context.service_principal_id,
        audit_event_id=synthetic_uuid(identifier + 7),
        outbox_event_id=synthetic_uuid(identifier + 8),
        approved_at=NOW,
    )


async def _case(adapter: PersistenceAdapter, number: int):
    workspace_id = synthetic_uuid(number)
    environment_id = synthetic_uuid(number + 1)
    source = connector_source(workspace_id, environment_id, "provider1")
    return await prepare_canonical_case(adapter, number, source_object=source)


def _repository(adapter: PersistenceAdapter):
    return PostgreSQLRetentionApplicabilityRepository(
        uow_factory=adapter.uow_factory,
        authority=_Authority(),
        effects=_Effects(),
    )


async def test_postgresql_retention_applicability_persists_immutable_current_binding(
    postgresql_adapter: PersistenceAdapter,
) -> None:
    case = await _case(postgresql_adapter, 734000)
    context = postgresql_adapter.ingestion_context(
        case.source.workspace_id, case.source.environment_id, "retention_applicability"
    )
    repository = _repository(postgresql_adapter)
    applicability = _applicability(case, context)

    foreign_source = applicability.model_copy(
        update={
            "applicability_id": synthetic_uuid(734090),
            "source_object_id": synthetic_uuid(734091),
            "audit_event_id": synthetic_uuid(734092),
            "outbox_event_id": synthetic_uuid(734093),
        }
    )
    with pytest.raises(RetentionApplicabilityError):
        await repository.record_applicability(foreign_source, context)
    async with postgresql_adapter.uow_factory(context) as uow:
        rolled_back_audit = await uow.execute_statement(
            select(audit_events.c.audit_event_id).where(
                audit_events.c.audit_event_id == foreign_source.audit_event_id
            )
        )
        assert rolled_back_audit.scalar_one_or_none() is None
        await uow.rollback()

    assert await repository.record_applicability(applicability, context) == applicability
    candidates = await repository.list_current_applicabilities(
        RetentionSourceLineage(
            source_object_id=case.source.source_object_id,
            source_revision_id=case.revision.revision_id,
            source_class=case.source.source_kind,
        ),
        context,
    )

    assert candidates == (applicability,)
    async with postgresql_adapter.uow_factory(context) as uow:
        pointer = await uow.execute_statement(
            select(current_retention_applicabilities.c.current_applicability_id)
        )
        assert pointer.scalar_one() == applicability.applicability_id
        with pytest.raises(UnexpectedPersistenceError):
            await uow.execute_statement(
                update(retention_applicabilities)
                .where(
                    retention_applicabilities.c.applicability_id
                    == applicability.applicability_id
                )
                .values(retention_policy_digest="d" * 64)
            )


async def test_postgresql_retention_reader_enforces_revision_lineage_and_tenant_rls(
    postgresql_adapter: PersistenceAdapter,
) -> None:
    case = await _case(postgresql_adapter, 734200)
    context = postgresql_adapter.ingestion_context(
        case.source.workspace_id, case.source.environment_id, "retention_applicability"
    )
    applicability = _applicability(case, context, identifier=734300)
    repository = _repository(postgresql_adapter)
    await repository.record_applicability(applicability, context)

    wrong_revision = await repository.list_current_applicabilities(
        RetentionSourceLineage(
            source_object_id=case.source.source_object_id,
            source_revision_id=synthetic_uuid(999999),
            source_class=case.source.source_kind,
        ),
        context,
    )
    foreign = await _case(postgresql_adapter, 734400)
    foreign_context = postgresql_adapter.ingestion_context(
        foreign.source.workspace_id,
        foreign.source.environment_id,
        "retention_applicability",
    )
    async with postgresql_adapter.uow_factory(foreign_context) as uow:
        hidden = await uow.execute_statement(
            select(retention_applicabilities.c.applicability_id).where(
                retention_applicabilities.c.applicability_id
                == applicability.applicability_id
            )
        )
        assert hidden.scalar_one_or_none() is None
        await uow.rollback()

    assert wrong_revision == ()


async def test_postgresql_retention_applicability_cas_allows_one_current_winner(
    postgresql_adapter: PersistenceAdapter,
) -> None:
    case = await _case(postgresql_adapter, 734500)
    context = postgresql_adapter.ingestion_context(
        case.source.workspace_id, case.source.environment_id, "retention_applicability"
    )
    repository = _repository(postgresql_adapter)
    first = _applicability(case, context, identifier=734600)
    second = _applicability(case, context, identifier=734700)

    results = await asyncio.gather(
        repository.record_applicability(first, context),
        repository.record_applicability(second, context),
        return_exceptions=True,
    )

    assert sum(isinstance(item, RetentionApplicability) for item in results) == 1
    assert sum(
        isinstance(item, RetentionApplicabilityConflictError) for item in results
    ) == 1
