from __future__ import annotations

from uuid import UUID

import pytest

from spine.application.document_ingestion import (
    PROJECTION_REQUEST_EVENT_TYPE,
    PROJECTION_REQUEST_SCHEMA_VERSION,
    ProjectionRequestPayload,
)
from spine.application.persistence.errors import (
    InvalidPersistenceContextError,
    OutboxConflictError,
    UnitOfWorkLifecycleError,
)
from spine.application.persistence.outbox import OpaqueObjectReference
from spine.domain.common import EnvironmentKind
from spine.domain.workspaces import Environment, Workspace

from .adapter import PersistenceAdapter
from .ids import synthetic_uuid
from .outbox_events import WorkspaceCreatedPayload
from .test_idempotency_repository_contract import persist_environment, persist_workspace


def workspace(workspace_id: UUID) -> Workspace:
    return Workspace(
        id=workspace_id,
        slug=f"outbox-{workspace_id.hex[-12:]}",
        display_name="Outbox Workspace",
    )


def intent(
    adapter: PersistenceAdapter,
    *,
    workspace_id: UUID,
    event_id: UUID | None,
    environment_id: UUID | None = None,
    producer_deduplication_id: str | None = "workspace:create:v1",
    trace_id: UUID,
):
    return adapter.outbox_events.build_intent(
        event_id=event_id,
        workspace_id=workspace_id,
        environment_id=environment_id,
        event_type="workspace.created",
        schema_version=1,
        payload=WorkspaceCreatedPayload(
            workspace=OpaqueObjectReference(
                object_type="workspace",
                object_id=workspace_id,
                schema_version=1,
            ),
            lifecycle_state="active",
        ),
        producer_deduplication_id=producer_deduplication_id,
        trace_id=trace_id,
    )


def projection_intent(
    adapter: PersistenceAdapter,
    *,
    workspace_id: UUID,
    environment_id: UUID,
    event_id: UUID,
    item_id: UUID,
    revision_id: UUID,
    trace_id: UUID,
):
    return adapter.outbox_events.build_intent(
        event_id=event_id,
        workspace_id=workspace_id,
        environment_id=environment_id,
        event_type=PROJECTION_REQUEST_EVENT_TYPE,
        schema_version=PROJECTION_REQUEST_SCHEMA_VERSION,
        payload=ProjectionRequestPayload(
            ingestion_run_id=synthetic_uuid(2140),
            ingestion_run_source_id=item_id,
            source_revision_id=revision_id,
            processing_receipt_id=synthetic_uuid(2141),
            processor_kind="document-ingestion",
            parser_version="text-markdown:v1",
            parser_config_reference="text-markdown-default:v1",
            locator_contract_version="physical-line-codepoint:v1",
        ),
        producer_deduplication_id=f"projection-request:{item_id}",
        trace_id=trace_id,
        correlation_id=synthetic_uuid(2140),
        causation_id=synthetic_uuid(2142),
    )


@pytest.mark.asyncio
async def test_canonical_mutation_and_intent_become_visible_only_after_commit(
    persistence_adapter: PersistenceAdapter,
) -> None:
    workspace_id = synthetic_uuid(2101)
    event_id = synthetic_uuid(2102)
    context = persistence_adapter.workspace_context(workspace_id)
    expected_workspace = workspace(workspace_id)
    expected_intent = intent(
        persistence_adapter,
        workspace_id=workspace_id,
        event_id=event_id,
        trace_id=context.trace_id,
        producer_deduplication_id=None,
    )

    async with persistence_adapter.uow_factory(context) as uow:
        await uow.workspaces.add(expected_workspace)
        assert await uow.outbox.append(expected_intent) == event_id
        await uow.commit()

    async with persistence_adapter.uow_factory(context) as reader:
        assert await reader.workspaces.resolve(workspace_id) == expected_workspace
        with pytest.raises(OutboxConflictError, match="already exists"):
            await reader.outbox.append(expected_intent)


