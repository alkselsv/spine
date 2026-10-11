from datetime import datetime, timezone
from uuid import NAMESPACE_URL, UUID, uuid5

import asyncio
import pytest

from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
    WorkspaceScope,
)
from spine.application.persistence.command_digest import digest_command
from spine.application.persistence.errors import ConstraintConflictError, InvalidPersistenceContextError
from spine.application.persistence.idempotency import IdempotencyKey, OpaqueResultReference
from spine.application.persistence.ingestion import (
    IngestionBindingCommand,
    IngestionRunCommand,
    deterministic_receipt_id,
)
from spine.application.persistence.repositories import SourceObservationCommand
from spine.application.persistence.outbox import OutboxEventRegistry
from spine.domain.common import EnvironmentKind
from spine.domain.ingestion import (
    IngestionRun,
    IngestionRunSource,
    IngestionRunSourceBinding,
    IngestionRunState,
    IngestionItemState,
    IngestionTargetKind,
    IngestionTransition,
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
from spine.domain.workspaces import Environment, Workspace
from spine.infrastructure.persistence.contexts import TrustedContextBoundary, create_initial_workspace_bootstrap_authority
from spine.infrastructure.persistence.in_memory import InMemoryPersistence


NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
WORKSPACE = uuid5(NAMESPACE_URL, "issue86-workspace")
ENVIRONMENT = uuid5(NAMESPACE_URL, "issue86-environment")


def context(boundary: TrustedContextBoundary, operation: str):
    return boundary.worker(
        scope=EnvironmentScope(workspace_id=WORKSPACE, environment_id=ENVIRONMENT),
        service_principal_id=uuid5(NAMESPACE_URL, "issue86-service"),
        purpose=PersistencePurpose("ingestion_test"),
        operation=PersistenceOperation(operation),
        trace_id=uuid5(NAMESPACE_URL, f"trace-{operation}"),
    )


@pytest.fixture
async def persistence():
    boundary = TrustedContextBoundary.for_testing(issuer_id=uuid5(NAMESPACE_URL, "issue86-issuer"), secret=b"issue86-test-secret-32-bytes-long")
    authority = create_initial_workspace_bootstrap_authority()
    persistence = InMemoryPersistence(
        context_verifier=boundary,
        outbox_events=OutboxEventRegistry(),
        bootstrap_authority=authority,
        transaction_lock=asyncio.Lock(),
    )
    await persistence.initial_workspace_bootstrap.create_initial_workspace(
        authority, Workspace(id=WORKSPACE, slug="issue86", display_name="Issue 86")
    )
    async with persistence.uow_factory(
        boundary.worker(
            scope=WorkspaceScope(workspace_id=WORKSPACE),
            service_principal_id=uuid5(NAMESPACE_URL, "issue86-service"), purpose=PersistencePurpose("ingestion_test"),
            operation=PersistenceOperation("environment_repository"), trace_id=uuid5(NAMESPACE_URL, "environment-trace")
        )
    ) as uow:
        await uow.environments.add(Environment(id=ENVIRONMENT, workspace_id=WORKSPACE, kind=EnvironmentKind.DEVELOPMENT, display_name="Issue 86"))
        await uow.commit()
    return persistence, boundary


def run(key: str) -> IngestionRun:
    return IngestionRun(
        ingestion_run_id=uuid5(NAMESPACE_URL, f"run-{key}"), workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT, command_identity=f"command-{key}", command_digest="a" * 64,
        idempotency_receipt_id=deterministic_receipt_id(workspace_id=WORKSPACE, environment_id=ENVIRONMENT, operation="ingestion_run", key=IdempotencyKey(key)),
        trigger_kind="upload", requested_scope_schema="ingestion-scope:v1", requested_scope_digest="b" * 64,
        requested_at=NOW,
    )


def binding_observation(label: str = "default") -> tuple[SourceObject, object, SourceObservationCommand]:
    source = SourceObject(
        source_object_id=uuid5(NAMESPACE_URL, f"issue86-binding-source-{label}"),
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        source_kind="document",
        identity_mode=IdentityMode.UPLOAD,
        upload_identity=f"issue86-binding-upload-{label}",
        created_at=NOW,
    )
    metadata = RevisionMetadata(embedded_title="Issue 86 binding")
    revision = content_revision(
        revision_id=uuid5(NAMESPACE_URL, f"issue86-binding-revision-{label}"),
        source_object_id=source.source_object_id,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        kind=RevisionKind.CONTENT,
        revision_schema_version="source-revision:v1",
        revision_metadata_schema="revision-metadata:r1-document-v1",
        revision_metadata=metadata,
        revision_metadata_digest=canonical_revision_metadata_digest(metadata),
        original_reference=ObjectReference(
            schema_version=1,
            object_id=uuid5(NAMESPACE_URL, f"issue86-binding-object-{label}"),
            storage_generation=uuid5(NAMESPACE_URL, f"issue86-binding-generation-{label}"),
            digest_algorithm="sha256",
            digest_hex="c" * 64,
            byte_length=4,
        ),
        original_sha256="c" * 64,
        byte_length=4,
        media_type="text/plain",
        observed_at=NOW,
    )
    provenance = SourceRevisionProvenance(
        provenance_id=uuid5(NAMESPACE_URL, f"issue86-binding-provenance-{label}"),
        source_object_id=source.source_object_id,
        revision_id=revision.revision_id,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        producer_kind="upload",
        producer_reference="issue86-test",
        event_identity=f"issue86-binding-event-{label}",
        event_digest="d" * 64,
        observer_service="issue86-test",
        received_at=NOW,
        observed_at=NOW,
    )
    return source, revision, SourceObservationCommand.create(
        source=source,
        revision=revision,
        provenance=provenance,
        idempotency_key=IdempotencyKey(f"issue86-binding-observation-{label}"),
    )


def binding_command(
    item: IngestionRunSource,
    source: SourceObject,
    revision: object,
    observation: SourceObservationCommand,
    binding_id: str,
    command_id: str,
    receipt_id: UUID,
) -> IngestionBindingCommand:
    binding = IngestionRunSourceBinding(
        binding_id=uuid5(NAMESPACE_URL, binding_id),
        ingestion_run_source_id=item.ingestion_run_source_id,
        source_object_id=source.source_object_id,
        source_revision_id=revision.revision_id,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        binding_command_identity=command_id,
        binding_command_digest="a" * 64,
        original_reference=revision.original_reference,
        original_receipt_kind="issue6.write_receipt",
        original_receipt_id=receipt_id,
        original_receipt_digest=revision.original_sha256,
        bound_at=NOW,
    )
    return IngestionBindingCommand.create(
        item=item,
        binding=binding,
        observation=observation,
        idempotency_key=IdempotencyKey(command_id),
    )


@pytest.mark.asyncio
async def test_ingestion_run_and_item_replay_are_stable(persistence) -> None:
    adapter, boundary = persistence
    command = IngestionRunCommand.create(run=run("same"), idempotency_key=IdempotencyKey("same"))
    assert command.run.command_digest == command.digest.value
    async with adapter.uow_factory(context(boundary, "ingestion_run")) as uow:
        first = await uow.ingestion.create_run(command)
        await uow.ingestion.create_item(IngestionRunSource(
            ingestion_run_source_id=uuid5(NAMESPACE_URL, "item-1"), ingestion_run_id=command.run.ingestion_run_id,
            workspace_id=WORKSPACE, environment_id=ENVIRONMENT, input_ordinal=1, input_identity="input-1",
            input_digest="c" * 64, command_identity="item-command-1", command_digest="d" * 64, created_at=NOW,
        ))
        await uow.commit()
    async with adapter.uow_factory(context(boundary, "ingestion_run")) as uow:
        replay = await uow.ingestion.create_run(command)
        state = await uow.ingestion.derive_run_state(command.run.ingestion_run_id)
        await uow.rollback()
    assert replay.replay is True
    assert replay.result_reference == first.result_reference
    assert state is not None and state.state is IngestionRunState.REQUESTED


@pytest.mark.asyncio
async def test_content_binding_rejects_unverified_original_receipt(persistence) -> None:
    adapter, boundary = persistence
    source, revision, observation = binding_observation()
    command = IngestionRunCommand.create(run=run("binding-receipt"), idempotency_key=IdempotencyKey("binding-receipt"))
    item = IngestionRunSource(
        ingestion_run_source_id=uuid5(NAMESPACE_URL, "issue86-binding-item"),
        ingestion_run_id=command.run.ingestion_run_id,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        input_ordinal=1,
        input_identity="issue86-binding-input",
        input_digest="e" * 64,
        command_identity="issue86-binding-item-command",
        command_digest="f" * 64,
        created_at=NOW,
    )
    async with adapter.uow_factory(context(boundary, "original.upload")) as uow:
        claim = await uow.idempotency.claim(
            operation_schema_version=1,
            key=IdempotencyKey("issue86-unverified-original-upload"),
            digest=digest_command(
                operation=PersistenceOperation("original.upload"),
                operation_schema_version=1,
                payload={"fixture": "issue86-unverified-original"},
            ),
        )
        await uow.idempotency.complete(
            claim,
            OpaqueResultReference(
                result_type="original_upload",
                result_id=uuid5(NAMESPACE_URL, "issue86-unverified-original-result"),
                schema_version=1,
            ),
        )
        await uow.commit()

    binding = IngestionRunSourceBinding(
        binding_id=uuid5(NAMESPACE_URL, "issue86-binding"),
        ingestion_run_source_id=item.ingestion_run_source_id,
        source_object_id=source.source_object_id,
        source_revision_id=revision.revision_id,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        binding_command_identity="issue86-binding-command",
        binding_command_digest="a" * 64,
        original_reference=revision.original_reference,
        original_receipt_kind="issue6.write_receipt",
        original_receipt_id=claim.receipt_id,
        original_receipt_digest=revision.original_sha256,
        bound_at=NOW,
    )
    binding_command = IngestionBindingCommand.create(
        item=item,
        binding=binding,
        observation=observation,
        idempotency_key=IdempotencyKey("issue86-binding-command"),
    )

    async with adapter.uow_factory(context(boundary, "ingestion_run")) as uow:
        await uow.ingestion.create_run(command)
        await uow.ingestion.create_item(item)
        await uow.commit()

    async with adapter.uow_factory(context(boundary, "ingestion_binding")) as uow:
        with pytest.raises(ConstraintConflictError, match="verification is unavailable"):
            await uow.ingestion.bind_source(binding_command)

    async with adapter.uow_factory(context(boundary, "ingestion_run")) as uow:
        state = await uow.ingestion.derive_run_state(command.run.ingestion_run_id)
        await uow.rollback()
    assert state is not None
    assert state.item_states == (IngestionItemState.RECEIVED,)


@pytest.mark.asyncio
async def test_concurrent_content_bindings_fail_closed_without_effect(persistence) -> None:
    adapter, boundary = persistence
    source, revision, observation = binding_observation("one")
    source_two, revision_two, observation_two = binding_observation("two")
    command = IngestionRunCommand.create(run=run("binding-race"), idempotency_key=IdempotencyKey("binding-race"))
    item = IngestionRunSource(
        ingestion_run_source_id=uuid5(NAMESPACE_URL, "issue86-binding-race-item"),
        ingestion_run_id=command.run.ingestion_run_id,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        input_ordinal=1,
        input_identity="issue86-binding-race-input",
        input_digest="e" * 64,
        command_identity="issue86-binding-race-item-command",
        command_digest="f" * 64,
        created_at=NOW,
    )
    receipt_key = IdempotencyKey("issue86-original-upload")
    receipt_digest = digest_command(
        operation=PersistenceOperation("original.upload"),
        operation_schema_version=1,
        payload={"fixture": "issue86-binding-race"},
    )
    receipt_id = None
    async with adapter.uow_factory(context(boundary, "ingestion_run")) as uow:
        await uow.ingestion.create_run(command)
        await uow.ingestion.create_item(item)
        await uow.commit()
    async with adapter.uow_factory(context(boundary, "original.upload")) as uow:
        claim = await uow.idempotency.claim(
            operation_schema_version=1, key=receipt_key, digest=receipt_digest
        )
        receipt_id = claim.receipt_id
        await uow.idempotency.complete(
            claim,
            OpaqueResultReference(
                result_type="original_upload",
                result_id=uuid5(NAMESPACE_URL, "issue86-original-upload-result"),
                schema_version=1,
            ),
        )
        await uow.commit()
    assert receipt_id is not None
    first = binding_command(
        item, source, revision, observation, "issue86-binding-race-one", "issue86-binding-race-one", receipt_id
    )
    second = binding_command(
        item, source_two, revision_two, observation_two, "issue86-binding-race-two", "issue86-binding-race-two", receipt_id
    )

    async def bind(command: IngestionBindingCommand) -> object:
        async with adapter.uow_factory(context(boundary, "ingestion_binding")) as uow:
            result = await uow.ingestion.bind_source(command)
            await uow.commit()
            return result

    results = await asyncio.gather(bind(first), bind(second), return_exceptions=True)
    assert len(results) == 2, results
    assert all(isinstance(result, ConstraintConflictError) for result in results)
    async with adapter.uow_factory(context(boundary, "ingestion_run")) as uow:
        state = await uow.ingestion.derive_run_state(command.run.ingestion_run_id)
        await uow.rollback()
    assert state is not None
    assert state.item_states == (IngestionItemState.RECEIVED,)


@pytest.mark.asyncio
async def test_run_state_derives_partial_success_from_item_heads(persistence) -> None:
    adapter, boundary = persistence
    command = IngestionRunCommand.create(run=run("partial"), idempotency_key=IdempotencyKey("partial"))
    successful_item = IngestionRunSource(
        ingestion_run_source_id=uuid5(NAMESPACE_URL, "issue86-partial-success-item"),
        ingestion_run_id=command.run.ingestion_run_id,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        input_ordinal=1,
        input_identity="issue86-partial-success-input",
        input_digest="c" * 64,
        command_identity="issue86-partial-success-command",
        command_digest="d" * 64,
        created_at=NOW,
    )
    failed_item = successful_item.model_copy(update={
        "ingestion_run_source_id": uuid5(NAMESPACE_URL, "issue86-partial-failed-item"),
        "input_ordinal": 2,
        "input_identity": "issue86-partial-failed-input",
        "command_identity": "issue86-partial-failed-command",
    })

    async with adapter.uow_factory(context(boundary, "ingestion_run")) as uow:
        await uow.ingestion.create_run(command)
        await uow.ingestion.create_item(successful_item)
        await uow.ingestion.create_item(failed_item)
        run_previous = uuid5(NAMESPACE_URL, f"ingestion-run-root:{command.run.ingestion_run_id}")
        for state in (IngestionRunState.RECEIVING, IngestionRunState.PROCESSING):
            transition_id = uuid5(NAMESPACE_URL, f"issue86-partial-run-{state.value}")
            await uow.ingestion.append_transition(IngestionTransition(
                ingestion_transition_id=transition_id,
                workspace_id=WORKSPACE,
                environment_id=ENVIRONMENT,
                target_kind=IngestionTargetKind.RUN,
                ingestion_run_id=command.run.ingestion_run_id,
                predecessor_transition_id=run_previous,
                state=state,
                attempt=1,
                actor_or_service="issue86-test",
                trace_id=uuid5(NAMESPACE_URL, f"issue86-partial-run-trace-{state.value}"),
                occurred_at=NOW,
            ))
            run_previous = transition_id

        for item, terminal_state in ((successful_item, IngestionItemState.PROCESSED), (failed_item, IngestionItemState.PROCESSING_FAILED)):
            previous = uuid5(NAMESPACE_URL, f"ingestion-item-root:{item.ingestion_run_source_id}")
            states = (
                IngestionItemState.OBSERVED,
                IngestionItemState.ACCEPTED,
                IngestionItemState.PROCESSING,
                terminal_state,
            )
            for index, state in enumerate(states, start=1):
                transition_id = uuid5(NAMESPACE_URL, f"issue86-partial-{item.ingestion_run_source_id}-{index}")
                await uow.ingestion.append_transition(IngestionTransition(
                    ingestion_transition_id=transition_id,
                    workspace_id=WORKSPACE,
                    environment_id=ENVIRONMENT,
                    target_kind=IngestionTargetKind.ITEM,
                    ingestion_run_id=command.run.ingestion_run_id,
                    ingestion_run_source_id=item.ingestion_run_source_id,
                    predecessor_transition_id=previous,
                    state=state,
                    attempt=1,
                    actor_or_service="issue86-test",
                    trace_id=uuid5(NAMESPACE_URL, f"issue86-partial-trace-{item.ingestion_run_source_id}-{index}"),
                    occurred_at=NOW,
                ))
                previous = transition_id
        await uow.commit()

    async with adapter.uow_factory(context(boundary, "ingestion_run")) as uow:
        state = await uow.ingestion.derive_run_state(command.run.ingestion_run_id)
        await uow.rollback()
    assert state is not None
    assert state.state is IngestionRunState.PARTIALLY_SUCCEEDED


@pytest.mark.asyncio
async def test_illegal_ingestion_transition_is_rejected_without_mutation(persistence) -> None:
    adapter, boundary = persistence
    command = IngestionRunCommand.create(run=run("illegal"), idempotency_key=IdempotencyKey("illegal"))
    async with adapter.uow_factory(context(boundary, "ingestion_run")) as uow:
        await uow.ingestion.create_run(command)
        with pytest.raises(ConstraintConflictError):
            await uow.ingestion.append_transition(IngestionTransition(
                ingestion_transition_id=uuid5(NAMESPACE_URL, "bad"), workspace_id=WORKSPACE, environment_id=ENVIRONMENT,
                target_kind=IngestionTargetKind.RUN,
                ingestion_run_id=command.run.ingestion_run_id, state=IngestionRunState.SUCCEEDED, attempt=1,
                actor_or_service="test", trace_id=uuid5(NAMESPACE_URL, "bad-trace"), occurred_at=NOW,
            ))


@pytest.mark.asyncio
async def test_concurrent_same_run_command_has_one_effect_and_stable_replay(persistence) -> None:
    adapter, boundary = persistence
    command = IngestionRunCommand.create(run=run("race"), idempotency_key=IdempotencyKey("race"))

    async def create() -> object:
        async with adapter.uow_factory(context(boundary, "ingestion_run")) as uow:
            result = await uow.ingestion.create_run(command)
            await uow.commit()
            return result

    first, second = await asyncio.gather(create(), create())

    assert {first.replay, second.replay} == {False, True}
    assert first.result_reference == second.result_reference


@pytest.mark.asyncio
async def test_cancelled_ingestion_run_does_not_leave_a_partial_effect(persistence) -> None:
    adapter, boundary = persistence
    command = IngestionRunCommand.create(run=run("cancel"), idempotency_key=IdempotencyKey("cancel"))

    async def cancelled() -> None:
        async with adapter.uow_factory(context(boundary, "ingestion_run")) as uow:
            await uow.ingestion.create_run(command)
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await cancelled()

    async with adapter.uow_factory(context(boundary, "ingestion_run")) as uow:
        assert await uow.ingestion.derive_run_state(command.run.ingestion_run_id) is None
        await uow.rollback()


@pytest.mark.asyncio
async def test_ingestion_scope_mismatch_fails_without_disclosing_ledger_rows(persistence) -> None:
    adapter, boundary = persistence
    foreign_run = run("foreign").model_copy(update={"workspace_id": uuid5(NAMESPACE_URL, "foreign-workspace")})
    command = IngestionRunCommand.create(run=foreign_run, idempotency_key=IdempotencyKey("foreign"))

    async with adapter.uow_factory(context(boundary, "ingestion_run")) as uow:
        with pytest.raises(InvalidPersistenceContextError):
            await uow.ingestion.create_run(command)

    async with adapter.uow_factory(context(boundary, "ingestion_run")) as uow:
        assert await uow.ingestion.derive_run_state(foreign_run.ingestion_run_id) is None
        await uow.rollback()
