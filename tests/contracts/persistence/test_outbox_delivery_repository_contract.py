from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from spine.application.persistence.delivery import (
    OutboxDeliveryStatus,
    StaleDeliveryLeaseError,
)
from spine.domain.common import EnvironmentKind
from spine.domain.workspaces import Environment

from .adapter import DeliveryPersistenceAdapter
from .ids import synthetic_uuid
from .test_outbox_writer_contract import intent, workspace


@pytest.mark.asyncio
async def test_materialize_creates_bounded_pending_control_state(
    delivery_persistence_adapter: DeliveryPersistenceAdapter,
) -> None:
    adapter = delivery_persistence_adapter
    workspace_id = synthetic_uuid(6518)
    context = adapter.workspace_context(workspace_id)
    async with adapter.uow_factory(context) as uow:
        await uow.workspaces.add(workspace(workspace_id))
        for event_id in (synthetic_uuid(6519), synthetic_uuid(6520)):
            await uow.outbox.append(
                intent(
                    adapter,
                    workspace_id=workspace_id,
                    event_id=event_id,
                    trace_id=context.trace_id,
                    producer_deduplication_id=None,
                )
            )
        await uow.commit()
    materialized_at = datetime(2026, 10, 11, 11, tzinfo=timezone.utc)

    first = await adapter.delivery_repository.materialize(
        limit=1,
        materialized_at=materialized_at,
    )

    assert len(first) == 1
    assert first[0].status is OutboxDeliveryStatus.PENDING
    assert first[0].attempt_count == 0
    assert first[0].next_attempt_at == materialized_at
    assert await adapter.delivery_repository.materialize(
        limit=1,
        materialized_at=materialized_at,
    ) != ()
    assert await adapter.delivery_repository.materialize(
        limit=1,
        materialized_at=materialized_at,
    ) == ()


@pytest.mark.asyncio
async def test_claim_commits_a_bounded_lease_before_delivery(
    delivery_persistence_adapter: DeliveryPersistenceAdapter,
) -> None:
    adapter = delivery_persistence_adapter
    workspace_id = synthetic_uuid(6520)
    event_id = synthetic_uuid(6521)
    context = adapter.workspace_context(workspace_id)
    expected_intent = intent(
        adapter,
        workspace_id=workspace_id,
        event_id=event_id,
        trace_id=context.trace_id,
        producer_deduplication_id=None,
    )
    async with adapter.uow_factory(context) as uow:
        await uow.workspaces.add(workspace(workspace_id))
        await uow.outbox.append(expected_intent)
        await uow.commit()

    claimed_at = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)
    claims = await adapter.delivery_repository.claim_due(
        limit=1,
        worker_identity="worker_alpha",
        claimed_at=claimed_at,
        lease_duration=timedelta(seconds=30),
    )

    assert len(claims) == 1
    claim = claims[0]
    assert claim.intent == expected_intent
    assert claim.consumer_name == "workspace_projection"
    assert claim.attempt_number == 1
    persisted = await adapter.delivery_repository.resolve(event_id)
    assert persisted is not None
    assert persisted.status is OutboxDeliveryStatus.IN_FLIGHT
    assert persisted.lease_token == claim.lease_token


@pytest.mark.asyncio
async def test_acknowledge_requires_the_active_lease_and_is_terminal(
    delivery_persistence_adapter: DeliveryPersistenceAdapter,
) -> None:
    adapter = delivery_persistence_adapter
    workspace_id = synthetic_uuid(6522)
    event_id = synthetic_uuid(6523)
    context = adapter.workspace_context(workspace_id)
    async with adapter.uow_factory(context) as uow:
        await uow.workspaces.add(workspace(workspace_id))
        await uow.outbox.append(
            intent(
                adapter,
                workspace_id=workspace_id,
                event_id=event_id,
                trace_id=context.trace_id,
                producer_deduplication_id=None,
            )
        )
        await uow.commit()
    claimed_at = datetime(2026, 10, 11, 13, tzinfo=timezone.utc)
    claim = (
        await adapter.delivery_repository.claim_due(
            limit=1,
            worker_identity="worker_alpha",
            claimed_at=claimed_at,
            lease_duration=timedelta(seconds=30),
        )
    )[0]

    delivered = await adapter.delivery_repository.acknowledge(
        claim,
        completed_at=claimed_at + timedelta(seconds=1),
    )

    assert delivered.status is OutboxDeliveryStatus.DELIVERED
    assert delivered.delivered_at == claimed_at + timedelta(seconds=1)
    assert delivered.lease_token is None
    assert delivered.version == 2
    with pytest.raises(StaleDeliveryLeaseError, match="not active"):
        await adapter.delivery_repository.acknowledge(
            claim,
            completed_at=claimed_at + timedelta(seconds=2),
        )