@pytest.mark.asyncio
async def test_missing_event_identity_is_generated_and_returned(
    persistence_adapter: PersistenceAdapter,
) -> None:
    workspace_id = synthetic_uuid(2114)
    context = persistence_adapter.workspace_context(workspace_id)
    expected_intent = intent(
        persistence_adapter,
        workspace_id=workspace_id,
        event_id=None,
        trace_id=context.trace_id,
        producer_deduplication_id=None,
    )

    async with persistence_adapter.uow_factory(context) as uow:
        await uow.workspaces.add(workspace(workspace_id))
        event_id = await uow.outbox.append(expected_intent)
        await uow.commit()

    assert isinstance(event_id, UUID)
    assert event_id.int != 0


@pytest.mark.asyncio
async def test_trace_mismatch_is_terminal_and_discards_pending_mutation(
    persistence_adapter: PersistenceAdapter,
) -> None:
    workspace_id = synthetic_uuid(2115)
    context = persistence_adapter.workspace_context(workspace_id)
    mismatched = intent(
        persistence_adapter,
        workspace_id=workspace_id,
        event_id=synthetic_uuid(2116),
        trace_id=synthetic_uuid(2117),
    )

    async with persistence_adapter.uow_factory(context) as uow:
        await uow.workspaces.add(workspace(workspace_id))
        with pytest.raises(InvalidPersistenceContextError, match="trace"):
            await uow.outbox.append(mismatched)
        with pytest.raises(UnitOfWorkLifecycleError, match="not active"):
            await uow.commit()

    async with persistence_adapter.uow_factory(context) as reader:
        assert await reader.workspaces.resolve(workspace_id) is None


@pytest.mark.asyncio
async def test_rollback_discards_canonical_mutation_and_intent(
    persistence_adapter: PersistenceAdapter,
) -> None:
    workspace_id = synthetic_uuid(2103)
    event_id = synthetic_uuid(2104)
    context = persistence_adapter.workspace_context(workspace_id)
    expected_intent = intent(
        persistence_adapter,
        workspace_id=workspace_id,
        event_id=event_id,
        trace_id=context.trace_id,
    )

    async with persistence_adapter.uow_factory(context) as uow:
        await uow.workspaces.add(workspace(workspace_id))
        await uow.outbox.append(expected_intent)
        await uow.rollback()

    async with persistence_adapter.uow_factory(context) as retry:
        assert await retry.workspaces.resolve(workspace_id) is None
        await retry.workspaces.add(workspace(workspace_id))
        await retry.outbox.append(expected_intent)
        await retry.rollback()


@pytest.mark.asyncio
async def test_intent_scope_must_exactly_match_unit_of_work_scope(
    persistence_adapter: PersistenceAdapter,
) -> None:
    workspace_id = synthetic_uuid(2105)
    other_workspace_id = synthetic_uuid(2106)
    context = persistence_adapter.workspace_context(workspace_id)

    with pytest.raises(InvalidPersistenceContextError, match="scope"):
        async with persistence_adapter.uow_factory(context) as uow:
            await uow.outbox.append(
                intent(
                    persistence_adapter,
                    workspace_id=other_workspace_id,
                    event_id=synthetic_uuid(2107),
                    trace_id=context.trace_id,
                )
            )


@pytest.mark.asyncio
async def test_environment_intent_requires_matching_environment_scope(
    persistence_adapter: PersistenceAdapter,
) -> None:
    workspace_id = synthetic_uuid(2108)
    environment_id = synthetic_uuid(2109)
    workspace_context = persistence_adapter.workspace_context(workspace_id)
    async with persistence_adapter.uow_factory(workspace_context) as uow:
        await uow.workspaces.add(workspace(workspace_id))
        await uow.commit()
    async with persistence_adapter.uow_factory(workspace_context) as uow:
        await uow.environments.add(
            Environment(
                id=environment_id,
                workspace_id=workspace_id,
                kind=EnvironmentKind.PRODUCTION,
                display_name="Production",
            )
        )
        await uow.commit()

    environment_context = persistence_adapter.environment_context(
        workspace_id,
        environment_id,
    )
    expected_intent = intent(
        persistence_adapter,
        workspace_id=workspace_id,
        environment_id=environment_id,
        event_id=synthetic_uuid(2110),
        trace_id=environment_context.trace_id,
    )
    async with persistence_adapter.uow_factory(environment_context) as uow:
        await uow.outbox.append(expected_intent)
        await uow.commit()


