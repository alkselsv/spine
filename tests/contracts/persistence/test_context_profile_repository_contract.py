from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest

from spine.application.persistence.errors import (
    ConstraintConflictError,
    ContextProfileDigestMismatchError,
    ContextProfileNotFoundError,
    ContextProfileRevokedError,
)
from spine.domain.common import EnvironmentKind
from spine.domain.context_profiles import (
    CompletenessRequirement,
    ContextProfileVersion,
    PartialAnswerFallback,
    RetrievalStrategy,
)
from spine.domain.workspaces import Environment, Workspace

from .adapter import PersistenceAdapter
from .ids import synthetic_uuid


def profile(workspace_id: UUID, environment_id: UUID, **overrides: object) -> ContextProfileVersion:
    values: dict[str, object] = {
        "profile_id": synthetic_uuid(3201),
        "workspace_id": workspace_id,
        "environment_id": environment_id,
        "profile_version": 1,
        "retrieval_strategy": RetrievalStrategy.HYBRID,
        "max_results": 25,
        "max_tokens": 4096,
        "max_relation_hops": 2,
        "freshness_max_age_seconds": 3600,
        "completeness_requirement": CompletenessRequirement.BEST_EFFORT,
        "partial_answer_fallback": PartialAnswerFallback.DISALLOWED,
        "server_approved": True,
        "created_at": datetime(2026, 10, 14, tzinfo=timezone.utc),
    }
    values.update(overrides)
    return ContextProfileVersion.create(**values)


async def persist_scope(
    adapter: PersistenceAdapter, workspace_id: UUID, environment_id: UUID
) -> None:
    async with adapter.uow_factory(adapter.workspace_context(workspace_id)) as uow:
        await uow.workspaces.add(
            Workspace(
                id=workspace_id,
                slug=f"profile-{workspace_id.hex[-8:]}",
                display_name="Profile workspace",
            )
        )
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
async def test_context_profile_repository_replays_same_immutable_version(
    persistence_adapter: PersistenceAdapter,
) -> None:
    workspace_id = synthetic_uuid(3202)
    environment_id = synthetic_uuid(3203)
    expected = profile(workspace_id, environment_id)
    await persist_scope(persistence_adapter, workspace_id, environment_id)
    context = persistence_adapter.environment_context(workspace_id, environment_id)

    async with persistence_adapter.uow_factory(context) as uow:
        await uow.context_profiles.add(expected)
        await uow.commit()

    async with persistence_adapter.uow_factory(context) as uow:
        actual = await uow.context_profiles.resolve(
            profile_id=expected.profile_id,
            profile_version=expected.profile_version,
            profile_digest=expected.profile_digest,
        )

    assert actual == expected


@pytest.mark.asyncio
async def test_context_profile_repository_create_is_idempotent_for_same_version(
    persistence_adapter: PersistenceAdapter,
) -> None:
    workspace_id = synthetic_uuid(3214)
    environment_id = synthetic_uuid(3215)
    expected = profile(workspace_id, environment_id)
    await persist_scope(persistence_adapter, workspace_id, environment_id)
    context = persistence_adapter.environment_context(workspace_id, environment_id)

    async with persistence_adapter.uow_factory(context) as uow:
        first = await uow.context_profiles.create(expected)
        await uow.commit()

    async with persistence_adapter.uow_factory(context) as uow:
        replay = await uow.context_profiles.create(expected)
        await uow.commit()

    assert first == expected
    assert replay == expected


@pytest.mark.asyncio
async def test_context_profile_repository_retries_after_uncommitted_create_failure(
    persistence_adapter: PersistenceAdapter,
) -> None:
    workspace_id = synthetic_uuid(3218)
    environment_id = synthetic_uuid(3219)
    expected = profile(workspace_id, environment_id)
    await persist_scope(persistence_adapter, workspace_id, environment_id)
    context = persistence_adapter.environment_context(workspace_id, environment_id)

    with pytest.raises(RuntimeError, match="synthetic profile failure"):
        async with persistence_adapter.uow_factory(context) as uow:
            await uow.context_profiles.create(expected)
            raise RuntimeError("synthetic profile failure")

    async with persistence_adapter.uow_factory(context) as uow:
        actual = await uow.context_profiles.create(expected)
        await uow.commit()

    assert actual == expected


@pytest.mark.asyncio
async def test_context_profile_repository_preserves_allowed_partial_fallback(
    persistence_adapter: PersistenceAdapter,
) -> None:
    workspace_id = synthetic_uuid(3216)
    environment_id = synthetic_uuid(3217)
    expected = profile(
        workspace_id,
        environment_id,
        partial_answer_fallback=PartialAnswerFallback.ALLOWED,
    )
    await persist_scope(persistence_adapter, workspace_id, environment_id)
    context = persistence_adapter.environment_context(workspace_id, environment_id)

    async with persistence_adapter.uow_factory(context) as uow:
        await uow.context_profiles.create(expected)
        await uow.commit()

    async with persistence_adapter.uow_factory(context) as uow:
        actual = await uow.context_profiles.resolve(
            profile_id=expected.profile_id,
            profile_version=expected.profile_version,
            profile_digest=expected.profile_digest,
        )

    assert actual.partial_answer_fallback is PartialAnswerFallback.ALLOWED