@pytest.mark.asyncio
async def test_retry_is_claimable_only_at_its_due_boundary(
    delivery_persistence_adapter: DeliveryPersistenceAdapter,
) -> None:
    adapter = delivery_persistence_adapter
    workspace_id = synthetic_uuid(6524)
    context = adapter.workspace_context(workspace_id)
    async with adapter.uow_factory(context) as uow:
        await uow.workspaces.add(workspace(workspace_id))
        await uow.outbox.append(
            intent(
                adapter,
                workspace_id=workspace_id,
                event_id=synthetic_uuid(6525),
                trace_id=context.trace_id,
                producer_deduplication_id=None,
            )
        )
        await uow.commit()
    claimed_at = datetime(2026, 10, 11, 14, tzinfo=timezone.utc)
    first = (
        await adapter.delivery_repository.claim_due(
            limit=1,
            worker_identity="worker_alpha",
            claimed_at=claimed_at,
            lease_duration=timedelta(seconds=30),
        )
    )[0]
    due_at = claimed_at + timedelta(seconds=10)

    retry = await adapter.delivery_repository.reschedule(
        first,
        failed_at=claimed_at + timedelta(seconds=1),
        next_attempt_at=due_at,
        error_code="consumer.unavailable",
    )

    assert retry.status is OutboxDeliveryStatus.RETRY_SCHEDULED
    assert retry.last_error_code == "consumer.unavailable"
    assert await adapter.delivery_repository.claim_due(
        limit=1,
        worker_identity="worker_beta",
        claimed_at=due_at - timedelta(microseconds=1),
        lease_duration=timedelta(seconds=30),
    ) == ()
    second = (
        await adapter.delivery_repository.claim_due(
            limit=1,
            worker_identity="worker_beta",
            claimed_at=due_at,
            lease_duration=timedelta(seconds=30),
        )
    )[0]
    assert second.attempt_number == 2
    assert second.lease_token != first.lease_token


@pytest.mark.asyncio
async def test_expired_lease_is_reclaimed_and_stale_worker_cannot_finalize(
    delivery_persistence_adapter: DeliveryPersistenceAdapter,
) -> None:
    adapter = delivery_persistence_adapter
    workspace_id = synthetic_uuid(6526)
    context = adapter.workspace_context(workspace_id)
    async with adapter.uow_factory(context) as uow:
        await uow.workspaces.add(workspace(workspace_id))
        await uow.outbox.append(
            intent(
                adapter,
                workspace_id=workspace_id,
                event_id=synthetic_uuid(6527),
                trace_id=context.trace_id,
                producer_deduplication_id=None,
            )
        )
        await uow.commit()
    claimed_at = datetime(2026, 10, 11, 15, tzinfo=timezone.utc)
    first = (
        await adapter.delivery_repository.claim_due(
            limit=1,
            worker_identity="worker_alpha",
            claimed_at=claimed_at,
            lease_duration=timedelta(seconds=30),
        )
    )[0]

    second = (
        await adapter.delivery_repository.claim_due(
            limit=1,
            worker_identity="worker_beta",
            claimed_at=first.lease_expires_at,
            lease_duration=timedelta(seconds=30),
        )
    )[0]

    assert second.attempt_number == 2
    assert second.worker_identity == "worker_beta"
    with pytest.raises(StaleDeliveryLeaseError, match="not active"):
        await adapter.delivery_repository.acknowledge(
            first,
            completed_at=first.lease_expires_at - timedelta(microseconds=1),
        )
    delivered = await adapter.delivery_repository.acknowledge(
        second,
        completed_at=second.claimed_at + timedelta(seconds=1),
    )
    assert delivered.status is OutboxDeliveryStatus.DELIVERED


@pytest.mark.asyncio
async def test_unknown_route_can_be_claimed_and_safely_quarantined(
    delivery_persistence_adapter: DeliveryPersistenceAdapter,
) -> None:
    adapter = delivery_persistence_adapter
    adapter.outbox_events.register(
        event_type="workspace.unrouted",
        schema_version=1,
        payload_type=type(
            intent(
                adapter,
                workspace_id=synthetic_uuid(6528),
                event_id=synthetic_uuid(6529),
                trace_id=synthetic_uuid(6530),
            ).payload
        ),
    )
    workspace_id = synthetic_uuid(6531)
    context = adapter.workspace_context(workspace_id)
    unrouted = adapter.outbox_events.build_intent(
        event_id=synthetic_uuid(6532),
        workspace_id=workspace_id,
        event_type="workspace.unrouted",
        schema_version=1,
        payload=intent(
            adapter,
            workspace_id=workspace_id,
            event_id=synthetic_uuid(6533),
            trace_id=context.trace_id,
        ).payload,
        trace_id=context.trace_id,
    )
    async with adapter.uow_factory(context) as uow:
        await uow.workspaces.add(workspace(workspace_id))
        await uow.outbox.append(unrouted)
        await uow.commit()
    claimed_at = datetime(2026, 10, 11, 16, tzinfo=timezone.utc)
    claim = (
        await adapter.delivery_repository.claim_due(
            limit=1,
            worker_identity="worker_alpha",
            claimed_at=claimed_at,
            lease_duration=timedelta(seconds=30),
        )
    )[0]

    assert claim.consumer_name is None
    quarantined = await adapter.delivery_repository.quarantine(
        claim,
        quarantined_at=claimed_at + timedelta(seconds=1),
        error_code="delivery.unknown_route",
    )

    assert quarantined.status is OutboxDeliveryStatus.QUARANTINED
    assert quarantined.last_error_code == "delivery.unknown_route"
    assert await adapter.delivery_repository.claim_due(
        limit=1,
        worker_identity="worker_beta",
        claimed_at=claimed_at + timedelta(minutes=5),
        lease_duration=timedelta(seconds=30),
    ) == ()


