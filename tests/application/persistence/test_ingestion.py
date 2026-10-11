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
from spine.application.persistence.errors import ConstraintConflictError, InvalidPersistenceContextError
from spine.application.persistence.idempotency import IdempotencyKey
from spine.application.persistence.ingestion import IngestionRunCommand, deterministic_receipt_id
from spine.application.persistence.outbox import OutboxEventRegistry
from spine.domain.common import EnvironmentKind
from spine.domain.ingestion import (
    IngestionRun,
    IngestionRunSource,
    IngestionRunState,
    IngestionTargetKind,
    IngestionTransition,
)
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
