from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

import pytest

from spine.application.persistence.command_digest import digest_command
from spine.application.persistence.context import (
    PersistenceOperation,
    PersistencePurpose,
    WorkspaceScope,
)
from spine.application.persistence.errors import IdempotencyConflictError
from spine.application.persistence.idempotency import (
    IdempotencyKey,
    IdempotencyReplay,
    OpaqueResultReference,
    OwnedIdempotencyClaim,
)
from spine.domain.workspaces import Workspace
from spine.infrastructure.persistence.contexts import (
    TrustedContextBoundary,
    create_initial_workspace_bootstrap_authority,
)
from spine.infrastructure.persistence.in_memory import InMemoryPersistence


WORKSPACE_ID = UUID("10000000-0000-0000-0000-000000000041")
ACTOR_ID = UUID("20000000-0000-0000-0000-000000000041")
TRACE_ID = UUID("30000000-0000-0000-0000-000000000041")
ISSUER_ID = UUID("40000000-0000-0000-0000-000000000041")
RESULT_ID = UUID("50000000-0000-0000-0000-000000000041")


@dataclass
class ResultResolver:
    authorized: bool = True
    resolutions: int = 0

    async def resolve(
        self,
        actor_id: UUID,
        reference: OpaqueResultReference,
    ) -> OpaqueResultReference:
        self.resolutions += 1
        if actor_id != ACTOR_ID or not self.authorized:
            raise PermissionError("Current authorization denied.")
        return reference


async def execute_idempotent_example(
    persistence: InMemoryPersistence,
    boundary: TrustedContextBoundary,
    resolver: ResultResolver,
    key: IdempotencyKey,
) -> OpaqueResultReference:
    context = boundary.interactive(
        scope=WorkspaceScope(workspace_id=WORKSPACE_ID),
        acting_subject_id=ACTOR_ID,
        purpose=PersistencePurpose("question_answering"),
        operation=PersistenceOperation("proposal.generate"),
        trace_id=TRACE_ID,
    )
    digest = digest_command(
        operation=PersistenceOperation("proposal.generate"),
        operation_schema_version=1,
        payload={"amount": Decimal("10.00")},
    )
    async with persistence.uow_factory(context) as uow:
        outcome = await uow.idempotency.claim(
            operation_schema_version=1,
            key=key,
            digest=digest,
        )
        if isinstance(outcome, IdempotencyReplay):
            await uow.commit()
            return await resolver.resolve(ACTOR_ID, outcome.result)
        assert isinstance(outcome, OwnedIdempotencyClaim)
        reference = OpaqueResultReference(
            result_type="proposal",
            result_id=RESULT_ID,
            schema_version=1,
        )
        await uow.idempotency.complete(outcome, reference)
        await uow.commit()
        return await resolver.resolve(ACTOR_ID, reference)


@pytest.mark.asyncio
async def test_replay_revalidates_current_authorization_and_stores_only_reference() -> None:
    boundary = TrustedContextBoundary.for_testing(
        issuer_id=ISSUER_ID,
        secret=b"issue-41-replay-authorization-secret",
    )
    bootstrap_authority = create_initial_workspace_bootstrap_authority()
    persistence = InMemoryPersistence(
        context_verifier=boundary,
        bootstrap_authority=bootstrap_authority,
    )
    await persistence.initial_workspace_bootstrap.create_initial_workspace(
        bootstrap_authority,
        Workspace(
            id=WORKSPACE_ID,
            slug="northwind",
            display_name="Northwind",
        ),
    )
    resolver = ResultResolver()
    key = IdempotencyKey("authorization-replay-key")

    assert await execute_idempotent_example(persistence, boundary, resolver, key) == (
        OpaqueResultReference("proposal", RESULT_ID, 1)
    )
    resolver.authorized = False
    with pytest.raises(PermissionError, match="Current authorization denied"):
        await execute_idempotent_example(persistence, boundary, resolver, key)

    assert resolver.resolutions == 2


@pytest.mark.asyncio
async def test_same_key_and_digest_under_different_trusted_operation_does_not_replay() -> None:
    boundary = TrustedContextBoundary.for_testing(
        issuer_id=ISSUER_ID,
        secret=b"issue-41-operation-isolation-secret",
    )
    bootstrap_authority = create_initial_workspace_bootstrap_authority()
    persistence = InMemoryPersistence(
        context_verifier=boundary,
        bootstrap_authority=bootstrap_authority,
    )
    await persistence.initial_workspace_bootstrap.create_initial_workspace(
        bootstrap_authority,
        Workspace(
            id=WORKSPACE_ID,
            slug="northwind",
            display_name="Northwind",
        ),
    )
    key = IdempotencyKey("operation-isolation-key")
    operation_a = PersistenceOperation("proposal.generate")
    operation_b = PersistenceOperation("proposal.reprice")
    context_a = boundary.interactive(
        scope=WorkspaceScope(workspace_id=WORKSPACE_ID),
        acting_subject_id=ACTOR_ID,
        purpose=PersistencePurpose("question_answering"),
        operation=operation_a,
        trace_id=TRACE_ID,
    )
    context_b = boundary.interactive(
        scope=WorkspaceScope(workspace_id=WORKSPACE_ID),
        acting_subject_id=ACTOR_ID,
        purpose=PersistencePurpose("question_answering"),
        operation=operation_b,
        trace_id=UUID("30000000-0000-0000-0000-000000000042"),
    )
    digest = digest_command(
        operation=operation_a,
        operation_schema_version=1,
        payload={"amount": Decimal("10.00")},
    )

    async with persistence.uow_factory(context_a) as uow:
        claim_a = await uow.idempotency.claim(
            operation_schema_version=1,
            key=key,
            digest=digest,
        )
        assert isinstance(claim_a, OwnedIdempotencyClaim)
        await uow.idempotency.complete(
            claim_a,
            OpaqueResultReference("proposal", RESULT_ID, 1),
        )
        await uow.commit()

    async with persistence.uow_factory(context_b) as uow:
        claim_b = await uow.idempotency.claim(
            operation_schema_version=1,
            key=key,
            digest=digest,
        )
        assert isinstance(claim_b, OwnedIdempotencyClaim)
        assert claim_b.operation == operation_b
        with pytest.raises(IdempotencyConflictError, match="cannot be completed"):
            await uow.idempotency.complete(
                claim_a,
                OpaqueResultReference("proposal", RESULT_ID, 1),
            )
