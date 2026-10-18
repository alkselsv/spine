"""Small bounded in-process store used by the authenticated capture service."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable
from uuid import NAMESPACE_URL, UUID, uuid5

from spine.application.qa_observation.channel import (
    CaptureDisposition,
    CaptureReceiveResult,
    CaptureReceiveState,
)
from spine.application.qa_observation.contracts import (
    AdmittedExecutionIdentity,
    CaptureEnvelope,
    CaptureSubscription,
)


@dataclass(slots=True)
class _Subscription:
    routing: CaptureSubscription
    identity: AdmittedExecutionIdentity | None = None
    envelope: CaptureEnvelope | None = None
    delivered: bool = False
    consumed: bool = False


class VolatileCaptureStore:
    """In-memory, one-shot state. A service restart intentionally loses it."""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime],
        max_buffered_envelopes: int,
    ) -> None:
        if max_buffered_envelopes < 1:
            raise ValueError("capture buffer capacity must be positive")
        self._clock = clock
        self._max_buffered_envelopes = max_buffered_envelopes
        self._subscriptions: dict[UUID, _Subscription] = {}

    async def register(
        self, *, capture_id: UUID, ttl: timedelta
    ) -> CaptureSubscription:
        if capture_id.int == 0 or ttl <= timedelta(0):
            raise ValueError("capture registration bounds are invalid")
        if any(
            state.routing.capture_id == capture_id
            for state in self._subscriptions.values()
        ):
            raise ValueError("capture_id is already registered")
        subscription_id = uuid5(NAMESPACE_URL, f"spine:qa-observer:{capture_id}")
        if subscription_id in self._subscriptions:
            raise ValueError("subscription identity collision")
        routing = CaptureSubscription(
            capture_id=capture_id,
            subscription_id=subscription_id,
            expires_at=self._utc_now() + ttl,
        )
        self._subscriptions[subscription_id] = _Subscription(routing)
        return routing

    async def bind(
        self, *, subscription_id: UUID, identity: AdmittedExecutionIdentity
    ) -> CaptureDisposition:
        state = self._active(subscription_id)
        if state is None:
            return self._missing(subscription_id)
        if state.identity is not None:
            return (
                CaptureDisposition.ALREADY_BOUND
                if state.identity.matches(identity)
                else CaptureDisposition.IDENTITY_MISMATCH
            )
        if state.envelope is not None and not state.envelope.identity.matches(identity):
            state.envelope = None
            state.consumed = True
            return CaptureDisposition.IDENTITY_MISMATCH
        state.identity = identity
        return CaptureDisposition.BOUND

    async def receive(
        self, *, subscription_id: UUID, identity: AdmittedExecutionIdentity
    ) -> CaptureReceiveResult:
        state = self._active(subscription_id)
        if state is None:
            return CaptureReceiveResult(
                CaptureReceiveState.EXPIRED
                if subscription_id in self._subscriptions
                else CaptureReceiveState.UNAVAILABLE
            )
        if state.identity is None:
            return CaptureReceiveResult(CaptureReceiveState.PENDING)
        if not state.identity.matches(identity):
            return CaptureReceiveResult(CaptureReceiveState.IDENTITY_MISMATCH)
        if state.envelope is None or state.delivered or state.consumed:
            return CaptureReceiveResult(CaptureReceiveState.PENDING)
        if not state.envelope.identity.matches(identity):
            state.envelope = None
            state.consumed = True
            return CaptureReceiveResult(CaptureReceiveState.IDENTITY_MISMATCH)
        state.delivered = True
        return CaptureReceiveResult(CaptureReceiveState.CAPTURED, state.envelope)

    async def publish(self, envelope: CaptureEnvelope) -> CaptureDisposition:
        state = self._active(envelope.subscription_id)
        if state is None:
            return self._missing(envelope.subscription_id)
        if envelope.capture_id != state.routing.capture_id:
            return CaptureDisposition.IDENTITY_MISMATCH
        if state.consumed:
            return CaptureDisposition.ALREADY_CONSUMED
        if state.envelope is not None:
            return (
                CaptureDisposition.DUPLICATE
                if state.envelope == envelope
                else CaptureDisposition.CONFLICT
            )
        buffered_count = sum(
            candidate.envelope is not None
            and self._utc_now() < candidate.routing.expires_at
            for candidate in self._subscriptions.values()
        )
        if buffered_count >= self._max_buffered_envelopes:
            return CaptureDisposition.BACKPRESSURE
        if state.identity is not None and not state.identity.matches(envelope.identity):
            return CaptureDisposition.IDENTITY_MISMATCH
        state.envelope = envelope
        return CaptureDisposition.BUFFERED

    async def acknowledge(
        self,
        *,
        subscription_id: UUID,
        capture_id: UUID,
        identity: AdmittedExecutionIdentity,
    ) -> CaptureDisposition:
        state = self._active(subscription_id)
        if state is None:
            return self._missing(subscription_id)
        if (
            capture_id != state.routing.capture_id
            or state.identity is None
            or not state.identity.matches(identity)
        ):
            return CaptureDisposition.IDENTITY_MISMATCH
        if state.consumed:
            return CaptureDisposition.ALREADY_CONSUMED
        if not state.delivered or state.envelope is None:
            return CaptureDisposition.NOT_FOUND
        state.envelope = None
        state.consumed = True
        return CaptureDisposition.ACKNOWLEDGED

    async def cleanup(self, *, subscription_id: UUID) -> CaptureDisposition:
        return (
            CaptureDisposition.CLEANED
            if self._subscriptions.pop(subscription_id, None) is not None
            else CaptureDisposition.NOT_FOUND
        )

    def _active(self, subscription_id: UUID) -> _Subscription | None:
        state = self._subscriptions.get(subscription_id)
        if state is None:
            return None
        if self._utc_now() >= state.routing.expires_at:
            state.envelope = None
            return None
        return state

    def _missing(self, subscription_id: UUID) -> CaptureDisposition:
        return (
            CaptureDisposition.EXPIRED
            if subscription_id in self._subscriptions
            else CaptureDisposition.NOT_FOUND
        )

    def _utc_now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() != timedelta(0):
            raise ValueError("capture channel clock must return aware UTC timestamps")
        return now.astimezone(timezone.utc)
