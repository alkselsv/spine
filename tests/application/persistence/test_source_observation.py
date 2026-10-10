from datetime import datetime, timezone
import asyncio
from dataclasses import replace
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
from spine.domain.sources.canonicalization import content_revision, tombstone_revision
from spine.domain.workspaces.models import Environment
from spine.domain.common import EnvironmentKind
from spine.infrastructure.object_storage.contracts import ObjectReference
from spine.infrastructure.persistence.contexts import TrustedContextBoundary, create_initial_workspace_bootstrap_authority
from spine.infrastructure.persistence.in_memory import InMemoryPersistence
from spine.application.persistence.context import EnvironmentScope, PersistenceOperation, PersistencePurpose, WorkspaceScope
from spine.application.persistence.outbox import OutboxEventRegistry
from spine.application.persistence.idempotency import IdempotencyKey
from spine.application.persistence.command_digest import digest_command
from spine.application.persistence.repositories import SourceObservationCommand
from spine.application.persistence.errors import IdempotencyConflictError, ObservationCommandDigestMismatchError, RevisionDigestMismatchError
from spine.domain.sources.errors import RevisionDigestMismatchError as DomainRevisionDigestMismatchError



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
        kind=RevisionKind.CONTENT, revision_digest="0" * 64,
        revision_schema_version="source-revision:v1", revision_metadata_schema="revision-metadata:r1-document-v1",
        revision_metadata=RevisionMetadata(embedded_title="  Title\r\n"), revision_metadata_digest="b" * 64,
        original_reference=ObjectReference(schema_version=1, object_id=synthetic_uuid(102), storage_generation=synthetic_uuid(103), digest_algorithm="sha256", digest_hex="c" * 64, byte_length=4),
        original_sha256="c" * 64, byte_length=4, media_type="text/plain", observed_at=NOW,
    )


def valid_revision(source_object: SourceObject, revision_id: UUID = synthetic_uuid(101)) -> SourceRevision:
    values = revision(source_object, revision_id).model_dump()
    values.pop("revision_digest")
    return content_revision(**values)


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
        first_revision = valid_revision(resolved)
        first_provenance = provenance(resolved, first_revision, 1)
        first = await uow.sources.record_observation(command(resolved, first_revision, first_provenance, "key-1"))
        second_revision = valid_revision(resolved, synthetic_uuid(104))
        second_provenance = provenance(resolved, second_revision, 2)
        second = await uow.sources.record_observation(command(resolved, second_revision, second_provenance, "key-2"))
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


@pytest.mark.parametrize(
    "value",
    ("EN", " en", "en ", "en US", "en_US", "e", "en-QQQ", "en-u-ca-gregory-u-nu-latn"),
)
def test_source_identity_and_language_values_reject_noncanonical_input(value: str) -> None:
    with pytest.raises(ValueError):
        SourceObject(
            source_object_id=synthetic_uuid(1), workspace_id=synthetic_uuid(2), environment_id=synthetic_uuid(3),
            source_kind="Document", identity_mode=IdentityMode.UPLOAD, upload_identity=value, created_at=NOW,
        )


def test_language_profile_normalizes_lowercase_and_accepts_registered_forms() -> None:
    assert RevisionMetadata(document_language="en-US").document_language == "en-us"
    assert RevisionMetadata(document_language="en-US-u-co-phonebk").document_language == "en-us-u-co-phonebk"
    assert RevisionMetadata(document_language="sl-ROZAJ").document_language == "sl-rozaj"
    assert RevisionMetadata(document_language="i-klingon").document_language == "i-klingon"


def test_connector_identity_rejects_zero_connection_id() -> None:
    with pytest.raises(ValueError):
        SourceObject(
            source_object_id=synthetic_uuid(1), workspace_id=synthetic_uuid(2), environment_id=synthetic_uuid(3),
            source_kind="document", identity_mode=IdentityMode.CONNECTOR,
            connection_id=UUID(int=0), external_namespace="provider",
            external_generation="generation-1", external_object_id="object-1", created_at=NOW,
        )


def test_revision_digest_excludes_observed_time() -> None:
    source_object = source()
    left = valid_revision(source_object)
    right = left.model_copy(update={"observed_at": NOW.replace(hour=13)})
    assert revision_digest(left) == revision_digest(right)


def test_revision_digest_matches_pinned_golden_vector() -> None:
    assert revision_digest(valid_revision(source())) == "f4f27b1eadd4743c93f859c3f00106e444677629262d9e137f7c00bc2990afb3"


def command(source_object: SourceObject, source_revision: SourceRevision, source_provenance: SourceRevisionProvenance, key: str) -> SourceObservationCommand:
    return SourceObservationCommand.create(
        source=source_object,
        revision=source_revision,
        provenance=source_provenance,
        idempotency_key=IdempotencyKey(key),
    )


@pytest.mark.asyncio
async def test_observation_replay_uses_opaque_idempotency_result(source_persistence) -> None:
    uow_factory, workspace_context, environment_context = source_persistence
    workspace_id, environment_id = synthetic_uuid(901), synthetic_uuid(910)
    async with uow_factory(workspace_context(workspace_id)) as uow:
        await uow.environments.add(Environment(id=environment_id, workspace_id=workspace_id, kind=EnvironmentKind.DEVELOPMENT, display_name="dev"))
        await uow.commit()
    async with uow_factory(environment_context(workspace_id, environment_id)) as uow:
        source_object = source()
        source_revision = valid_revision(source_object)
        source_provenance = provenance(source_object, source_revision)
        observation = command(source_object, source_revision, source_provenance, "replay-key")
        first = await uow.sources.record_observation(observation)
        second = await uow.sources.record_observation(observation)
        await uow.commit()
    assert first.replay is False
    assert second.replay is True
    assert first.result_reference == second.result_reference
    assert first.result_reference.result_type == "source_revision"


