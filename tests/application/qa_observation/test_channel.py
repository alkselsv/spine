from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from spine.application.qa_observation.channel import (
    CaptureDisposition,
    CaptureReceiveState,
)
from spine.application.qa_observation.contracts import CaptureEnvelope
from spine.application.testing.qa_observation import InMemoryCaptureChannel
from tests.application.qa_observation.factories import (
    capture_envelope,
    execution_identity,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 18, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now


@pytest.mark.asyncio
async def test_publish_before_bind_is_buffered_then_released_after_exact_bind() -> None:
    clock = FakeClock()
    channel = InMemoryCaptureChannel(clock=clock)
    capture_id = UUID("c0000000-0000-0000-0000-000000000471")
    identity = execution_identity()
    subscription = await channel.register(
        capture_id=capture_id, ttl=timedelta(minutes=1)
    )
    envelope = capture_envelope(subscription_id=subscription.subscription_id)

    assert await channel.publish(envelope) is CaptureDisposition.BUFFERED
    assert (
        await channel.receive(
            subscription_id=subscription.subscription_id,
            identity=identity,
            timeout=timedelta(seconds=1),
        )
    ).state is CaptureReceiveState.PENDING
    assert (
        await channel.bind(
            subscription_id=subscription.subscription_id,
            identity=identity,
        )
        is CaptureDisposition.BOUND
    )
    received = await channel.receive(
        subscription_id=subscription.subscription_id,
        identity=identity,
        timeout=timedelta(seconds=1),
    )

    assert received.state is CaptureReceiveState.CAPTURED
    assert received.capture == envelope


@pytest.mark.asyncio
async def test_registration_retry_reuses_same_active_subscription() -> None:
    clock = FakeClock()
    channel = InMemoryCaptureChannel(clock=clock)
    capture_id = UUID("c1000000-0000-0000-0000-000000000471")

    first = await channel.register(capture_id=capture_id, ttl=timedelta(minutes=1))
    replay = await channel.register(capture_id=capture_id, ttl=timedelta(minutes=1))

    assert replay == first


@pytest.mark.asyncio
async def test_duplicate_publish_is_idempotent_and_acknowledgement_is_one_shot() -> (
    None
):
    clock = FakeClock()
    channel = InMemoryCaptureChannel(clock=clock)
    envelope = capture_envelope()
    subscription = await channel.register(
        capture_id=envelope.capture_id,
        ttl=timedelta(minutes=1),
    )
    envelope = CaptureEnvelope.model_validate(
        envelope.model_dump(mode="python")
        | {"subscription_id": subscription.subscription_id}
    )
    identity = execution_identity()
    await channel.bind(subscription_id=subscription.subscription_id, identity=identity)
    assert await channel.publish(envelope) is CaptureDisposition.BUFFERED
    assert await channel.publish(envelope) is CaptureDisposition.DUPLICATE

    first = await channel.receive(
        subscription_id=subscription.subscription_id,
        identity=identity,
        timeout=timedelta(seconds=1),
    )
    second = await channel.receive(
        subscription_id=subscription.subscription_id,
        identity=identity,
        timeout=timedelta(seconds=1),
    )
    assert first.state is CaptureReceiveState.CAPTURED
    assert second.state is CaptureReceiveState.PENDING
    assert (
        await channel.acknowledge(
            subscription_id=subscription.subscription_id,
            capture_id=envelope.capture_id,
            identity=identity,
        )
        is CaptureDisposition.ACKNOWLEDGED
    )
    assert (
        await channel.acknowledge(
            subscription_id=subscription.subscription_id,
            capture_id=envelope.capture_id,
            identity=identity,
        )
        is CaptureDisposition.ALREADY_CONSUMED
    )


@pytest.mark.asyncio
async def test_subscription_expires_and_drops_volatile_capture() -> None:
    clock = FakeClock()
    channel = InMemoryCaptureChannel(clock=clock)
    envelope = capture_envelope()
    subscription = await channel.register(
        capture_id=envelope.capture_id,
        ttl=timedelta(seconds=2),
    )
    envelope = CaptureEnvelope.model_validate(
        envelope.model_dump(mode="python")
        | {"subscription_id": subscription.subscription_id}
    )
    await channel.publish(envelope)
    clock.now += timedelta(seconds=2)

    assert (
        await channel.bind(
            subscription_id=subscription.subscription_id,
            identity=execution_identity(),
        )
        is CaptureDisposition.EXPIRED
    )
    received = await channel.receive(
        subscription_id=subscription.subscription_id,
        identity=execution_identity(),
        timeout=timedelta(seconds=1),
    )
    assert received.state is CaptureReceiveState.EXPIRED


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("command_id", UUID("e1000000-0000-0000-0000-000000000471")),
        ("run_id", UUID("e2000000-0000-0000-0000-000000000471")),
        ("session_id", UUID("e3000000-0000-0000-0000-000000000471")),
        ("message_id", UUID("e4000000-0000-0000-0000-000000000471")),
        ("workspace_id", UUID("e5000000-0000-0000-0000-000000000471")),
        ("environment_id", UUID("e6000000-0000-0000-0000-000000000471")),
        (
            "requester",
            execution_identity().requester.model_copy(
                update={"id": UUID("e7000000-0000-0000-0000-000000000471")}
            ),
        ),
        ("attempt_id", UUID("e8000000-0000-0000-0000-000000000471")),
        ("attempt_number", 2),
    ],
)
@pytest.mark.asyncio
async def test_capture_cannot_be_bound_to_substituted_full_identity(
    field: str, value: object
) -> None:
    clock = FakeClock()
    channel = InMemoryCaptureChannel(clock=clock)
    envelope = capture_envelope()
    subscription = await channel.register(
        capture_id=envelope.capture_id,
        ttl=timedelta(minutes=1),
    )
    envelope = CaptureEnvelope.model_validate(
        envelope.model_dump(mode="python")
        | {"subscription_id": subscription.subscription_id}
    )
    await channel.publish(envelope)
    wrong = execution_identity().model_copy(update={field: value})

    assert (
        await channel.bind(
            subscription_id=subscription.subscription_id,
            identity=wrong,
        )
        is CaptureDisposition.IDENTITY_MISMATCH
    )


