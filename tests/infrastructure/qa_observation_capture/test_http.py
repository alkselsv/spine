from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI

from spine.application.qa_observation.channel import (
    CaptureDisposition,
    CaptureReceiveState,
)
from spine.application.qa_observation.contracts import CaptureEnvelope
from spine.infrastructure.qa_observation_capture.http import (
    MAX_CAPTURE_PAYLOAD_BYTES,
    CaptureWorkload,
    CaptureWorkloadRole,
    HttpCaptureChannel,
    create_capture_channel_router,
)
from tests.application.qa_observation.factories import (
    capture_envelope,
    execution_identity,
)

OBSERVER = CaptureWorkload(
    workload_id="qa-observer-test", role=CaptureWorkloadRole.OBSERVER
)
WORKER = CaptureWorkload(
    workload_id="qa-worker-test", role=CaptureWorkloadRole.WORKER
)


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 18, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now


class FakeAuthorizer:
    def __init__(self) -> None:
        self.denied = False
        self.trusted_identity = execution_identity()

    async def observer_may_bind(self, observer, identity) -> bool:
        return (
            not self.denied
            and observer == OBSERVER
            and identity.matches(self.trusted_identity)
        )

    async def worker_may_publish(self, worker, identity) -> bool:
        return (
            not self.denied
            and worker == WORKER
            and identity.matches(self.trusted_identity)
        )

    async def worker_may_deliver_to_observer(self, worker, observer, identity) -> bool:
        return (
            not self.denied
            and worker == WORKER
            and observer == OBSERVER
            and identity.matches(self.trusted_identity)
        )


@pytest.fixture
def transport_setup():
    clock = FakeClock()
    authorizer = FakeAuthorizer()
    workloads = {
        "Bearer observer-test": OBSERVER,
        "Bearer worker-test": WORKER,
    }

    async def authenticate(authorization):
        return workloads.get(authorization)

    app = FastAPI()
    app.include_router(
        create_capture_channel_router(
            authenticator=authenticate, authorizer=authorizer, clock=clock
        )
    )
    transport = httpx.ASGITransport(app=app)

    def client(token: str) -> HttpCaptureChannel:
        return HttpCaptureChannel(
            base_url="https://capture.test",
            headers=lambda: {"Authorization": f"Bearer {token}"},
            transport=transport,
        )

    return clock, authorizer, client, transport


@pytest.mark.asyncio
async def test_separate_observer_and_worker_clients_exchange_capture_once(
    transport_setup, caplog
):
    _, _, client, _ = transport_setup
    observer = client("observer-test")
    worker = client("worker-test")
    envelope = capture_envelope()
    subscription = await observer.register(
        capture_id=envelope.capture_id, ttl=timedelta(minutes=1)
    )
    envelope = CaptureEnvelope.model_validate(
        envelope.model_dump(mode="python")
        | {"subscription_id": subscription.subscription_id}
    )

    assert await worker.publish(envelope) is CaptureDisposition.BUFFERED
    identity = execution_identity()
    assert (
        await observer.receive(
            subscription_id=subscription.subscription_id,
            identity=identity,
            timeout=timedelta(0),
        )
    ).state is CaptureReceiveState.PENDING
    assert (
        await observer.bind(
            subscription_id=subscription.subscription_id, identity=identity
        )
    ) is CaptureDisposition.BOUND
    received = await observer.receive(
        subscription_id=subscription.subscription_id,
        identity=identity,
        timeout=timedelta(milliseconds=10),
    )
    assert received.state is CaptureReceiveState.CAPTURED
    assert received.capture == envelope
    assert await worker.publish(envelope) is CaptureDisposition.DUPLICATE
    assert (
        await observer.acknowledge(
            subscription_id=subscription.subscription_id,
            capture_id=envelope.capture_id,
            identity=identity,
        )
    ) is CaptureDisposition.ACKNOWLEDGED
    assert (
        await observer.cleanup(subscription_id=subscription.subscription_id)
    ) is CaptureDisposition.CLEANED
    assert "Synthetic question" not in caplog.text
    await observer.close()
    await worker.close()


@pytest.mark.asyncio
async def test_authentication_and_full_identity_authorization_fail_closed(transport_setup):
    _, authorizer, client, _ = transport_setup
    observer = client("observer-test")
    worker = client("worker-test")
    intruder = client("intruder")
    envelope = capture_envelope()
    subscription = await observer.register(
        capture_id=envelope.capture_id, ttl=timedelta(minutes=1)
    )
    identity = execution_identity()
    assert await intruder.bind(
        subscription_id=subscription.subscription_id, identity=identity
    ) is CaptureDisposition.UNAVAILABLE
    mismatch = identity.model_copy(
        update={"workspace_id": UUID("e5000000-0000-0000-0000-000000000471")}
    )
    assert await observer.bind(
        subscription_id=subscription.subscription_id, identity=identity
    ) is CaptureDisposition.BOUND
    assert await observer.bind(
        subscription_id=subscription.subscription_id, identity=mismatch
    ) is CaptureDisposition.NOT_FOUND
    envelope = CaptureEnvelope.model_validate(
        envelope.model_dump(mode="python")
        | {"subscription_id": subscription.subscription_id}
    )
    assert await worker.publish(envelope) is CaptureDisposition.BUFFERED
    authorizer.denied = True
    assert await worker.publish(envelope) is CaptureDisposition.NOT_FOUND
    assert (
        await intruder.receive(
            subscription_id=subscription.subscription_id,
            identity=identity,
            timeout=timedelta(0),
        )
    ).state is CaptureReceiveState.UNAVAILABLE
    await observer.close()
    await worker.close()
    await intruder.close()


