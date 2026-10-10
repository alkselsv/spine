from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import pytest

from spine.application.persistence import (
    IdempotencyKey,
    IdempotencyReplay,
    OpaqueResultReference,
    OwnedIdempotencyClaim,
    PersistenceOperation,
    PersistencePurpose,
    RetryEligibility,
    TransactionDeadlockError,
    TransactionSerializationError,
    UnitOfWork,
    WorkspaceScope,
    digest_command,
    run_transaction_with_retry,
)
from spine.domain.common import EnvironmentKind
from spine.domain.workspaces import Environment, Workspace
from spine.infrastructure.persistence.contexts import (
    TrustedContextBoundary,
    create_initial_workspace_bootstrap_authority,
)
from spine.infrastructure.persistence.in_memory import InMemoryPersistence


def synthetic_uuid(value: int) -> UUID:
    return UUID(int=value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "retry_error_type",
    (TransactionDeadlockError, TransactionSerializationError),
)
async def test_retry_restarts_complete_unit_of_work_and_commits_one_effect(
    retry_error_type: type[Exception],
) -> None:
    workspace_id = synthetic_uuid(2101)
    environment_id = synthetic_uuid(2102)
    result = OpaqueResultReference(
        result_type="environment",
        result_id=environment_id,
        schema_version=1,
    )
    boundary = TrustedContextBoundary.for_testing(
        issuer_id=synthetic_uuid(2103),
        secret=b"issue-46-complete-transaction-retry",
    )
    bootstrap_authority = create_initial_workspace_bootstrap_authority()
    persistence = InMemoryPersistence(
        context_verifier=boundary,
        bootstrap_authority=bootstrap_authority,
    )
    await persistence.initial_workspace_bootstrap.create_initial_workspace(
        bootstrap_authority,
        Workspace(
            id=workspace_id,
            slug="retry-workspace",
            display_name="Retry Workspace",
        ),
    )
    operation = PersistenceOperation("environment.create")
    context = boundary.interactive(
        scope=WorkspaceScope(workspace_id=workspace_id),
        acting_subject_id=synthetic_uuid(2104),
        purpose=PersistencePurpose("contract_test"),
        operation=operation,
        trace_id=synthetic_uuid(2105),
    )
    digest = digest_command(
        operation=operation,
        operation_schema_version=1,
        payload={"environment_id": environment_id, "budget": Decimal("10.00")},
    )
    key = IdempotencyKey("retry-entire-uow")
    attempts: list[int] = []

    async def transaction(uow: UnitOfWork, attempt: int) -> OpaqueResultReference:
        attempts.append(attempt)
        claim = await uow.idempotency.claim(
            operation_schema_version=1,
            key=key,
            digest=digest,
        )
        assert isinstance(claim, OwnedIdempotencyClaim)
        await uow.environments.add(
            Environment(
                id=environment_id,
                workspace_id=workspace_id,
                kind=EnvironmentKind.DEVELOPMENT,
                display_name="Development",
            )
        )
        if attempt == 1:
            raise retry_error_type("synthetic retryable failure")
        await uow.idempotency.complete(claim, result)
        return result

    committed = await run_transaction_with_retry(
        persistence.uow_factory,
        context,
        transaction,
        eligibility=RetryEligibility(
            idempotent=True,
            has_idempotency_key=True,
            reproducible=True,
        ),
    )

    assert committed == result
    assert attempts == [1, 2]
    async with persistence.uow_factory(context) as uow:
        assert await uow.environments.resolve(environment_id) == Environment(
            id=environment_id,
            workspace_id=workspace_id,
            kind=EnvironmentKind.DEVELOPMENT,
            display_name="Development",
        )
        replay = await uow.idempotency.claim(
            operation_schema_version=1,
            key=key,
            digest=digest,
        )
        assert isinstance(replay, IdempotencyReplay)
        assert replay.result == result
        await uow.commit()


@pytest.mark.asyncio
async def test_retry_exhaustion_stops_after_three_complete_unit_of_work_attempts() -> None:
    workspace_id = synthetic_uuid(2151)
    boundary = TrustedContextBoundary.for_testing(
        issuer_id=synthetic_uuid(2152),
        secret=b"issue-46-complete-transaction-exhaustion",
    )
    bootstrap_authority = create_initial_workspace_bootstrap_authority()
    persistence = InMemoryPersistence(
        context_verifier=boundary,
        bootstrap_authority=bootstrap_authority,
    )
    await persistence.initial_workspace_bootstrap.create_initial_workspace(
        bootstrap_authority,
        Workspace(
            id=workspace_id,
            slug="retry-exhaustion-workspace",
            display_name="Retry Exhaustion Workspace",
        ),
    )
    context = boundary.interactive(
        scope=WorkspaceScope(workspace_id=workspace_id),
        acting_subject_id=synthetic_uuid(2153),
        purpose=PersistencePurpose("contract_test"),
        operation=PersistenceOperation("environment.create"),
        trace_id=synthetic_uuid(2154),
    )
    attempts: list[int] = []

    async def transaction(_uow: UnitOfWork, attempt: int) -> None:
        attempts.append(attempt)
        raise TransactionDeadlockError("synthetic deadlock")

    with pytest.raises(TransactionDeadlockError):
        await run_transaction_with_retry(
            persistence.uow_factory,
            context,
            transaction,
            eligibility=RetryEligibility(
                idempotent=True,
                has_idempotency_key=True,
                reproducible=True,
            ),
        )

    assert attempts == [1, 2, 3]


@pytest.mark.asyncio
async def test_declared_external_side_effect_prevents_second_transaction_attempt() -> None:
    workspace_id = synthetic_uuid(2201)
    boundary = TrustedContextBoundary.for_testing(
        issuer_id=synthetic_uuid(2202),
        secret=b"issue-46-external-side-effect-retry",
    )
    bootstrap_authority = create_initial_workspace_bootstrap_authority()
    persistence = InMemoryPersistence(
        context_verifier=boundary,
        bootstrap_authority=bootstrap_authority,
    )
    await persistence.initial_workspace_bootstrap.create_initial_workspace(
        bootstrap_authority,
        Workspace(
            id=workspace_id,
            slug="external-effect-workspace",
            display_name="External Effect Workspace",
        ),
    )
    context = boundary.interactive(
        scope=WorkspaceScope(workspace_id=workspace_id),
        acting_subject_id=synthetic_uuid(2203),
        purpose=PersistencePurpose("contract_test"),
        operation=PersistenceOperation("external.notify"),
        trace_id=synthetic_uuid(2204),
    )
    attempts: list[int] = []

    async def transaction(_uow: UnitOfWork, attempt: int) -> None:
        attempts.append(attempt)
        raise TransactionSerializationError("synthetic serialization failure")

    with pytest.raises(TransactionSerializationError):
        await run_transaction_with_retry(
            persistence.uow_factory,
            context,
            transaction,
            eligibility=RetryEligibility(
                idempotent=True,
                has_idempotency_key=True,
                reproducible=True,
                external_side_effects=True,
            ),
        )

    assert attempts == [1]
