from __future__ import annotations

from datetime import datetime, timezone
import pytest

from spine.application.persistence.errors import ConstraintConflictError
from spine.application.persistence.idempotency import IdempotencyKey
from spine.application.persistence.ingestion import IngestionBindingCommand, IngestionRunCommand
from spine.application.persistence.repositories import SourceObservationCommand
from spine.domain.ingestion import (
    IngestionItemState,
    IngestionRun,
    IngestionRunSource,
    IngestionRunSourceBinding,
    IngestionRunState,
)
from spine.domain.sources import (
    IdentityMode,
    RevisionKind,
    RevisionMetadata,
    SourceObject,
    SourceRevisionProvenance,
    canonical_revision_metadata_digest,
)
from spine.domain.sources.canonicalization import content_revision
from spine.domain.sources.original_reference import ObjectReference

from .adapter import PersistenceAdapter
from .ids import synthetic_uuid
from .test_idempotency_repository_contract import persist_environment, persist_workspace


NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)


async def exercise_ingestion_run_item_contract(adapter: PersistenceAdapter) -> None:
    workspace_id = synthetic_uuid(1801)
    environment_id = synthetic_uuid(1802)
    await persist_workspace(adapter, workspace_id)
    await persist_environment(adapter, workspace_id, environment_id)
    context_factory = adapter.ingestion_context
    assert context_factory is not None

    run = IngestionRun(
        ingestion_run_id=synthetic_uuid(1803),
        workspace_id=workspace_id,
        environment_id=environment_id,
        command_identity="contract-ingestion-run",
        command_digest="a" * 64,
        trigger_kind="upload",
        requested_scope_schema="ingestion-scope:v1",
        requested_scope_digest="b" * 64,
        requested_at=NOW,
    )
    command = IngestionRunCommand.create(
        run=run, idempotency_key=IdempotencyKey("contract-ingestion-run")
    )
    item = IngestionRunSource(
        ingestion_run_source_id=synthetic_uuid(1804),
        ingestion_run_id=run.ingestion_run_id,
        workspace_id=workspace_id,
        environment_id=environment_id,
        input_ordinal=1,
        input_identity="contract-input",
        input_digest="c" * 64,
        command_identity="contract-ingestion-item",
        command_digest="d" * 64,
        created_at=NOW,
    )

    async with adapter.uow_factory(context_factory(workspace_id, environment_id, "ingestion_run")) as uow:
        first = await uow.ingestion.create_run(command)
        await uow.ingestion.create_item(item)
        await uow.commit()

    async with adapter.uow_factory(context_factory(workspace_id, environment_id, "ingestion_run")) as uow:
        replay = await uow.ingestion.create_run(command)
        state = await uow.ingestion.derive_run_state(run.ingestion_run_id)
        await uow.commit()

    assert first.replay is False
    assert replay.replay is True
    assert replay.result_reference == first.result_reference
    assert state is not None
    assert state.state is IngestionRunState.REQUESTED
    assert state.item_states == (IngestionItemState.RECEIVED,)


