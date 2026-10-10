from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import pytest

from spine.application.persistence.command_digest import digest_command
from spine.application.persistence.context import PersistenceOperation
from spine.application.persistence.errors import IdempotencyConflictError, UnitOfWorkLifecycleError
from spine.application.persistence.idempotency import (
    IdempotencyKey,
    IdempotencyReplay,
    OpaqueResultReference,
    OwnedIdempotencyClaim,
)
from spine.domain.common import EnvironmentKind
from spine.domain.workspaces import Environment, Workspace

from .adapter import PersistenceAdapter
from .ids import synthetic_uuid


OPERATION_SCHEMA_VERSION = 1


def command_digest(operation: PersistenceOperation, amount: str = "12.3400"):
    return digest_command(
        operation=operation,
        operation_schema_version=OPERATION_SCHEMA_VERSION,
        payload={"amount": Decimal(amount), "customer_id": synthetic_uuid(1401)},
    )


def result_ref(result_id: UUID = synthetic_uuid(1501)) -> OpaqueResultReference:
    return OpaqueResultReference(
        result_type="proposal",
        result_id=result_id,
        schema_version=1,
    )


def workspace(workspace_id: UUID) -> Workspace:
    return Workspace(
        id=workspace_id,
        slug=f"northwind-{workspace_id.hex[-12:]}",
        display_name="Northwind",
    )


async def persist_workspace(adapter: PersistenceAdapter, workspace_id: UUID) -> None:
    context = adapter.workspace_context(workspace_id)
    async with adapter.uow_factory(context) as uow:
        await uow.workspaces.add(workspace(workspace_id))
        await uow.commit()


async def persist_environment(
    adapter: PersistenceAdapter,
    workspace_id: UUID,
    environment_id: UUID,
) -> None:
    context = adapter.workspace_context(workspace_id)
    async with adapter.uow_factory(context) as uow:
        await uow.environments.add(
            Environment(
                id=environment_id,
                workspace_id=workspace_id,
                kind=EnvironmentKind.PRODUCTION,
                display_name="Production",
            )
        )
        await uow.commit()


@pytest.mark.asyncio
async def test_claim_complete_and_replay_return_same_result_reference(
    persistence_adapter: PersistenceAdapter,
) -> None:
    workspace_id = synthetic_uuid(1101)
    await persist_workspace(persistence_adapter, workspace_id)
    context = persistence_adapter.workspace_context(workspace_id)
    key = IdempotencyKey("transport-key-1")
    digest = command_digest(context.operation)
    expected = result_ref()

    async with persistence_adapter.uow_factory(context) as uow:
        claim = await uow.idempotency.claim(
            operation_schema_version=OPERATION_SCHEMA_VERSION,
            key=key,
            digest=digest,
        )
        assert isinstance(claim, OwnedIdempotencyClaim)
        assert claim.operation == context.operation
        await uow.idempotency.complete(claim, expected)
        await uow.commit()

    async with persistence_adapter.uow_factory(context) as uow:
        replay = await uow.idempotency.claim(
            operation_schema_version=OPERATION_SCHEMA_VERSION,
            key=key,
            digest=digest,
        )
        assert isinstance(replay, IdempotencyReplay)
        assert replay.operation == context.operation
        assert replay.result == expected
        await uow.commit()


@pytest.mark.asyncio
async def test_same_key_with_different_digest_raises_stable_conflict(
    persistence_adapter: PersistenceAdapter,
) -> None:
    workspace_id = synthetic_uuid(1102)
    await persist_workspace(persistence_adapter, workspace_id)
    context = persistence_adapter.workspace_context(workspace_id)
    key = IdempotencyKey("transport-key-2")

    async with persistence_adapter.uow_factory(context) as uow:
        claim = await uow.idempotency.claim(
            operation_schema_version=OPERATION_SCHEMA_VERSION,
            key=key,
            digest=command_digest(context.operation, "12.34"),
        )
        assert isinstance(claim, OwnedIdempotencyClaim)
        await uow.idempotency.complete(claim, result_ref())
        await uow.commit()

    with pytest.raises(IdempotencyConflictError, match="Idempotency key conflicts"):
        async with persistence_adapter.uow_factory(context) as uow:
            await uow.idempotency.claim(
                operation_schema_version=OPERATION_SCHEMA_VERSION,
                key=key,
                digest=command_digest(context.operation, "99.99"),
            )


