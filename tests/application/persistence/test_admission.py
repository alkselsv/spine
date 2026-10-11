from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from spine.application.admission import AdmissionDecisionService, AdmissionValidationEvidence
from spine.application.persistence.context import EnvironmentScope, PersistenceOperation, PersistencePurpose, WorkspaceScope
from spine.application.persistence.errors import ConstraintConflictError, InvalidPersistenceContextError
from spine.application.persistence.outbox import OutboxEventRegistry
from spine.application.persistence.idempotency import IdempotencyKey
from spine.application.persistence.repositories import SourceObservationCommand
from spine.domain.common import EnvironmentKind
from spine.domain.sources import (
    AdmissionDecision,
    AdmissionDisposition,
    AdmissionReference,
    IdentityMode,
    RevisionKind,
    RevisionMetadata,
    SourceObject,
    SourceRevisionProvenance,
    canonical_revision_metadata_digest,
)
from spine.domain.sources.canonicalization import content_revision, tombstone_revision
from spine.domain.workspaces import Environment, Workspace
from spine.infrastructure.object_storage.contracts import ObjectReference
from spine.infrastructure.persistence.contexts import TrustedContextBoundary, create_initial_workspace_bootstrap_authority
from spine.infrastructure.persistence.in_memory import InMemoryPersistence


NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)


def ident(number: int) -> UUID:
    return uuid5(NAMESPACE_URL, f"admission-test-{number}")


@pytest.fixture
def admission_persistence():
    boundary = TrustedContextBoundary.for_testing(issuer_id=ident(1), secret=b"admission-test-secret-32-bytes-long")
    authority = create_initial_workspace_bootstrap_authority()
    persistence = InMemoryPersistence(
        context_verifier=boundary,
        outbox_events=OutboxEventRegistry(),
        bootstrap_authority=authority,
        transaction_lock=asyncio.Lock(),
    )

    async def prepare() -> None:
        await persistence.initial_workspace_bootstrap.create_initial_workspace(
            authority, Workspace(id=ident(2), slug="admission", display_name="Admission")
        )

    async def create_environment() -> None:
        async with persistence.uow_factory(
            boundary.worker(
                scope=WorkspaceScope(workspace_id=ident(2)),
                service_principal_id=ident(4),
                purpose=PersistencePurpose("test"),
                operation=PersistenceOperation("environment"),
                trace_id=ident(6),
            )
        ) as uow:
            await uow.environments.add(Environment(id=ident(3), workspace_id=ident(2), kind=EnvironmentKind.DEVELOPMENT, display_name="dev"))
            await uow.commit()

    asyncio.run(prepare())
    asyncio.run(create_environment())

    def context() -> object:
        return boundary.worker(
            scope=EnvironmentScope(workspace_id=ident(2), environment_id=ident(3)),
            service_principal_id=ident(4),
            purpose=PersistencePurpose("test"),
            operation=PersistenceOperation("source_observation"),
            trace_id=ident(5),
        )

    return persistence.uow_factory, context


def source() -> SourceObject:
    return SourceObject(
        source_object_id=ident(10), workspace_id=ident(2), environment_id=ident(3),
        source_kind="document", identity_mode=IdentityMode.UPLOAD,
        upload_identity="admission-upload", created_at=NOW,
    )


def revision() -> object:
    metadata = RevisionMetadata(embedded_title="Admission document")
    return content_revision(
        revision_id=ident(11), source_object_id=ident(10), workspace_id=ident(2), environment_id=ident(3),
        kind=RevisionKind.CONTENT, revision_schema_version="source-revision:v1",
        revision_metadata_schema="revision-metadata:r1-document-v1", revision_metadata=metadata,
        revision_metadata_digest=canonical_revision_metadata_digest(metadata),
        original_reference=ObjectReference(schema_version=1, object_id=ident(12), storage_generation=ident(13), digest_algorithm="sha256", digest_hex="a" * 64, byte_length=1),
        original_sha256="a" * 64, byte_length=1, media_type="text/plain", observed_at=NOW,
    )


def provenance(revision: object) -> SourceRevisionProvenance:
    return SourceRevisionProvenance(
        provenance_id=ident(14), source_object_id=revision.source_object_id, revision_id=revision.revision_id,
        workspace_id=revision.workspace_id, environment_id=revision.environment_id,
        producer_kind="connector", producer_reference="provider", event_identity="event-1",
        event_digest="b" * 64, observer_service="observer", received_at=NOW, observed_at=NOW,
    )