@pytest.mark.asyncio
async def test_duplicate_producer_identity_has_stable_conflict(
    persistence_adapter: PersistenceAdapter,
) -> None:
    workspace_id = synthetic_uuid(2111)
    context = persistence_adapter.workspace_context(workspace_id)
    async with persistence_adapter.uow_factory(context) as uow:
        await uow.workspaces.add(workspace(workspace_id))
        await uow.commit()
    first = intent(
        persistence_adapter,
        workspace_id=workspace_id,
        event_id=synthetic_uuid(2112),
        producer_deduplication_id="workspace:create:stable",
        trace_id=context.trace_id,
    )
    duplicate = intent(
        persistence_adapter,
        workspace_id=workspace_id,
        event_id=synthetic_uuid(2113),
        producer_deduplication_id="workspace:create:stable",
        trace_id=context.trace_id,
    )

    async with persistence_adapter.uow_factory(context) as uow:
        await uow.outbox.append(first)
        await uow.commit()

    with pytest.raises(OutboxConflictError, match="producer identity"):
        async with persistence_adapter.uow_factory(context) as uow:
            await uow.outbox.append(duplicate)


@pytest.mark.asyncio
async def test_changed_projection_digest_conflicts_on_stable_item_identity(
    persistence_adapter: PersistenceAdapter,
) -> None:
    workspace_id = synthetic_uuid(2120)
    environment_id = synthetic_uuid(2121)
    item_id = synthetic_uuid(2122)
    await persist_workspace(persistence_adapter, workspace_id)
    await persist_environment(persistence_adapter, workspace_id, environment_id)
    context = persistence_adapter.environment_context(workspace_id, environment_id)
    first = projection_intent(
        persistence_adapter,
        workspace_id=workspace_id,
        environment_id=environment_id,
        event_id=synthetic_uuid(2123),
        item_id=item_id,
        revision_id=synthetic_uuid(2124),
        trace_id=context.trace_id,
    )
    changed_digest = projection_intent(
        persistence_adapter,
        workspace_id=workspace_id,
        environment_id=environment_id,
        event_id=synthetic_uuid(2125),
        item_id=item_id,
        revision_id=synthetic_uuid(2126),
        trace_id=context.trace_id,
    )
    async with persistence_adapter.uow_factory(context) as uow:
        await uow.outbox.append(first)
        await uow.commit()

    with pytest.raises(OutboxConflictError, match="producer identity"):
        async with persistence_adapter.uow_factory(context) as uow:
            await uow.outbox.append(changed_digest)


@pytest.mark.asyncio
async def test_projection_request_cannot_cross_environment_scope(
    persistence_adapter: PersistenceAdapter,
) -> None:
    workspace_id = synthetic_uuid(2130)
    environment_id = synthetic_uuid(2131)
    other_environment_id = synthetic_uuid(2132)
    await persist_workspace(persistence_adapter, workspace_id)
    await persist_environment(persistence_adapter, workspace_id, environment_id)
    await persist_environment(persistence_adapter, workspace_id, other_environment_id)
    other_context = persistence_adapter.environment_context(
        workspace_id, other_environment_id
    )
    protected = projection_intent(
        persistence_adapter,
        workspace_id=workspace_id,
        environment_id=environment_id,
        event_id=synthetic_uuid(2133),
        item_id=synthetic_uuid(2134),
        revision_id=synthetic_uuid(2135),
        trace_id=other_context.trace_id,
    )

    with pytest.raises(InvalidPersistenceContextError, match="scope"):
        async with persistence_adapter.uow_factory(other_context) as uow:
            await uow.outbox.append(protected)