@pytest.mark.asyncio
async def test_independent_workspaces_environments_and_keys_do_not_collide(
    persistence_adapter: PersistenceAdapter,
) -> None:
    workspace_id = synthetic_uuid(1103)
    other_workspace_id = synthetic_uuid(1104)
    environment_id = synthetic_uuid(1201)
    other_environment_id = synthetic_uuid(1202)
    await persist_workspace(persistence_adapter, workspace_id)
    await persist_workspace(persistence_adapter, other_workspace_id)
    await persist_environment(persistence_adapter, workspace_id, environment_id)
    await persist_environment(persistence_adapter, workspace_id, other_environment_id)
    key = IdempotencyKey("shared-key")

    contexts = [
        persistence_adapter.workspace_context(workspace_id),
        persistence_adapter.workspace_context(other_workspace_id),
        persistence_adapter.environment_context(workspace_id, environment_id),
        persistence_adapter.environment_context(workspace_id, other_environment_id),
    ]

    for index, context in enumerate(contexts, start=1):
        digest = command_digest(context.operation)
        async with persistence_adapter.uow_factory(context) as uow:
            claim = await uow.idempotency.claim(
                operation_schema_version=OPERATION_SCHEMA_VERSION,
                key=key,
                digest=digest,
            )
            assert isinstance(claim, OwnedIdempotencyClaim)
            await uow.idempotency.complete(claim, result_ref(synthetic_uuid(1600 + index)))
            await uow.commit()

    workspace_context = persistence_adapter.workspace_context(workspace_id)
    async with persistence_adapter.uow_factory(workspace_context) as uow:
        different_key = await uow.idempotency.claim(
            operation_schema_version=OPERATION_SCHEMA_VERSION,
            key=IdempotencyKey("different-key"),
            digest=command_digest(workspace_context.operation),
        )
        assert isinstance(different_key, OwnedIdempotencyClaim)


@pytest.mark.asyncio
async def test_incomplete_owned_claim_prevents_commit_and_rolls_back(
    persistence_adapter: PersistenceAdapter,
) -> None:
    workspace_id = synthetic_uuid(1105)
    await persist_workspace(persistence_adapter, workspace_id)
    context = persistence_adapter.workspace_context(workspace_id)
    key = IdempotencyKey("incomplete-key")
    digest = command_digest(context.operation)

    async with persistence_adapter.uow_factory(context) as uow:
        claim = await uow.idempotency.claim(
            operation_schema_version=OPERATION_SCHEMA_VERSION,
            key=key,
            digest=digest,
        )
        assert isinstance(claim, OwnedIdempotencyClaim)
        with pytest.raises(UnitOfWorkLifecycleError, match="must be completed"):
            await uow.commit()

    async with persistence_adapter.uow_factory(context) as uow:
        claim = await uow.idempotency.claim(
            operation_schema_version=OPERATION_SCHEMA_VERSION,
            key=key,
            digest=digest,
        )
        assert isinstance(claim, OwnedIdempotencyClaim)