@pytest.mark.asyncio
async def test_renew_rotates_lease_authority_before_expiry(
    delivery_persistence_adapter: DeliveryPersistenceAdapter,
) -> None:
    adapter = delivery_persistence_adapter
    workspace_id = synthetic_uuid(6534)
    context = adapter.workspace_context(workspace_id)
    async with adapter.uow_factory(context) as uow:
        await uow.workspaces.add(workspace(workspace_id))
        await uow.outbox.append(
            intent(
                adapter,
                workspace_id=workspace_id,
                event_id=synthetic_uuid(6535),
                trace_id=context.trace_id,
                producer_deduplication_id=None,
            )
        )
        await uow.commit()
    claimed_at = datetime(2026, 10, 11, 17, tzinfo=timezone.utc)
    first = (
        await adapter.delivery_repository.claim_due(
            limit=1,
            worker_identity="worker_alpha",
            claimed_at=claimed_at,
            lease_duration=timedelta(seconds=30),
        )
    )[0]

    renewed = await adapter.delivery_repository.renew(
        first,
        renewed_at=claimed_at + timedelta(seconds=10),
        lease_duration=timedelta(seconds=30),
    )

    assert renewed.lease_token != first.lease_token
    assert renewed.delivery_version == first.delivery_version + 1
    assert renewed.lease_expires_at == claimed_at + timedelta(seconds=40)
    with pytest.raises(StaleDeliveryLeaseError, match="not active"):
        await adapter.delivery_repository.acknowledge(
            first,
            completed_at=claimed_at + timedelta(seconds=11),
        )


@pytest.mark.asyncio
async def test_concurrent_workers_cannot_claim_the_same_delivery(
    delivery_persistence_adapter: DeliveryPersistenceAdapter,
) -> None:
    adapter = delivery_persistence_adapter
    workspace_id = synthetic_uuid(6536)
    context = adapter.workspace_context(workspace_id)
    async with adapter.uow_factory(context) as uow:
        await uow.workspaces.add(workspace(workspace_id))
        await uow.outbox.append(
            intent(
                adapter,
                workspace_id=workspace_id,
                event_id=synthetic_uuid(6537),
                trace_id=context.trace_id,
                producer_deduplication_id=None,
            )
        )
        await uow.commit()
    now = datetime(2026, 10, 11, 18, tzinfo=timezone.utc)

    results = await asyncio.gather(
        adapter.delivery_repository.claim_due(
            limit=1,
            worker_identity="worker_alpha",
            claimed_at=now,
            lease_duration=timedelta(seconds=30),
        ),
        adapter.delivery_repository.claim_due(
            limit=1,
            worker_identity="worker_beta",
            claimed_at=now,
            lease_duration=timedelta(seconds=30),
        ),
    )

    claims = results[0] + results[1]
    assert len(claims) == 1
    assert claims[0].intent.event_id == synthetic_uuid(6537)


@pytest.mark.asyncio
async def test_claim_preserves_workspace_environment_and_trace_scope(
    delivery_persistence_adapter: DeliveryPersistenceAdapter,
) -> None:
    adapter = delivery_persistence_adapter
    workspace_id = synthetic_uuid(6538)
    environment_id = synthetic_uuid(6539)
    workspace_context = adapter.workspace_context(workspace_id)
    async with adapter.uow_factory(workspace_context) as uow:
        await uow.workspaces.add(workspace(workspace_id))
        await uow.commit()
    async with adapter.uow_factory(workspace_context) as uow:
        await uow.environments.add(
            Environment(
                id=environment_id,
                workspace_id=workspace_id,
                kind=EnvironmentKind.PRODUCTION,
                display_name="Production",
            )
        )
        await uow.commit()
    environment_context = adapter.environment_context(workspace_id, environment_id)
    expected = intent(
        adapter,
        workspace_id=workspace_id,
        environment_id=environment_id,
        event_id=synthetic_uuid(6547),
        trace_id=environment_context.trace_id,
        producer_deduplication_id=None,
    )
    async with adapter.uow_factory(environment_context) as uow:
        await uow.outbox.append(expected)
        await uow.commit()

    claim = (
        await adapter.delivery_repository.claim_due(
            limit=1,
            worker_identity="worker_alpha",
            claimed_at=datetime(2026, 10, 11, 19, tzinfo=timezone.utc),
            lease_duration=timedelta(seconds=30),
        )
    )[0]

    assert claim.intent.workspace_id == workspace_id
    assert claim.intent.environment_id == environment_id
    assert claim.intent.trace_id == environment_context.trace_id
