from __future__ import annotations

from uuid import UUID

import pytest

from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
    WorkspaceScope,
)
from spine.application.persistence.errors import ContextBundleConflictError
from spine.application.persistence.outbox import OutboxEventRegistry
from spine.domain.common import EnvironmentKind
from spine.domain.workspaces import Environment, Workspace
from spine.infrastructure.persistence.contexts import (
    TrustedContextBoundary,
    create_initial_workspace_bootstrap_authority,
)
from spine.infrastructure.persistence.in_memory import InMemoryPersistence

from tests.domain.context_bundle.test_context_bundle_models import context_bundle


WORKSPACE = UUID(int=1)
ENVIRONMENT = UUID(int=2)
ACTOR = UUID(int=30)
TRACE = UUID(int=31)
ISSUER = UUID(int=32)
SECRET = b"context-bundle-repository-test-secret-0001"


def _authority() -> TrustedContextBoundary:
    return TrustedContextBoundary.for_testing(issuer_id=ISSUER, secret=SECRET)


def _context(authority: TrustedContextBoundary):
    return authority.interactive(
        scope=EnvironmentScope(workspace_id=WORKSPACE, environment_id=ENVIRONMENT),
        acting_subject_id=ACTOR,
        purpose=PersistencePurpose("qa_retrieval"),
        operation=PersistenceOperation("persist_context_bundle"),
        trace_id=TRACE,
    )


async def _persistence() -> tuple[InMemoryPersistence, TrustedContextBoundary]:
    authority = _authority()
    bootstrap = create_initial_workspace_bootstrap_authority()
    persistence = InMemoryPersistence(
        outbox_events=OutboxEventRegistry(),
        context_verifier=authority,
        bootstrap_authority=bootstrap,
    )
    await persistence.initial_workspace_bootstrap.create_initial_workspace(
        bootstrap,
        Workspace(id=WORKSPACE, slug="qa", display_name="Q&A"),
    )
    async with persistence.uow_factory(
        authority.interactive(
            scope=WorkspaceScope(workspace_id=WORKSPACE),
            acting_subject_id=ACTOR,
            purpose=PersistencePurpose("qa_retrieval"),
            operation=PersistenceOperation("bootstrap_environment"),
            trace_id=TRACE,
        )
    ) as uow:
        await uow.environments.add(
            Environment(
                id=ENVIRONMENT,
                workspace_id=WORKSPACE,
                kind=EnvironmentKind.DEVELOPMENT,
                display_name="dev",
            )
        )
        await uow.commit()
    return persistence, authority


@pytest.mark.asyncio
async def test_context_bundle_repository_replays_exact_immutable_attempt() -> None:
    persistence, authority = await _persistence()
    bundle = context_bundle()

    async with persistence.uow_factory(_context(authority)) as uow:
        created = await uow.context_bundles.create(bundle)
        replay = await uow.context_bundles.create(bundle)
        assert created == bundle
        assert replay == bundle
        await uow.commit()

    async with persistence.uow_factory(_context(authority)) as uow:
        resolved = await uow.context_bundles.resolve_attempt(
            run_id=bundle.run_id,
            attempt_id=bundle.attempt_id,
            idempotency_key=bundle.idempotency_key,
        )
        assert resolved == bundle


@pytest.mark.asyncio
async def test_context_bundle_repository_rejects_conflicting_replay() -> None:
    persistence, authority = await _persistence()
    bundle = context_bundle()
    conflicting = context_bundle(
        bundle_id=UUID(int=99),
        evidence_references=(),
        policy_decision_generations=(),
        outcome="insufficient",
        reason_code="insufficient_evidence",
    )

    async with persistence.uow_factory(_context(authority)) as uow:
        await uow.context_bundles.create(bundle)
        with pytest.raises(ContextBundleConflictError, match="attempt identity"):
            await uow.context_bundles.create(conflicting)
