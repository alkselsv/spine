"""Deterministic in-memory fake for Q&A observer handoff contract tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable
from uuid import NAMESPACE_URL, UUID, uuid5

from spine.application.qa_observation.channel import (
    CaptureChannel,
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
class _SubscriptionState:
    subscription: CaptureSubscription
    identity: AdmittedExecutionIdentity | None = None
    envelope: CaptureEnvelope | None = None
    delivered: bool = False
    consumed: bool = False


class InMemoryCaptureChannel(CaptureChannel):
    """Volatile channel fake with injectable clock and deterministic IDs.

    The fake deliberately retains a typed payload only in process memory. A
    receive call never waits: tests can control publish/bind ordering directly.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], datetime],
        max_buffered_envelopes: int = 64,
    ) -> None:
        if max_buffered_envelopes < 1:
            raise ValueError("channel buffer capacity must be positive")
        self._clock = clock
        self._max_buffered_envelopes = max_buffered_envelopes
        self._subscriptions: dict[UUID, _SubscriptionState] = {}

    async def register(
        self,
        *,
        capture_id: UUID,
        ttl: timedelta,
    ) -> CaptureSubscription:
        if capture_id.int == 0 or ttl <= timedelta(0):
            raise ValueError("capture registration bounds are invalid")
        if any(
            state.subscription.capture_id == capture_id
            for state in self._subscriptions.values()
        ):
            raise ValueError("capture_id is already registered")
        subscription_id = uuid5(NAMESPACE_URL, f"spine:qa-observer:{capture_id}")
        if subscription_id in self._subscriptions:
            raise ValueError("subscription identity collision")
        now = self._utc_now()
        subscription = CaptureSubscription(
            capture_id=capture_id,
            subscription_id=subscription_id,
            expires_at=now + ttl,
        )
        self._subscriptions[subscription_id] = _SubscriptionState(
            subscription=subscription,
        )
        return subscription

    async def bind(
        self,
        *,
        subscription_id: UUID,
        identity: AdmittedExecutionIdentity,
    ) -> CaptureDisposition:
        state = self._active(subscription_id)
        if state is None:
            return self._missing_disposition(subscription_id)
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
        self,
        *,
        subscription_id: UUID,
        identity: AdmittedExecutionIdentity,
        timeout: timedelta,
    ) -> CaptureReceiveResult:
        if timeout < timedelta(0):
            raise ValueError("receive timeout must be non-negative")
        state = self._active(subscription_id)
        if state is None:
            if subscription_id in self._subscriptions:
                return CaptureReceiveResult(CaptureReceiveState.EXPIRED)
            return CaptureReceiveResult(CaptureReceiveState.UNAVAILABLE)
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
            return self._missing_disposition(envelope.subscription_id)
        if envelope.capture_id != state.subscription.capture_id:
            return CaptureDisposition.IDENTITY_MISMATCH
        if state.consumed:
            return CaptureDisposition.ALREADY_CONSUMED
        if state.envelope is not None:
            return (
                CaptureDisposition.DUPLICATE
                if state.envelope == envelope
                else CaptureDisposition.CONFLICT
            )
        now = self._utc_now()
        buffered_count = sum(
            candidate.envelope is not None and now < candidate.subscription.expires_at
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
            return self._missing_disposition(subscription_id)
        if (
            capture_id != state.subscription.capture_id
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
        state = self._subscriptions.pop(subscription_id, None)
        return (
            CaptureDisposition.CLEANED
            if state is not None
            else CaptureDisposition.NOT_FOUND
        )

    def _active(self, subscription_id: UUID) -> _SubscriptionState | None:
        state = self._subscriptions.get(subscription_id)
        if state is None:
            return None
        if self._utc_now() >= state.subscription.expires_at:
            state.envelope = None
            return None
        return state

    def _missing_disposition(self, subscription_id: UUID) -> CaptureDisposition:
        return (
            CaptureDisposition.EXPIRED
            if subscription_id in self._subscriptions
            else CaptureDisposition.NOT_FOUND
        )

    def _utc_now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() != timedelta(0):
            raise ValueError("fake channel clock must return aware UTC timestamps")
        return now.astimezone(timezone.utc)
