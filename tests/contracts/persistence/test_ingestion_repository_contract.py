from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import pytest

from spine.application.persistence.command_digest import digest_command
from spine.application.persistence.context import PersistenceOperation
from spine.application.persistence.errors import ConstraintConflictError
from spine.application.persistence.idempotency import IdempotencyKey, OpaqueResultReference
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
    foreign_workspace_id = synthetic_uuid(1822)
    foreign_environment_id = synthetic_uuid(1823)
    await persist_workspace(persistence_adapter, foreign_workspace_id)
    await persist_environment(
        persistence_adapter, foreign_workspace_id, foreign_environment_id
    )
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
    ingestion_context = persistence_adapter.ingestion_context
    foreign_digest = digest_command(
        operation=PersistenceOperation("original.upload"),
        operation_schema_version=1,
        payload={"fixture": "foreign-original-receipt"},
    )
    async with persistence_adapter.uow_factory(
        ingestion_context(foreign_workspace_id, foreign_environment_id, "original.upload")
    ) as uow:
        foreign_claim = await uow.idempotency.claim(
            operation_schema_version=1,
            key=IdempotencyKey("foreign-original-upload"),
            digest=foreign_digest,
        )
        await uow.idempotency.complete(
            foreign_claim,
            OpaqueResultReference(
                result_type="original_upload",
                result_id=synthetic_uuid(1824),
                schema_version=1,
            ),
        )
        await uow.commit()
    binding = binding.model_copy(
        update={"original_receipt_id": foreign_claim.receipt_id}
    )
    binding_command = IngestionBindingCommand.create(
        item=item,
        binding=binding,
        observation=observation,
        idempotency_key=IdempotencyKey("contract-binding-command"),
    )
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
            await uow.commit()

    workspace_scoped_digest = digest_command(
        operation=PersistenceOperation("workspace_repository"),
        operation_schema_version=1,
        payload={"fixture": "workspace-scoped-original-receipt"},
    )
    async with persistence_adapter.uow_factory(
        persistence_adapter.workspace_context(foreign_workspace_id)
    ) as uow:
        workspace_scoped_claim = await uow.idempotency.claim(
            operation_schema_version=1,
            key=IdempotencyKey("workspace-scoped-original-upload"),
            digest=workspace_scoped_digest,
        )
        await uow.idempotency.complete(
            workspace_scoped_claim,
            OpaqueResultReference(
                result_type="original_upload",
                result_id=synthetic_uuid(1825),
                schema_version=1,
            ),
        )
        await uow.commit()
    workspace_scoped_binding = binding.model_copy(
        update={"original_receipt_id": workspace_scoped_claim.receipt_id}
    )
    workspace_scoped_command = IngestionBindingCommand.create(
        item=item,
        binding=workspace_scoped_binding,
        observation=observation,
        idempotency_key=IdempotencyKey("contract-binding-workspace-scoped-receipt"),
    )
    with pytest.raises(ConstraintConflictError):
        async with persistence_adapter.uow_factory(
            ingestion_context(workspace_id, environment_id, "ingestion_binding")
        ) as uow:
            await uow.ingestion.bind_source(workspace_scoped_command)
            await uow.commit()