@pytest.mark.asyncio
async def test_rollback_discards_claim_and_completed_receipt(
    persistence_adapter: PersistenceAdapter,
) -> None:
    workspace_id = synthetic_uuid(1106)
    await persist_workspace(persistence_adapter, workspace_id)
    context = persistence_adapter.workspace_context(workspace_id)
    key = IdempotencyKey("rollback-key")
    digest = command_digest(context.operation)

    async with persistence_adapter.uow_factory(context) as uow:
        claim = await uow.idempotency.claim(
            operation_schema_version=OPERATION_SCHEMA_VERSION,
            key=key,
            digest=digest,
        )
        assert isinstance(claim, OwnedIdempotencyClaim)
        await uow.idempotency.complete(claim, result_ref())
        await uow.rollback()

    async with persistence_adapter.uow_factory(context) as uow:
        claim = await uow.idempotency.claim(
            operation_schema_version=OPERATION_SCHEMA_VERSION,
            key=key,
            digest=digest,
        )
        assert isinstance(claim, OwnedIdempotencyClaim)


@pytest.mark.asyncio
async def test_completing_replay_unknown_or_already_completed_claim_fails(
    persistence_adapter: PersistenceAdapter,
) -> None:
    workspace_id = synthetic_uuid(1107)
    await persist_workspace(persistence_adapter, workspace_id)
    context = persistence_adapter.workspace_context(workspace_id)
    key = IdempotencyKey("completion-conflict-key")
    digest = command_digest(context.operation)

    async with persistence_adapter.uow_factory(context) as uow:
        claim = await uow.idempotency.claim(
            operation_schema_version=OPERATION_SCHEMA_VERSION,
            key=key,
            digest=digest,
        )
        assert isinstance(claim, OwnedIdempotencyClaim)
        await uow.idempotency.complete(claim, result_ref())
        await uow.commit()

    async with persistence_adapter.uow_factory(context) as uow:
        claim = await uow.idempotency.claim(
            operation_schema_version=OPERATION_SCHEMA_VERSION,
            key=IdempotencyKey("already-completed-key"),
            digest=digest,
        )
        assert isinstance(claim, OwnedIdempotencyClaim)
        await uow.idempotency.complete(claim, result_ref(synthetic_uuid(1503)))
        with pytest.raises(IdempotencyConflictError, match="cannot be completed"):
            await uow.idempotency.complete(claim, result_ref(synthetic_uuid(1504)))

    async with persistence_adapter.uow_factory(context) as uow:
        replay = await uow.idempotency.claim(
            operation_schema_version=OPERATION_SCHEMA_VERSION,
            key=key,
            digest=digest,
        )
        assert isinstance(replay, IdempotencyReplay)
        with pytest.raises(IdempotencyConflictError, match="Only an owned"):
            await uow.idempotency.complete(replay, result_ref())  # type: ignore[arg-type]

    unknown_claim = OwnedIdempotencyClaim(
        kind="owned",
        receipt_id=synthetic_uuid(1999),
        operation=context.operation,
        operation_schema_version=OPERATION_SCHEMA_VERSION,
        key=IdempotencyKey("unknown-key"),
        digest=digest,
    )
    async with persistence_adapter.uow_factory(context) as uow:
        with pytest.raises(IdempotencyConflictError, match="cannot be completed"):
            await uow.idempotency.complete(unknown_claim, result_ref())


@pytest.mark.asyncio
async def test_differently_scoped_claim_cannot_complete(
    persistence_adapter: PersistenceAdapter,
) -> None:
    workspace_id = synthetic_uuid(1108)
    other_workspace_id = synthetic_uuid(1109)
    await persist_workspace(persistence_adapter, workspace_id)
    await persist_workspace(persistence_adapter, other_workspace_id)
    context = persistence_adapter.workspace_context(workspace_id)
    digest = command_digest(context.operation)

    async with persistence_adapter.uow_factory(context) as uow:
        claim = await uow.idempotency.claim(
            operation_schema_version=OPERATION_SCHEMA_VERSION,
            key=IdempotencyKey("scope-key"),
            digest=digest,
        )
        assert isinstance(claim, OwnedIdempotencyClaim)
        await uow.rollback()

    async with persistence_adapter.uow_factory(
        persistence_adapter.workspace_context(other_workspace_id)
    ) as uow:
        with pytest.raises(IdempotencyConflictError, match="cannot be completed"):
            await uow.idempotency.complete(claim, result_ref())