@pytest.mark.asyncio
async def test_expiry_and_cleanup_drop_buffered_capture(transport_setup):
    clock, _, client, _ = transport_setup
    observer = client("observer-test")
    worker = client("worker-test")
    envelope = capture_envelope()
    subscription = await observer.register(
        capture_id=envelope.capture_id, ttl=timedelta(seconds=2)
    )
    envelope = CaptureEnvelope.model_validate(
        envelope.model_dump(mode="python")
        | {"subscription_id": subscription.subscription_id}
    )
    assert await worker.publish(envelope) is CaptureDisposition.BUFFERED
    clock.now += timedelta(seconds=2)
    assert await worker.publish(envelope) is CaptureDisposition.NOT_FOUND
    assert (
        await observer.receive(
            subscription_id=subscription.subscription_id,
            identity=execution_identity(),
            timeout=timedelta(0),
        )
    ).state is CaptureReceiveState.UNAVAILABLE
    await observer.close()
    await worker.close()


@pytest.mark.asyncio
async def test_oversized_body_is_rejected_and_auth_failure_is_unavailable(
    transport_setup,
):
    _, _, client, transport = transport_setup
    observer = client("observer-test")
    oversized = httpx.AsyncClient(
        base_url="https://capture.test",
        transport=transport,
        headers={"Authorization": "Bearer observer-test"},
    )
    response = await oversized.post(
        "/internal/qa-capture/subscriptions",
        content=b" " * (MAX_CAPTURE_PAYLOAD_BYTES + 1),
    )
    assert response.status_code == 413
    await oversized.aclose()

    denied = HttpCaptureChannel(
        base_url="https://capture.test",
        headers=lambda: {"Authorization": "Bearer invalid"},
        transport=transport,
    )
    assert await denied.bind(
        subscription_id=UUID("c0000000-0000-0000-0000-000000000471"),
        identity=execution_identity(),
    ) is CaptureDisposition.UNAVAILABLE
    await observer.close()
    await denied.close()


@pytest.mark.asyncio
async def test_buffer_capacity_is_bounded_with_explicit_backpressure(transport_setup):
    _, _, client, _ = transport_setup
    observer = client("observer-test")
    worker = client("worker-test")
    first = capture_envelope()
    first_subscription = await observer.register(
        capture_id=first.capture_id, ttl=timedelta(minutes=1)
    )
    first = CaptureEnvelope.model_validate(
        first.model_dump(mode="python")
        | {"subscription_id": first_subscription.subscription_id}
    )
    assert await worker.publish(first) is CaptureDisposition.BUFFERED
    for offset in range(1, 129):
        capture_id = UUID(int=0xC0000000000000000000000000000000 + offset)
        subscription = await observer.register(
            capture_id=capture_id, ttl=timedelta(minutes=1)
        )
        envelope = CaptureEnvelope.model_validate(
            first.model_dump(mode="python")
            | {
                "capture_id": capture_id,
                "subscription_id": subscription.subscription_id,
            }
        )
        disposition = await worker.publish(envelope)
        if offset < 128:
            assert disposition is CaptureDisposition.BUFFERED
        else:
            assert disposition is CaptureDisposition.BACKPRESSURE
    await observer.close()
    await worker.close()


@pytest.mark.asyncio
async def test_service_restart_loses_pending_capture_without_durable_recovery(
    transport_setup,
):
    _, authorizer, client, _ = transport_setup
    observer = client("observer-test")
    worker = client("worker-test")
    envelope = capture_envelope()
    subscription = await observer.register(
        capture_id=envelope.capture_id, ttl=timedelta(minutes=1)
    )
    envelope = CaptureEnvelope.model_validate(
        envelope.model_dump(mode="python")
        | {"subscription_id": subscription.subscription_id}
    )
    assert await worker.publish(envelope) is CaptureDisposition.BUFFERED

    # A new router models a restarted service process with an empty volatile store.
    app = FastAPI()

    async def authenticate(authorization):
        return {
            "Bearer observer-test": OBSERVER,
            "Bearer worker-test": WORKER,
        }.get(authorization)

    app.include_router(
        create_capture_channel_router(
            authenticator=authenticate, authorizer=authorizer, clock=lambda: datetime.now(timezone.utc)
        )
    )
    restarted_observer = HttpCaptureChannel(
        base_url="https://capture.test",
        headers=lambda: {"Authorization": "Bearer observer-test"},
        transport=httpx.ASGITransport(app=app),
    )
    assert (
        await restarted_observer.receive(
            subscription_id=subscription.subscription_id,
            identity=execution_identity(),
            timeout=timedelta(0),
        )
    ).state is CaptureReceiveState.UNAVAILABLE
    await observer.close()
    await worker.close()
    await restarted_observer.close()