@pytest.mark.asyncio
async def test_bounded_channel_returns_backpressure_when_buffer_is_full() -> None:
    channel = InMemoryCaptureChannel(clock=FakeClock(), max_buffered_envelopes=1)
    first = capture_envelope()
    first_subscription = await channel.register(
        capture_id=first.capture_id,
        ttl=timedelta(minutes=1),
    )
    first = CaptureEnvelope.model_validate(
        first.model_dump(mode="python")
        | {"subscription_id": first_subscription.subscription_id}
    )
    second = capture_envelope(
        capture_id=UUID("e9000000-0000-0000-0000-000000000471"),
        subscription_id=UUID("ea000000-0000-0000-0000-000000000471"),
    )
    second_subscription = await channel.register(
        capture_id=second.capture_id,
        ttl=timedelta(minutes=1),
    )
    second = CaptureEnvelope.model_validate(
        second.model_dump(mode="python")
        | {"subscription_id": second_subscription.subscription_id}
    )

    assert await channel.publish(first) is CaptureDisposition.BUFFERED
    assert await channel.publish(second) is CaptureDisposition.BACKPRESSURE


@pytest.mark.asyncio
async def test_binding_is_one_time_and_cleanup_removes_subscription() -> None:
    channel = InMemoryCaptureChannel(clock=FakeClock())
    envelope = capture_envelope()
    subscription = await channel.register(
        capture_id=envelope.capture_id,
        ttl=timedelta(minutes=1),
    )
    identity = execution_identity()

    assert (
        await channel.bind(
            subscription_id=subscription.subscription_id,
            identity=identity,
        )
        is CaptureDisposition.BOUND
    )
    assert (
        await channel.bind(
            subscription_id=subscription.subscription_id,
            identity=identity,
        )
        is CaptureDisposition.ALREADY_BOUND
    )
    assert (
        await channel.bind(
            subscription_id=subscription.subscription_id,
            identity=identity.model_copy(
                update={"run_id": UUID("f1000000-0000-0000-0000-000000000471")}
            ),
        )
        is CaptureDisposition.IDENTITY_MISMATCH
    )
    assert (
        await channel.cleanup(subscription_id=subscription.subscription_id)
        is CaptureDisposition.CLEANED
    )
    assert (
        await channel.cleanup(subscription_id=subscription.subscription_id)
        is CaptureDisposition.NOT_FOUND
    )