def decision(revision: object, *, version: int = 1, predecessor: UUID | None = None, disposition: AdmissionDisposition = AdmissionDisposition.QUARANTINED) -> AdmissionDecision:
    return AdmissionDecision(
        decision_id=ident(20 + version), source_object_id=revision.source_object_id, revision_id=revision.revision_id,
        workspace_id=revision.workspace_id, environment_id=revision.environment_id,
        decision_version=version, predecessor_decision_id=predecessor, disposition=disposition,
        validator_profile_version="admission:r1", reason_schema_version="admission-reasons:r1",
        reason_codes=("missing_provenance",) if disposition is AdmissionDisposition.QUARANTINED else ("validated",),
        authority_reference=AdmissionReference(reference_type="validator", reference_id=ident(30), schema_version=1),
        trace_id=ident(31), audit_event_id=ident(32 + version), decided_at=NOW,
    )


async def observe(uow: object) -> object:
    observed_revision = revision()
    await uow.sources.record_observation(SourceObservationCommand.create(
        source=source(), revision=observed_revision, provenance=provenance(observed_revision),
        idempotency_key=IdempotencyKey("observe-admission"),
    ))
    return observed_revision


@pytest.mark.asyncio
async def test_admission_chain_is_append_only_and_effective(admission_persistence) -> None:
    uow_factory, context = admission_persistence
    async with uow_factory(context()) as uow:
        observed = await observe(uow)
        root = await uow.admissions.append_decision(decision(observed))
        successor = decision(observed, version=2, predecessor=root.decision_id, disposition=AdmissionDisposition.ADMITTED)
        await uow.admissions.append_decision(successor)
        effective = await uow.admissions.resolve_effective(observed.revision_id)
        assert effective is not None
        assert effective.disposition is AdmissionDisposition.ADMITTED
        assert effective.predecessor_decision_id == root.decision_id
        await uow.commit()


@pytest.mark.asyncio
async def test_admission_rejects_two_successors_from_one_predecessor(admission_persistence) -> None:
    uow_factory, context = admission_persistence
    async with uow_factory(context()) as uow:
        observed = await observe(uow)
        root = await uow.admissions.append_decision(decision(observed))
        await uow.commit()
    async with uow_factory(context()) as uow:
        successor = decision(observed, version=2, predecessor=root.decision_id, disposition=AdmissionDisposition.ADMITTED)
        await uow.admissions.append_decision(successor)
        with pytest.raises(ConstraintConflictError):
            await uow.admissions.append_decision(successor.model_copy(update={"decision_id": ident(99), "audit_event_id": ident(100)}))


@pytest.mark.asyncio
async def test_admission_service_quarantines_missing_provenance(admission_persistence) -> None:
    uow_factory, context = admission_persistence
    async with uow_factory(context()) as uow:
        observed = await observe(uow)
        result = await AdmissionDecisionService(uow.admissions, id_factory=lambda: ident(50)).record(
            revision=observed,
            evidence=AdmissionValidationEvidence(
                disposition=AdmissionDisposition.ADMITTED,
                validator_profile_version="admission:r1", reason_schema_version="admission-reasons:r1",
                reason_codes=("validated",), authority_reference=AdmissionReference(reference_type="validator", reference_id=ident(51), schema_version=1),
                trace_id=ident(52), audit_event_id=ident(53), decided_at=NOW,
            ),
        )
        assert result.disposition is AdmissionDisposition.QUARANTINED
        assert result.reason_codes == ("missing_provenance",)


@pytest.mark.asyncio
async def test_admission_rejects_tombstone_as_content_admission(admission_persistence) -> None:
    uow_factory, context = admission_persistence
    tombstone = tombstone_revision(
        revision_id=ident(60),
        source_object_id=ident(10),
        workspace_id=ident(2),
        environment_id=ident(3),
        kind=RevisionKind.TOMBSTONE,
        revision_schema_version="source-revision:v1",
        revision_metadata_schema="revision-metadata:r1-document-v1",
        deletion_reason="source_deleted",
        deletion_provenance="provider-event",
        observed_at=NOW,
    )
    async with uow_factory(context()) as uow:
        await uow.sources.record_observation(SourceObservationCommand.create(
            source=source(), revision=tombstone, provenance=provenance(tombstone),
            idempotency_key=IdempotencyKey("observe-tombstone-admission"),
        ))
        with pytest.raises(ConstraintConflictError, match="Tombstone admission"):
            await uow.admissions.append_decision(decision(tombstone, disposition=AdmissionDisposition.ADMITTED))


@pytest.mark.asyncio
async def test_admission_is_environment_scoped(admission_persistence) -> None:
    uow_factory, context = admission_persistence
    async with uow_factory(context()) as uow:
        observed = await observe(uow)
        wrong_scope = decision(observed).model_copy(update={"environment_id": ident(999)})
        with pytest.raises(InvalidPersistenceContextError):
            await uow.admissions.append_decision(wrong_scope)