async def exercise_ingestion_binding_race(adapter: PersistenceAdapter) -> None:
    assert adapter.ingestion_context is not None
    workspace_id = synthetic_uuid(1830)
    environment_id = synthetic_uuid(1831)
    await persist_workspace(adapter, workspace_id)
    await persist_environment(adapter, workspace_id, environment_id)
    run = IngestionRun(
        ingestion_run_id=synthetic_uuid(1832),
        workspace_id=workspace_id,
        environment_id=environment_id,
        command_identity="contract-binding-race-run",
        command_digest="a" * 64,
        trigger_kind="upload",
        requested_scope_schema="ingestion-scope:v1",
        requested_scope_digest="b" * 64,
        requested_at=NOW,
    )
    run_command = IngestionRunCommand.create(
        run=run, idempotency_key=IdempotencyKey("contract-binding-race-run")
    )
    item = IngestionRunSource(
        ingestion_run_source_id=synthetic_uuid(1833),
        ingestion_run_id=run.ingestion_run_id,
        workspace_id=workspace_id,
        environment_id=environment_id,
        input_ordinal=1,
        input_identity="contract-binding-race-input",
        input_digest="c" * 64,
        command_identity="contract-binding-race-item",
        command_digest="d" * 64,
        created_at=NOW,
    )
    rollback_item = item.model_copy(
        update={
            "ingestion_run_source_id": synthetic_uuid(1835),
            "input_ordinal": 2,
            "input_identity": "contract-binding-race-rollback-input",
            "command_identity": "contract-binding-race-rollback-item",
        }
    )

    def source_case(offset: int, label: str) -> tuple[SourceObject, object, SourceObservationCommand]:
        source = SourceObject(
            source_object_id=synthetic_uuid(offset),
            workspace_id=workspace_id,
            environment_id=environment_id,
            source_kind="document",
            identity_mode=IdentityMode.UPLOAD,
            upload_identity=f"contract-binding-race-upload-{label}",
            created_at=NOW,
        )
        metadata = RevisionMetadata(embedded_title=f"Contract race {label}")
        revision = content_revision(
            revision_id=synthetic_uuid(offset + 1),
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
                object_id=synthetic_uuid(offset + 2),
                storage_generation=synthetic_uuid(offset + 3),
                digest_algorithm="sha256",
                digest_hex="e" * 64,
                byte_length=4,
            ),
            original_sha256="e" * 64,
            byte_length=4,
            media_type="text/plain",
            observed_at=NOW,
        )
        return source, revision, SourceObservationCommand.create(
            source=source,
            revision=revision,
            provenance=SourceRevisionProvenance(
                provenance_id=synthetic_uuid(offset + 4),
                source_object_id=source.source_object_id,
                revision_id=revision.revision_id,
                workspace_id=workspace_id,
                environment_id=environment_id,
                producer_kind="upload",
                producer_reference="contract-race",
                event_identity=f"contract-binding-race-event-{label}",
                event_digest="f" * 64,
                observer_service="contract-race",
                received_at=NOW,
                observed_at=NOW,
            ),
            idempotency_key=IdempotencyKey(f"contract-binding-race-observation-{label}"),
        )

    first_source, first_revision, first_observation = source_case(1834, "one")
    second_source, second_revision, second_observation = source_case(1840, "two")
    original_digest = digest_command(
        operation=PersistenceOperation("original.upload"),
        operation_schema_version=1,
        payload={"fixture": "contract-binding-race"},
    )
    original_receipt_id = None
    ingestion_context = adapter.ingestion_context
    async with adapter.uow_factory(
        ingestion_context(workspace_id, environment_id, "ingestion_run")
    ) as uow:
        await uow.ingestion.create_run(run_command)
        await uow.ingestion.create_item(item)
        await uow.ingestion.create_item(rollback_item)
        await uow.commit()
    async with adapter.uow_factory(
        ingestion_context(workspace_id, environment_id, "original.upload")
    ) as uow:
        claim = await uow.idempotency.claim(
            operation_schema_version=1,
            key=IdempotencyKey("contract-binding-race-original"),
            digest=original_digest,
        )
        original_receipt_id = claim.receipt_id
        await uow.idempotency.complete(
            claim,
            OpaqueResultReference(
                result_type="original_upload",
                result_id=synthetic_uuid(1846),
                schema_version=1,
            ),
        )
        await uow.commit()
    assert original_receipt_id is not None

    def binding_case(
        source: SourceObject,
        revision: object,
        observation: SourceObservationCommand,
        label: str,
        binding_id: int,
        item_to_bind: IngestionRunSource = item,
    ) -> IngestionBindingCommand:
        return IngestionBindingCommand.create(
            item=item_to_bind,
            binding=IngestionRunSourceBinding(
                binding_id=synthetic_uuid(binding_id),
                ingestion_run_source_id=item_to_bind.ingestion_run_source_id,
                source_object_id=source.source_object_id,
                source_revision_id=revision.revision_id,
                workspace_id=workspace_id,
                environment_id=environment_id,
                binding_command_identity=f"contract-binding-race-{label}",
                binding_command_digest="a" * 64,
                original_reference=revision.original_reference,
                original_receipt_kind="issue6.write_receipt",
                original_receipt_id=original_receipt_id,
                original_receipt_digest=revision.original_sha256,
                bound_at=NOW,
            ),
            observation=observation,
            idempotency_key=IdempotencyKey(f"contract-binding-race-{label}"),
        )

    first = binding_case(first_source, first_revision, first_observation, "one", 1847)
    second = binding_case(second_source, second_revision, second_observation, "two", 1848)

    rollback_source, rollback_revision, rollback_observation = source_case(1850, "rollback")
    rollback = binding_case(
        rollback_source,
        rollback_revision,
        rollback_observation,
        "rollback",
        1855,
        rollback_item,
    )

    async def bind_then_fail(command: IngestionBindingCommand) -> None:
        async with adapter.uow_factory(
            ingestion_context(workspace_id, environment_id, "ingestion_binding")
        ) as uow:
            result = await uow.ingestion.bind_source(command)
            assert result.replay is False
            raise RuntimeError("injected pre-commit binding failure")

    with pytest.raises(RuntimeError, match="injected pre-commit"):
        await bind_then_fail(rollback)

    async with adapter.uow_factory(
        ingestion_context(workspace_id, environment_id, "ingestion_binding")
    ) as uow:
        retry_after_rollback = await uow.ingestion.bind_source(rollback)
        await uow.commit()
    assert retry_after_rollback.replay is False

    async def bind(command: IngestionBindingCommand) -> object:
        async with adapter.uow_factory(
            ingestion_context(workspace_id, environment_id, "ingestion_binding")
        ) as uow:
            result = await uow.ingestion.bind_source(command)
            await uow.commit()
            return result

    results = await asyncio.gather(bind(first), bind(second), return_exceptions=True)
    assert sum(isinstance(result, ConstraintConflictError) for result in results) == 1
    assert sum(not isinstance(result, BaseException) for result in results) == 1

    winner = first if not isinstance(results[0], BaseException) else second
    async with adapter.uow_factory(
        ingestion_context(workspace_id, environment_id, "ingestion_binding")
    ) as uow:
        replay = await uow.ingestion.bind_source(winner)
        await uow.commit()
    assert replay.replay is True
    assert replay.value.binding_id == winner.binding.binding_id


@pytest.mark.asyncio
async def test_ingestion_binding_race_contract(
    persistence_adapter: PersistenceAdapter,
) -> None:
    await exercise_ingestion_binding_race(persistence_adapter)


@pytest.mark.asyncio
async def test_ingestion_run_item_contract(persistence_adapter: PersistenceAdapter) -> None:
    await exercise_ingestion_run_item_contract(persistence_adapter)