@pytest.mark.asyncio
async def test_ingestion_binding_requires_a_scoped_original_receipt(
    persistence_adapter: PersistenceAdapter,
) -> None:
    assert persistence_adapter.ingestion_context is not None
    workspace_id = synthetic_uuid(1811)
    environment_id = synthetic_uuid(1812)
    await persist_workspace(persistence_adapter, workspace_id)
    await persist_environment(persistence_adapter, workspace_id, environment_id)
    run = IngestionRun(
        ingestion_run_id=synthetic_uuid(1813),
        workspace_id=workspace_id,
        environment_id=environment_id,
        command_identity="contract-binding-run",
        command_digest="a" * 64,
        trigger_kind="upload",
        requested_scope_schema="ingestion-scope:v1",
        requested_scope_digest="b" * 64,
        requested_at=NOW,
    )
    command = IngestionRunCommand.create(
        run=run, idempotency_key=IdempotencyKey("contract-binding-run")
    )
    item = IngestionRunSource(
        ingestion_run_source_id=synthetic_uuid(1814),
        ingestion_run_id=run.ingestion_run_id,
        workspace_id=workspace_id,
        environment_id=environment_id,
        input_ordinal=1,
        input_identity="contract-binding-input",
        input_digest="c" * 64,
        command_identity="contract-binding-item",
        command_digest="d" * 64,
        created_at=NOW,
    )
    source = SourceObject(
        source_object_id=synthetic_uuid(1815),
        workspace_id=workspace_id,
        environment_id=environment_id,
        source_kind="document",
        identity_mode=IdentityMode.UPLOAD,
        upload_identity="contract-binding-upload",
        created_at=NOW,
    )
    metadata = RevisionMetadata(embedded_title="Contract binding")
    revision = content_revision(
        revision_id=synthetic_uuid(1816),
        source_object_id=source.source_object_id,
        workspace_id=workspace_id,
        environment_id=environment_id,
        kind=RevisionKind.CONTENT,
        revision_schema_version="source-revision:v1",
        revision_metadata_schema="revision-metadata:r1-document-v1",
        revision_metadata=metadata,
        revision_metadata_digest=canonical_revision_metadata_digest(metadata),
        original_reference=ObjectReference(
            schema_version=1,
            object_id=synthetic_uuid(1817),
            storage_generation=synthetic_uuid(1818),
            digest_algorithm="sha256",
            digest_hex="e" * 64,
            byte_length=4,
        ),
        original_sha256="e" * 64,
        byte_length=4,
        media_type="text/plain",
        observed_at=NOW,
    )
    observation = SourceObservationCommand.create(
        source=source,
        revision=revision,
        provenance=SourceRevisionProvenance(
            provenance_id=synthetic_uuid(1819),
            source_object_id=source.source_object_id,
            revision_id=revision.revision_id,
            workspace_id=workspace_id,
            environment_id=environment_id,
            producer_kind="upload",
            producer_reference="contract",
            event_identity="contract-binding-event",
            event_digest="f" * 64,
            observer_service="contract",
            received_at=NOW,
            observed_at=NOW,
        ),
        idempotency_key=IdempotencyKey("contract-binding-observation"),
    )
    binding = IngestionRunSourceBinding(
        binding_id=synthetic_uuid(1820),
        ingestion_run_source_id=item.ingestion_run_source_id,
        source_object_id=source.source_object_id,
        source_revision_id=revision.revision_id,
        workspace_id=workspace_id,
        environment_id=environment_id,
        binding_command_identity="contract-binding-command",
        binding_command_digest="a" * 64,
        original_reference=revision.original_reference,
        original_receipt_kind="issue6.write_receipt",
        original_receipt_id=synthetic_uuid(1821),
        original_receipt_digest=revision.original_sha256,
        bound_at=NOW,
    )
    binding_command = IngestionBindingCommand.create(
        item=item,
        binding=binding,
        observation=observation,
        idempotency_key=IdempotencyKey("contract-binding-command"),
    )
    ingestion_context = persistence_adapter.ingestion_context
    async with persistence_adapter.uow_factory(
        ingestion_context(workspace_id, environment_id, "ingestion_run")
    ) as uow:
        await uow.ingestion.create_run(command)
        await uow.ingestion.create_item(item)
        await uow.commit()
    with pytest.raises(ConstraintConflictError):
        async with persistence_adapter.uow_factory(
            ingestion_context(workspace_id, environment_id, "ingestion_binding")
        ) as uow:
            await uow.ingestion.bind_source(binding_command)


@pytest.mark.asyncio
async def test_ingestion_run_item_contract(persistence_adapter: PersistenceAdapter) -> None:
    await exercise_ingestion_run_item_contract(persistence_adapter)
