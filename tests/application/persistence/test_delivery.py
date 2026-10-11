from __future__ import annotations

from datetime import datetime, timedelta, timezone
import pytest
from pydantic import ValidationError

from spine.application.persistence.delivery import (
    ClaimedOutboxDelivery,
    DuplicateDeliveryRouteError,
    OutboxDelivery,
    OutboxDeliveryRouteRegistry,
    OutboxDeliveryStatus,
    UnsupportedDeliveryRouteError,
)
from spine.application.persistence.outbox import OutboxEventRegistry
from tests.contracts.persistence.ids import synthetic_uuid
from tests.contracts.persistence.outbox_events import (
    WorkspaceCreatedPayload,
    create_outbox_event_registry,
)
from spine.application.persistence.outbox import OpaqueObjectReference


def test_registered_event_resolves_to_one_explicit_consumer() -> None:
    registry = OutboxDeliveryRouteRegistry()
    registry.register(
        event_type="workspace.created",
        schema_version=1,
        consumer_name="workspace_projection",
    )

    route = registry.resolve("workspace.created", 1)

    assert route.event_type == "workspace.created"
    assert route.schema_version == 1
    assert route.consumer_name == "workspace_projection"


def test_duplicate_event_route_is_rejected_deterministically() -> None:
    registry = OutboxDeliveryRouteRegistry()
    registry.register(
        event_type="workspace.created",
        schema_version=1,
        consumer_name="workspace_projection",
    )

    with pytest.raises(DuplicateDeliveryRouteError, match="already registered"):
        registry.register(
            event_type="workspace.created",
            schema_version=1,
            consumer_name="another_consumer",
        )


def test_unknown_event_route_is_reported_without_payload_detail() -> None:
    registry = OutboxDeliveryRouteRegistry()

    with pytest.raises(
        UnsupportedDeliveryRouteError,
        match="Outbox delivery route is not registered",
    ) as captured:
        registry.resolve("protected.document.secret", 99)

    assert "protected.document.secret" not in str(captured.value)


def _intent(registry: OutboxEventRegistry):
    workspace_id = synthetic_uuid(6501)
    return registry.build_intent(
        event_id=synthetic_uuid(6502),
        workspace_id=workspace_id,
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
        trace_id=synthetic_uuid(6503),
    )


def test_claimed_delivery_is_an_immutable_validated_snapshot() -> None:
    claimed_at = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)
    intent = _intent(create_outbox_event_registry())
    claim = ClaimedOutboxDelivery(
        intent=intent,
        consumer_name="workspace_projection",
        worker_identity="worker_alpha",
        lease_token=synthetic_uuid(6504),
        claimed_at=claimed_at,
        lease_expires_at=claimed_at + timedelta(seconds=30),
        attempt_number=1,
        delivery_version=1,
    )

    assert claim.intent.event_id == synthetic_uuid(6502)
    assert claim.lease_token == synthetic_uuid(6504)
    with pytest.raises(ValidationError):
        claim.attempt_number = 2  # type: ignore[misc]


def test_delivery_state_rejects_raw_errors_and_incomplete_lease_state() -> None:
    now = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)
    common = {
        "event_id": synthetic_uuid(6510),
        "workspace_id": synthetic_uuid(6511),
        "environment_id": None,
        "consumer_name": "workspace_projection",
        "attempt_count": 1,
        "next_attempt_at": None,
        "delivered_at": None,
        "quarantined_at": None,
        "version": 1,
    }

    with pytest.raises(ValidationError):
        OutboxDelivery(
            **common,
            status=OutboxDeliveryStatus.IN_FLIGHT,
            lease_owner="worker_alpha",
            lease_token=synthetic_uuid(6512),
            lease_expires_at=None,
            last_error_code=None,
        )

    with pytest.raises(ValidationError):
        OutboxDelivery(
            **(common | {"next_attempt_at": now}),
            status=OutboxDeliveryStatus.RETRY_SCHEDULED,
            lease_owner=None,
            lease_token=None,
            lease_expires_at=None,
            last_error_code="provider failed: protected payload",
        )

    assert "payload" not in OutboxDelivery.model_fields
    assert "exception" not in OutboxDelivery.model_fields
