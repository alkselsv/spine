from datetime import datetime, timezone
import asyncio
from uuid import UUID, NAMESPACE_URL, uuid5

import pytest
from pydantic import BaseModel, ConfigDict

from spine.application.diagnostics.audit import AuditEventRegistry
from spine.application.persistence.canonical import CanonicalAcceptanceCommand
from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
    WorkspaceScope,
)
from spine.application.persistence.idempotency import IdempotencyKey
from spine.application.persistence.outbox import OpaqueObjectReference, OutboxEventRegistry
from spine.domain.audit import (
    AuditObjectReference,
    AuditOutcome,
    CanonicalTransitionAuditPayload,
)
from spine.domain.common import ContextOrigin, EnvironmentKind
from spine.domain.sources import AdmissionDecision, AdmissionDisposition
from spine.domain.workspaces import Environment, Workspace
from spine.infrastructure.persistence.contexts import (
    TrustedContextBoundary,
    create_initial_workspace_bootstrap_authority,
)
from spine.infrastructure.persistence.in_memory import InMemoryPersistence
from tests.application.persistence.test_source_observation import (
    NOW,
    provenance,
    source,
    synthetic_uuid,
    valid_revision,
)
from tests.application.persistence.test_source_observation import command as observation_command


def uid(value: int) -> UUID:
    return uuid5(NAMESPACE_URL, f"canonical-test-{value}")


class SourceAcceptedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_revision_id: UUID
    accepted_generation: int
    transition: str


@pytest.fixture
def canonical_persistence():
    boundary = TrustedContextBoundary.for_testing(
        issuer_id=uid(900), secret=b"canonical-acceptance-test-secret-32-bytes"
    )
    authority = create_initial_workspace_bootstrap_authority()
    events = OutboxEventRegistry()
    events.register(
        event_type="source.accepted",
        schema_version=1,
        payload_type=SourceAcceptedPayload,
    )
    persistence = InMemoryPersistence(
        context_verifier=boundary,
        outbox_events=events,
        audit_events=AuditEventRegistry.with_default_families(),
        bootstrap_authority=authority,
        transaction_lock=asyncio.Lock(),
    )

    async def initialize() -> None:
        await persistence.initial_workspace_bootstrap.create_initial_workspace(
            authority,
            Workspace(id=synthetic_uuid(901), slug="canonical", display_name="Canonical"),
        )

    asyncio.run(initialize())

    def workspace_context() -> object:
        return boundary.worker(
            scope=WorkspaceScope(workspace_id=synthetic_uuid(901)),
            service_principal_id=uid(902),
            purpose=PersistencePurpose("canonical_test"),
            operation=PersistenceOperation("source_observation"),
            trace_id=uid(903),
        )

    def source_context() -> object:
        return boundary.worker(
            scope=EnvironmentScope(workspace_id=synthetic_uuid(901), environment_id=synthetic_uuid(910)),
            service_principal_id=uid(902),
            purpose=PersistencePurpose("canonical_test"),
            operation=PersistenceOperation("source_observation"),
            trace_id=uid(904),
        )

    def canonical_context() -> object:
        return boundary.worker(
            scope=EnvironmentScope(workspace_id=synthetic_uuid(901), environment_id=synthetic_uuid(910)),
            service_principal_id=uid(902),
            purpose=PersistencePurpose("canonical_test"),
            operation=PersistenceOperation("canonical_acceptance"),
            trace_id=uid(904),
        )

    return persistence, workspace_context, source_context, canonical_context


async def add_environment(persistence, workspace_context) -> None:
    async with persistence.uow_factory(workspace_context()) as uow:
        await uow.environments.add(
            Environment(
                id=synthetic_uuid(910),
                workspace_id=synthetic_uuid(901),
                kind=EnvironmentKind.DEVELOPMENT,
                display_name="dev",
            )
        )
        await uow.commit()


