from __future__ import annotations

from uuid import UUID

from spine.application.persistence.idempotency import IdempotencyKey
from spine.application.persistence.projection import (
    PROJECTION_ACTIVATED_OUTBOX_EVENT_TYPE,
    PROJECTION_CATCH_UP_OUTBOX_EVENT_TYPE,
    ProjectionActivationCommand,
    ProjectionActivationKind,
    register_projection_outbox_events,
)
from spine.application.persistence.outbox import OutboxEventRegistry


def test_activation_command_digest_covers_all_compare_and_swap_claims() -> None:
    base = dict(
        projection_kind="qa",
        projection_snapshot_id=UUID(int=1),
        activation_kind=ProjectionActivationKind.PUBLISH,
        expected_predecessor_snapshot_id=UUID(int=2),
        expected_predecessor_activation_generation=4,
        expected_active_pointer_cas_version=7,
        command_identity="activation-1",
        idempotency_key=IdempotencyKey("activation-1"),
    )

    first = ProjectionActivationCommand.create(**base)
    changed = ProjectionActivationCommand.create(
        **{**base, "expected_active_pointer_cas_version": 8}
    )

    assert first.digest.operation.value == "projection_activation"
    assert first.digest.value != changed.digest.value


def test_projection_outbox_schemas_are_allowlisted_and_frozen() -> None:
    registry = OutboxEventRegistry()
    register_projection_outbox_events(registry)

    assert registry.resolve(PROJECTION_ACTIVATED_OUTBOX_EVENT_TYPE, 1)
    assert registry.resolve(PROJECTION_CATCH_UP_OUTBOX_EVENT_TYPE, 1)
