from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest

from spine.application.persistence.errors import IdempotencyConflictError
from spine.application.persistence.idempotency import IdempotencyKey
from spine.application.persistence.repositories import SourceObservationCommand
from spine.domain.common import EnvironmentKind
from spine.domain.sources import IdentityMode, RevisionKind, RevisionMetadata, SourceObject, SourceRevisionProvenance, canonical_revision_metadata_digest
from spine.domain.sources.canonicalization import content_revision, tombstone_revision
from spine.domain.workspaces import Environment, Workspace
from spine.infrastructure.object_storage.contracts import ObjectReference

from .adapter import PersistenceAdapter
from .ids import synthetic_uuid


NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)


def source(workspace_id: UUID, environment_id: UUID) -> SourceObject:
    return SourceObject(
        source_object_id=synthetic_uuid(7001),
        workspace_id=workspace_id,
        environment_id=environment_id,
        source_kind="document",
        identity_mode=IdentityMode.UPLOAD,
        upload_identity="contract-upload-1",
        created_at=NOW,
    )


def connector_source(workspace_id: UUID, environment_id: UUID, generation: str) -> SourceObject:
    return SourceObject(
        source_object_id=synthetic_uuid(7200 + int(generation[-1])),
        workspace_id=workspace_id,
        environment_id=environment_id,
        source_kind="document",
        identity_mode=IdentityMode.CONNECTOR,
        connection_id=synthetic_uuid(7201),
        external_namespace="contract-provider",
        external_generation=generation,
        external_object_id="object-1",
        created_at=NOW,
    )


def revision(source_object: SourceObject, revision_id: UUID) -> object:
    values = {
        "revision_id": revision_id,
        "source_object_id": source_object.source_object_id,
        "workspace_id": source_object.workspace_id,
        "environment_id": source_object.environment_id,
        "kind": RevisionKind.CONTENT,
        "revision_schema_version": "source-revision:v1",
        "revision_metadata_schema": "revision-metadata:r1-document-v1",
        "revision_metadata": RevisionMetadata(embedded_title="Contract title"),
        "revision_metadata_digest": canonical_revision_metadata_digest(RevisionMetadata(embedded_title="Contract title")),
        "original_reference": ObjectReference(
            schema_version=1,
            object_id=synthetic_uuid(7002),
            storage_generation=synthetic_uuid(7003),
            digest_algorithm="sha256",
            digest_hex="c" * 64,
            byte_length=4,
        ),
        "original_sha256": "c" * 64,
        "byte_length": 4,
        "media_type": "text/plain",
        "observed_at": NOW,
    }
    return content_revision(**values)


def provenance(source_object: SourceObject, revision_id: UUID, event: str) -> SourceRevisionProvenance:
    return SourceRevisionProvenance(
        provenance_id=synthetic_uuid(7100 + int(event[-1])),
        source_object_id=source_object.source_object_id,
        revision_id=revision_id,
        workspace_id=source_object.workspace_id,
        environment_id=source_object.environment_id,
        producer_kind="connector",
        producer_reference="contract-connector",
        event_identity=event,
        event_digest="d" * 64,
        observer_service="contract-observer",
        received_at=NOW,
        observed_at=NOW,
    )


def tombstone(source_object: SourceObject, revision_id: UUID):
    return tombstone_revision(
        revision_id=revision_id,
        source_object_id=source_object.source_object_id,
        workspace_id=source_object.workspace_id,
        environment_id=source_object.environment_id,
        kind=RevisionKind.TOMBSTONE,
        revision_schema_version="source-revision:v1",
        revision_metadata_schema="revision-metadata:r1-document-v1",
        deletion_reason="source_deleted",
        deletion_provenance="contract-delete",
        observed_at=NOW,
    )


async def prepare(adapter: PersistenceAdapter, workspace_id: UUID, environment_id: UUID) -> None:
    async with adapter.uow_factory(adapter.workspace_context(workspace_id)) as uow:
        await uow.workspaces.add(
            Workspace(
                id=workspace_id,
                slug=f"contract-{workspace_id.hex[:12]}",
                display_name="Contract workspace",
            )
        )
        await uow.environments.add(Environment(id=environment_id, workspace_id=workspace_id, kind=EnvironmentKind.DEVELOPMENT, display_name="Contract"))
        await uow.commit()