def acceptance_command(source_object, revision, provenance_value, *, key, generation, expected=None, tombstone=False):
    audit_id = uid(700 + generation)
    outbox_id = uid(800 + generation)
    audit = AuditEventRegistry.with_default_families().build_event(
        workspace_id=source_object.workspace_id,
        environment_id=source_object.environment_id,
        event_type="canonical.transition",
        schema_version=1,
        payload=CanonicalTransitionAuditPayload(transition="source.accepted"),
        origin=ContextOrigin.WORKER,
        acting_subject_id=None,
        service_principal_id=uid(902),
        trace_id=uid(904),
        occurred_at=NOW,
        outcome=AuditOutcome.COMMITTED,
        reason="source_tombstoned" if tombstone else "source_accepted",
        target=AuditObjectReference(object_type="source_revision", object_id=revision.revision_id, schema_version=1),
        producer_deduplication_id=f"canonical:{revision.revision_id}",
        audit_event_id=audit_id,
    )
    events = OutboxEventRegistry()
    events.register(event_type="source.accepted", schema_version=1, payload_type=SourceAcceptedPayload)
    outbox = events.build_intent(
        workspace_id=source_object.workspace_id,
        environment_id=source_object.environment_id,
        event_type="source.accepted",
        schema_version=1,
        payload=SourceAcceptedPayload(source_revision_id=revision.revision_id, accepted_generation=generation, transition="accepted"),
        trace_id=uid(904),
        aggregate=OpaqueObjectReference(object_type="source_object", object_id=source_object.source_object_id, schema_version=1),
        producer_deduplication_id=f"canonical:{revision.revision_id}",
        event_id=outbox_id,
    )
    return CanonicalAcceptanceCommand.create(
        source=source_object,
        revision=revision,
        provenance=provenance_value,
        expected_current_revision_id=expected,
        idempotency_key=IdempotencyKey(key),
        canonical_acceptance_id=uid(600 + generation),
        admission_decision_id=uid(500 + generation) if not tombstone else None,
        access_policy_version_id=uid(400 + generation) if not tombstone else None,
        audit_event_id=audit_id,
        outbox_event_id=outbox_id,
        accepted_at=NOW,
        audit_event=audit,
        outbox_intent=outbox,
    )


@pytest.mark.asyncio
async def test_canonical_acceptance_advances_head_and_replays_without_second_effect(canonical_persistence):
    persistence, workspace_context, source_context, canonical_context = canonical_persistence
    await add_environment(persistence, workspace_context)
    source_object = source()
    revision = valid_revision(source_object)
    source_provenance = provenance(source_object, revision)
    async with persistence.uow_factory(source_context()) as uow:
        resolved = await uow.sources.resolve_or_create_source(source_object)
        await uow.sources.record_observation(
            observation_command(resolved, revision, source_provenance, "observe")
        )
        await uow.commit()
    async with persistence.uow_factory(canonical_context()) as uow:
        resolved = await uow.sources.resolve_or_create_source(source_object)
        await uow.admissions.append_decision(
            AdmissionDecision(
                decision_id=uid(501),
                source_object_id=resolved.source_object_id,
                revision_id=revision.revision_id,
                workspace_id=resolved.workspace_id,
                environment_id=resolved.environment_id,
                decision_version=1,
                disposition=AdmissionDisposition.ADMITTED,
                validator_profile_version="validator.v1",
                reason_schema_version="reason.v1",
                reason_codes=("clean",),
                authority_reference={"reference_type": "validator", "reference_id": uid(502), "schema_version": 1},
                trace_id=uid(904),
                audit_event_id=uid(503),
                decided_at=NOW,
            )
        )
        first = await uow.canonical.accept(
            acceptance_command(resolved, revision, source_provenance, key="accept-1", generation=1)
        )
        replay = await uow.canonical.accept(
            acceptance_command(resolved, revision, source_provenance, key="accept-1", generation=1)
        )
        await uow.commit()
    assert first.advanced is True
    assert replay.replay is True
    assert replay.acceptance == first.acceptance
    assert len(persistence.uow_factory._store.state.canonical_acceptances) == 1
    assert len(persistence.uow_factory._store.state.current_source_heads) == 1