@pytest.mark.asyncio
async def test_observation_replay_after_commit_is_stable(source_persistence) -> None:
    uow_factory, workspace_context, environment_context = source_persistence
    workspace_id, environment_id = synthetic_uuid(901), synthetic_uuid(910)
    async with uow_factory(workspace_context(workspace_id)) as uow:
        await uow.environments.add(Environment(id=environment_id, workspace_id=workspace_id, kind=EnvironmentKind.DEVELOPMENT, display_name="dev"))
        await uow.commit()
    source_object = source()
    source_revision = valid_revision(source_object)
    source_provenance = provenance(source_object, source_revision)
    observation = command(source_object, source_revision, source_provenance, "lost-response-key")
    async with uow_factory(environment_context(workspace_id, environment_id)) as uow:
        first = await uow.sources.record_observation(observation)
        await uow.commit()
    async with uow_factory(environment_context(workspace_id, environment_id)) as uow:
        replay = await uow.sources.record_observation(observation)
        await uow.rollback()
    assert replay.replay is True
    assert replay.result_reference == first.result_reference


@pytest.mark.asyncio
async def test_changed_observation_digest_is_typed_conflict(source_persistence) -> None:
    uow_factory, workspace_context, environment_context = source_persistence
    workspace_id, environment_id = synthetic_uuid(901), synthetic_uuid(910)
    async with uow_factory(workspace_context(workspace_id)) as uow:
        await uow.environments.add(Environment(id=environment_id, workspace_id=workspace_id, kind=EnvironmentKind.DEVELOPMENT, display_name="dev"))
        await uow.commit()
    async with uow_factory(environment_context(workspace_id, environment_id)) as uow:
        source_object = source()
        source_revision = valid_revision(source_object)
        source_provenance = provenance(source_object, source_revision)
        observation = command(source_object, source_revision, source_provenance, "conflict-key")
        await uow.sources.record_observation(observation)
        changed_digest = digest_command(
            operation=PersistenceOperation("source_observation"),
            operation_schema_version=1,
            payload={"changed": True},
        )
        with pytest.raises(IdempotencyConflictError):
            await uow.sources.record_observation(replace(observation, digest=changed_digest))


@pytest.mark.asyncio
async def test_forged_observation_digest_is_rejected_before_persistence(source_persistence) -> None:
    uow_factory, workspace_context, environment_context = source_persistence
    workspace_id, environment_id = synthetic_uuid(901), synthetic_uuid(910)
    async with uow_factory(workspace_context(workspace_id)) as uow:
        await uow.environments.add(Environment(id=environment_id, workspace_id=workspace_id, kind=EnvironmentKind.DEVELOPMENT, display_name="dev"))
        await uow.commit()
    async with uow_factory(environment_context(workspace_id, environment_id)) as uow:
        source_object = source()
        source_revision = valid_revision(source_object)
        source_provenance = provenance(source_object, source_revision)
        valid = command(source_object, source_revision, source_provenance, "forged-key")
        forged = replace(
            valid,
            digest=digest_command(
                operation=PersistenceOperation("source_observation"),
                operation_schema_version=1,
                payload={"unrelated": "payload"},
            ),
        )
        with pytest.raises(ObservationCommandDigestMismatchError):
            await uow.sources.record_observation(forged)


def test_revision_digest_mismatch_is_rejected() -> None:
    source_object = source()
    valid = valid_revision(source_object)
    from spine.domain.sources.canonicalization import assert_revision_digest
    for field, value in (
        ("original_sha256", "d" * 64),
        ("byte_length", 5),
        ("media_type", "application/json"),
        ("revision_metadata", RevisionMetadata(embedded_title="Changed")),
    ):
        with pytest.raises(DomainRevisionDigestMismatchError):
            assert_revision_digest(valid.model_copy(update={field: value}))


def test_tombstone_reason_and_forged_digest_are_rejected() -> None:
    values = revision(source()).model_dump()
    values.update(
        {
            "kind": RevisionKind.TOMBSTONE,
            "revision_id": synthetic_uuid(105),
            "original_reference": None,
            "original_sha256": None,
            "byte_length": None,
            "media_type": None,
            "revision_metadata": None,
            "revision_metadata_digest": None,
            "deletion_reason": "source_deleted",
            "deletion_provenance": "provider-event",
            "reappearance_after_tombstone_revision_id": None,
        }
    )
    tombstone = tombstone_revision(**{key: value for key, value in values.items() if key != "revision_digest"})
    from spine.domain.sources.canonicalization import assert_revision_digest
    with pytest.raises(DomainRevisionDigestMismatchError):
        assert_revision_digest(tombstone.model_copy(update={"deletion_reason": "provider_deleted"}))
    with pytest.raises(TypeError):
        tombstone_revision(**values)


def test_arbitrary_original_reference_is_rejected() -> None:
    with pytest.raises(ValueError):
        SourceRevision(
            **{
                **revision(source()).model_dump(),
                "original_reference": "https://example.invalid/object",
            }
        )