@pytest.mark.asyncio
async def test_context_profile_repository_rejects_duplicate_immutable_version(
    persistence_adapter: PersistenceAdapter,
) -> None:
    workspace_id = synthetic_uuid(3204)
    environment_id = synthetic_uuid(3205)
    expected = profile(workspace_id, environment_id)
    await persist_scope(persistence_adapter, workspace_id, environment_id)
    context = persistence_adapter.environment_context(workspace_id, environment_id)

    async with persistence_adapter.uow_factory(context) as uow:
        await uow.context_profiles.add(expected)
        await uow.commit()

    with pytest.raises(ConstraintConflictError, match="immutable"):
        async with persistence_adapter.uow_factory(context) as uow:
            await uow.context_profiles.add(expected)


@pytest.mark.asyncio
async def test_context_profile_repository_fails_closed_for_digest_mismatch(
    persistence_adapter: PersistenceAdapter,
) -> None:
    workspace_id = synthetic_uuid(3206)
    environment_id = synthetic_uuid(3207)
    expected = profile(workspace_id, environment_id)
    await persist_scope(persistence_adapter, workspace_id, environment_id)
    context = persistence_adapter.environment_context(workspace_id, environment_id)

    async with persistence_adapter.uow_factory(context) as uow:
        await uow.context_profiles.add(expected)
        await uow.commit()

    with pytest.raises(ContextProfileDigestMismatchError):
        async with persistence_adapter.uow_factory(context) as uow:
            await uow.context_profiles.resolve(
                profile_id=expected.profile_id,
                profile_version=expected.profile_version,
                profile_digest="0" * 64,
            )


@pytest.mark.asyncio
async def test_context_profile_repository_fails_closed_for_unknown_version(
    persistence_adapter: PersistenceAdapter,
) -> None:
    workspace_id = synthetic_uuid(3220)
    environment_id = synthetic_uuid(3221)
    expected = profile(workspace_id, environment_id)
    await persist_scope(persistence_adapter, workspace_id, environment_id)
    context = persistence_adapter.environment_context(workspace_id, environment_id)

    async with persistence_adapter.uow_factory(context) as uow:
        await uow.context_profiles.add(expected)
        await uow.commit()

    with pytest.raises(ContextProfileNotFoundError):
        async with persistence_adapter.uow_factory(context) as uow:
            await uow.context_profiles.resolve(
                profile_id=expected.profile_id,
                profile_version=99,
                profile_digest=expected.profile_digest,
            )


@pytest.mark.asyncio
async def test_context_profile_repository_does_not_disclose_cross_workspace_profile(
    persistence_adapter: PersistenceAdapter,
) -> None:
    owner_workspace_id = synthetic_uuid(3208)
    owner_environment_id = synthetic_uuid(3209)
    other_workspace_id = synthetic_uuid(3210)
    other_environment_id = synthetic_uuid(3211)
    expected = profile(owner_workspace_id, owner_environment_id)
    await persist_scope(persistence_adapter, owner_workspace_id, owner_environment_id)
    await persist_scope(persistence_adapter, other_workspace_id, other_environment_id)

    owner_context = persistence_adapter.environment_context(
        owner_workspace_id, owner_environment_id
    )
    async with persistence_adapter.uow_factory(owner_context) as uow:
        await uow.context_profiles.add(expected)
        await uow.commit()

    with pytest.raises(ContextProfileNotFoundError) as raised:
        async with persistence_adapter.uow_factory(
            persistence_adapter.environment_context(other_workspace_id, other_environment_id)
        ) as uow:
            await uow.context_profiles.resolve(
                profile_id=expected.profile_id,
                profile_version=expected.profile_version,
                profile_digest=expected.profile_digest,
            )

    assert str(owner_workspace_id) not in str(raised.value)
    assert str(expected.profile_id) not in str(raised.value)


@pytest.mark.asyncio
async def test_context_profile_repository_rejects_revoked_profile(
    persistence_adapter: PersistenceAdapter,
) -> None:
    workspace_id = synthetic_uuid(3212)
    environment_id = synthetic_uuid(3213)
    expected = profile(workspace_id, environment_id, revoked=True)
    await persist_scope(persistence_adapter, workspace_id, environment_id)
    context = persistence_adapter.environment_context(workspace_id, environment_id)

    async with persistence_adapter.uow_factory(context) as uow:
        await uow.context_profiles.add(expected)
        await uow.commit()

    with pytest.raises(ContextProfileRevokedError):
        async with persistence_adapter.uow_factory(context) as uow:
            await uow.context_profiles.resolve(
                profile_id=expected.profile_id,
                profile_version=expected.profile_version,
                profile_digest=expected.profile_digest,
            )