@pytest.mark.asyncio
async def test_source_observation_replay_is_stable(persistence_adapter: PersistenceAdapter) -> None:
    assert persistence_adapter.source_context is not None
    workspace_id, environment_id = synthetic_uuid(7004), synthetic_uuid(7005)
    await prepare(persistence_adapter, workspace_id, environment_id)
    source_object = source(workspace_id, environment_id)
    source_revision = revision(source_object, synthetic_uuid(7006))
    source_provenance = provenance(source_object, source_revision.revision_id, "event-1")
    command = SourceObservationCommand.create(
        source=source_object,
        revision=source_revision,
        provenance=source_provenance,
        idempotency_key=IdempotencyKey("source-contract-key"),
    )
    async with persistence_adapter.uow_factory(persistence_adapter.source_context(workspace_id, environment_id)) as uow:
        first = await uow.sources.record_observation(command)
        await uow.commit()
    async with persistence_adapter.uow_factory(persistence_adapter.source_context(workspace_id, environment_id)) as uow:
        replay = await uow.sources.record_observation(command)
        await uow.rollback()
    assert first.result_reference == replay.result_reference
    assert replay.replay is True


@pytest.mark.asyncio
async def test_source_observation_reuses_revision_for_new_provenance(persistence_adapter: PersistenceAdapter) -> None:
    assert persistence_adapter.source_context is not None
    workspace_id, environment_id = synthetic_uuid(7010), synthetic_uuid(7011)
    await prepare(persistence_adapter, workspace_id, environment_id)
    source_object = source(workspace_id, environment_id)
    source_revision = revision(source_object, synthetic_uuid(7012))
    first_provenance = provenance(source_object, source_revision.revision_id, "event-1")
    second_provenance = provenance(source_object, source_revision.revision_id, "event-2")
    first = SourceObservationCommand.create(source=source_object, revision=source_revision, provenance=first_provenance, idempotency_key=IdempotencyKey("source-contract-key-1"))
    second = SourceObservationCommand.create(source=source_object, revision=source_revision, provenance=second_provenance, idempotency_key=IdempotencyKey("source-contract-key-2"))
    async with persistence_adapter.uow_factory(persistence_adapter.source_context(workspace_id, environment_id)) as uow:
        first_result = await uow.sources.record_observation(first)
        second_result = await uow.sources.record_observation(second)
        await uow.commit()
    assert first_result.revision.revision_id == second_result.revision.revision_id
    assert first_result.provenance.provenance_id != second_result.provenance.provenance_id


@pytest.mark.asyncio
async def test_same_observation_key_with_changed_digest_conflicts(persistence_adapter: PersistenceAdapter) -> None:
    assert persistence_adapter.source_context is not None
    workspace_id, environment_id = synthetic_uuid(7020), synthetic_uuid(7021)
    await prepare(persistence_adapter, workspace_id, environment_id)
    source_object = source(workspace_id, environment_id)
    source_revision = revision(source_object, synthetic_uuid(7022))
    source_provenance = provenance(source_object, source_revision.revision_id, "event-1")
    command = SourceObservationCommand.create(source=source_object, revision=source_revision, provenance=source_provenance, idempotency_key=IdempotencyKey("source-contract-key-conflict"))
    changed_provenance = provenance(source_object, source_revision.revision_id, "event-2")
    forged = SourceObservationCommand.create(
        source=source_object,
        revision=source_revision,
        provenance=changed_provenance,
        idempotency_key=command.idempotency_key,
    )
    async with persistence_adapter.uow_factory(persistence_adapter.source_context(workspace_id, environment_id)) as uow:
        await uow.sources.record_observation(command)
        with pytest.raises(IdempotencyConflictError):
            await uow.sources.record_observation(forged)


