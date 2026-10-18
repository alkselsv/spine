"""Application port and safe dispositions for ephemeral Q&A capture delivery."""

from __future__ import annotations

from datetime import timedelta
from dataclasses import dataclass
from enum import Enum
from typing import Protocol
from uuid import UUID

from spine.application.qa_observation.contracts import (
    AdmittedExecutionIdentity,
    CaptureEnvelope,
    CaptureSubscription,
)


class CaptureDisposition(str, Enum):
    REGISTERED = "registered"
    BOUND = "bound"
    ALREADY_BOUND = "already_bound"
    BUFFERED = "buffered"
    DUPLICATE = "duplicate"
    ACKNOWLEDGED = "acknowledged"
    CLEANED = "cleaned"
    EXPIRED = "expired"
    IDENTITY_MISMATCH = "identity_mismatch"
    NOT_FOUND = "not_found"
    BACKPRESSURE = "backpressure"
    CONFLICT = "conflict"
    ALREADY_CONSUMED = "already_consumed"
    UNAVAILABLE = "unavailable"


class CaptureReceiveState(str, Enum):
    CAPTURED = "captured"
    PENDING = "pending"
    EXPIRED = "expired"
    IDENTITY_MISMATCH = "identity_mismatch"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class CaptureReceiveResult:
    """Typed receive status; protected payload exists only for captured state."""

    state: CaptureReceiveState
    capture: CaptureEnvelope | None = None

    def __post_init__(self) -> None:
        state, capture = self.state, self.capture
        if (state is CaptureReceiveState.CAPTURED) != (capture is not None):
            raise ValueError("captured state requires exactly one capture envelope")


class CaptureChannel(Protocol):
    """Authenticated, volatile one-shot handoff between observer and worker.

    Implementations authenticate both workloads and authorize each producer and
    consumer for its subscription. They register before admission, bind the
    subscription once to canonical post-admission identity, and use bounded
    buffering with backpressure and enforce the registered TTL. TTL expiry,
    identity mismatch, lost delivery,
    or transport failure is fail-closed. Duplicate publication of the same
    envelope is idempotent; acknowledgement is one-shot and cleanup removes all
    payloads. Production implementations must keep protected captures out of
    Temporal inputs/results, logs, and durable benchmark storage.
    """

    async def register(
        self,
        *,
        capture_id: UUID,
        ttl: timedelta,
    ) -> CaptureSubscription: ...

    async def bind(
        self,
        *,
        subscription_id: UUID,
        identity: AdmittedExecutionIdentity,
    ) -> CaptureDisposition: ...

    async def receive(
        self,
        *,
        subscription_id: UUID,
        identity: AdmittedExecutionIdentity,
        timeout: timedelta,
    ) -> CaptureReceiveResult: ...

    async def publish(self, envelope: CaptureEnvelope) -> CaptureDisposition: ...

    async def acknowledge(
        self,
        *,
        subscription_id: UUID,
        capture_id: UUID,
        identity: AdmittedExecutionIdentity,
    ) -> CaptureDisposition: ...

    async def cleanup(self, *, subscription_id: UUID) -> CaptureDisposition: ...
