from datetime import datetime, timezone
import asyncio
from contextlib import asynccontextmanager
from uuid import UUID, NAMESPACE_URL, uuid5

import pytest

from spine.domain.sources import (
    IdentityMode,
    RevisionKind,
    RevisionMetadata,
    SourceObject,
    SourceRevision,
    SourceRevisionProvenance,
)
from spine.domain.sources.canonicalization import revision_digest
from spine.domain.workspaces.models import Environment
from spine.domain.common import EnvironmentKind
from spine.infrastructure.object_storage.contracts import ObjectReference
from spine.infrastructure.persistence.contexts import TrustedContextBoundary, create_initial_workspace_bootstrap_authority
from spine.infrastructure.persistence.in_memory import InMemoryPersistence
from spine.application.persistence.context import EnvironmentScope, PersistenceOperation, PersistencePurpose, WorkspaceScope
from spine.application.persistence.outbox import OutboxEventRegistry



def synthetic_uuid(number: int) -> UUID:
    return uuid5(NAMESPACE_URL, f"spine-test-{number}")


NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)


def source() -> SourceObject:
    return SourceObject(
        source_object_id=synthetic_uuid(100), workspace_id=synthetic_uuid(901),
        environment_id=synthetic_uuid(910), source_kind="document",
        identity_mode=IdentityMode.UPLOAD, upload_identity="upload-100", created_at=NOW,
    )


def revision(source_object: SourceObject, revision_id: UUID = synthetic_uuid(101)) -> SourceRevision:
    return SourceRevision(
        revision_id=revision_id, source_object_id=source_object.source_object_id,
        workspace_id=source_object.workspace_id, environment_id=source_object.environment_id,
        kind=RevisionKind.CONTENT, revision_digest="a" * 64,
        revision_schema_version="source-revision:v1", revision_metadata_schema="revision-metadata:r1-document-v1",
        revision_metadata=RevisionMetadata(embedded_title="  Title\r\n"), revision_metadata_digest="b" * 64,
        original_reference=ObjectReference(schema_version=1, object_id=synthetic_uuid(102), storage_generation=synthetic_uuid(103), digest_algorithm="sha256", digest_hex="c" * 64, byte_length=4),
        original_sha256="c" * 64, byte_length=4, media_type="text/plain", observed_at=NOW,
    )


def provenance(source_object: SourceObject, source_revision: SourceRevision, number: int = 1) -> SourceRevisionProvenance:
    return SourceRevisionProvenance(
        provenance_id=synthetic_uuid(200 + number), source_object_id=source_object.source_object_id,
        revision_id=source_revision.revision_id, workspace_id=source_object.workspace_id,
        environment_id=source_object.environment_id, producer_kind="connector", producer_reference="conn-1",
        event_identity=f"event-{number}", event_digest="d" * 64, observer_service="observer", received_at=NOW, observed_at=NOW,
    )


@pytest.fixture
def source_persistence():
    boundary = TrustedContextBoundary.for_testing(issuer_id=synthetic_uuid(900), secret=b"source-observation-test-secret-32-bytes")
    authority = create_initial_workspace_bootstrap_authority()
    persistence = InMemoryPersistence(context_verifier=boundary, outbox_events=OutboxEventRegistry(), bootstrap_authority=authority, transaction_lock=asyncio.Lock())
    async def initialize():
        from spine.domain.workspaces.models import Workspace
        await persistence.initial_workspace_bootstrap.create_initial_workspace(authority, Workspace(id=synthetic_uuid(901), slug="source", display_name="Source"))
    asyncio.run(initialize())
    def workspace_context(workspace_id):
        return boundary.worker(scope=WorkspaceScope(workspace_id=workspace_id), service_principal_id=synthetic_uuid(906), purpose=PersistencePurpose("test"), operation=PersistenceOperation("source_observation"), trace_id=synthetic_uuid(907))
    def environment_context(workspace_id, environment_id):
        return boundary.worker(scope=EnvironmentScope(workspace_id=workspace_id, environment_id=environment_id), service_principal_id=synthetic_uuid(906), purpose=PersistencePurpose("test"), operation=PersistenceOperation("source_observation"), trace_id=synthetic_uuid(908))
    return persistence.uow_factory, workspace_context, environment_context


@pytest.mark.asyncio
async def test_source_observation_reuses_revision_and_appends_provenance(source_persistence) -> None:
    uow_factory, workspace_context, environment_context = source_persistence
    workspace_id = synthetic_uuid(901)
    environment_id = synthetic_uuid(910)
    async with uow_factory(workspace_context(workspace_id)) as uow:
        await uow.environments.add(Environment(id=environment_id, workspace_id=workspace_id, kind=EnvironmentKind.DEVELOPMENT, display_name="dev"))
        await uow.commit()
    async with uow_factory(environment_context(workspace_id, environment_id)) as uow:
        resolved = await uow.sources.resolve_or_create_source(source())
        first = await uow.sources.record_observation(revision(resolved), provenance(resolved, revision(resolved), 1))
        second = await uow.sources.record_observation(revision(resolved, synthetic_uuid(104)), provenance(resolved, revision(resolved, synthetic_uuid(104)), 2))
        await uow.commit()
    assert first.replay is False
    assert second.replay is False
    assert first.revision.revision_id == second.revision.revision_id
    assert first.provenance.provenance_id != second.provenance.provenance_id


def test_source_identity_modes_reject_mixed_fields() -> None:
    with pytest.raises(ValueError):
        SourceObject(
            source_object_id=synthetic_uuid(1), workspace_id=synthetic_uuid(2), environment_id=synthetic_uuid(3),
            source_kind="document", identity_mode=IdentityMode.UPLOAD, upload_identity="upload",
            connection_id=synthetic_uuid(4), created_at=NOW,
        )


def test_revision_digest_excludes_observed_time() -> None:
    source_object = source()
    left = revision(source_object)
    right = left.model_copy(update={"observed_at": NOW.replace(hour=13)})
    assert revision_digest(left) == revision_digest(right)


def test_revision_digest_matches_pinned_golden_vector() -> None:
    assert revision_digest(revision(source())) == "9bd5158728503e75aee153418376f2dd33d37587be4d7bcb47939249a160333e"