@pytest.mark.asyncio
async def test_source_identity_modes_remain_distinct(persistence_adapter: PersistenceAdapter) -> None:
    workspace_id, environment_id = synthetic_uuid(7030), synthetic_uuid(7031)
    await prepare(persistence_adapter, workspace_id, environment_id)
    upload_a = source(workspace_id, environment_id)
    upload_b = upload_a.model_copy(update={"source_object_id": synthetic_uuid(7032), "upload_identity": "contract-upload-2"})
    connector_a = connector_source(workspace_id, environment_id, "generation-1")
    connector_b = connector_source(workspace_id, environment_id, "generation-2")
    async with persistence_adapter.uow_factory(persistence_adapter.source_context(workspace_id, environment_id)) as uow:
        resolved_upload_a = await uow.sources.resolve_or_create_source(upload_a)
        resolved_upload_replay = await uow.sources.resolve_or_create_source(upload_a.model_copy(update={"source_object_id": synthetic_uuid(7033)}))
        resolved_upload_b = await uow.sources.resolve_or_create_source(upload_b)
        resolved_connector_a = await uow.sources.resolve_or_create_source(connector_a)
        resolved_connector_b = await uow.sources.resolve_or_create_source(connector_b)
        await uow.commit()
    assert resolved_upload_a.source_object_id == resolved_upload_replay.source_object_id
    assert resolved_upload_a.source_object_id != resolved_upload_b.source_object_id
    assert resolved_connector_a.source_object_id != resolved_connector_b.source_object_id


@pytest.mark.asyncio
async def test_tombstone_reappearance_requires_tombstone_predecessor(persistence_adapter: PersistenceAdapter) -> None:
    workspace_id, environment_id = synthetic_uuid(7040), synthetic_uuid(7041)
    await prepare(persistence_adapter, workspace_id, environment_id)
    source_object = source(workspace_id, environment_id)
    deletion = tombstone(source_object, synthetic_uuid(7042))
    deletion_provenance = provenance(source_object, deletion.revision_id, "event-1")
    reappearance_values = revision(source_object, synthetic_uuid(7043)).model_dump()
    reappearance_values.pop("revision_digest")
    reappearance_values["reappearance_after_tombstone_revision_id"] = deletion.revision_id
    reappearance = content_revision(**reappearance_values)
    reappearance_provenance = provenance(source_object, reappearance.revision_id, "event-2")
    async with persistence_adapter.uow_factory(persistence_adapter.source_context(workspace_id, environment_id)) as uow:
        await uow.sources.record_observation(SourceObservationCommand.create(
            source=source_object, revision=deletion, provenance=deletion_provenance,
            idempotency_key=IdempotencyKey("tombstone-key"),
        ))
        result = await uow.sources.record_observation(SourceObservationCommand.create(
            source=source_object, revision=reappearance, provenance=reappearance_provenance,
            idempotency_key=IdempotencyKey("reappearance-key"),
        ))
        await uow.commit()
    assert result.revision.revision_id == reappearance.revision_id


@pytest.mark.asyncio
async def test_observation_rollback_removes_source_revision_and_provenance(persistence_adapter: PersistenceAdapter) -> None:
    workspace_id, environment_id = synthetic_uuid(7050), synthetic_uuid(7051)
    await prepare(persistence_adapter, workspace_id, environment_id)
    source_object = source(workspace_id, environment_id)
    source_revision = revision(source_object, synthetic_uuid(7052))
    source_provenance = provenance(source_object, source_revision.revision_id, "event-1")
    async with persistence_adapter.uow_factory(persistence_adapter.source_context(workspace_id, environment_id)) as uow:
        await uow.sources.record_observation(SourceObservationCommand.create(
            source=source_object, revision=source_revision, provenance=source_provenance,
            idempotency_key=IdempotencyKey("rollback-key"),
        ))
        await uow.rollback()
    async with persistence_adapter.uow_factory(persistence_adapter.source_context(workspace_id, environment_id)) as uow:
        assert await uow.sources.resolve_revision(source_revision.revision_id) is None
        await uow.rollback()
