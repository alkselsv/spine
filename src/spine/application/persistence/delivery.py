"""Application contracts for durable outbox delivery state."""

from __future__ import annotations

import re
from datetime import timedelta
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from spine.application.persistence.errors import PersistenceError
from spine.application.persistence.outbox import OutboxIntent


_EVENT_TYPE = re.compile(r"[a-z][a-z0-9_.:-]{0,127}")
_CONSUMER_NAME = re.compile(r"[a-z][a-z0-9_.:-]{0,63}")


def _require_nonzero_uuid(value: UUID) -> UUID:
    if value.int == 0:
        raise ValueError("identity must be a non-zero UUID.")
    return value


class DuplicateDeliveryRouteError(PersistenceError):
    """An event type/version has more than one declared route."""


class UnsupportedDeliveryRouteError(PersistenceError):
    """An event type/version has no declared consumer route."""


class StaleDeliveryLeaseError(PersistenceError):
    """A delivery transition did not present the current active lease."""


class OutboxDeliveryStatus(str, Enum):
    """Operational states kept separate from immutable outbox intent facts."""

    PENDING = "pending"
    IN_FLIGHT = "in_flight"
    RETRY_SCHEDULED = "retry_scheduled"
    DELIVERED = "delivered"
    QUARANTINED = "quarantined"


class UnresolvedOutboxIntent(BaseModel):
    """Safe header-only representation of an unknown persisted event version."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    event_id: UUID
    workspace_id: UUID
    environment_id: UUID | None = None
    event_type: str
    schema_version: int
    trace_id: UUID
    correlation_id: UUID | None = None
    causation_id: UUID | None = None

    _validate_required_ids = field_validator(
        "event_id", "workspace_id", "trace_id"
    )(_require_nonzero_uuid)

    @field_validator("environment_id", "correlation_id", "causation_id")
    @classmethod
    def validate_optional_id(cls, value: UUID | None) -> UUID | None:
        return None if value is None else _require_nonzero_uuid(value)

    @field_validator("event_type")
    @classmethod
    def validate_event_type(cls, value: str) -> str:
        if _EVENT_TYPE.fullmatch(value) is None:
            raise ValueError("event_type must be a bounded identifier.")
        return value

    @field_validator("schema_version")
    @classmethod
    def validate_schema_version(cls, value: int) -> int:
        if isinstance(value, bool) or value <= 0:
            raise ValueError("schema_version must be a positive integer.")
        return value


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a timezone.")
    return value


def _require_optional_consumer(value: str | None) -> str | None:
    if value is not None and _CONSUMER_NAME.fullmatch(value) is None:
        raise ValueError("consumer_name must be a bounded identifier.")
    return value


class OutboxDelivery(BaseModel):
    """Immutable snapshot of mutable operational delivery control state."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    event_id: UUID
    workspace_id: UUID
    environment_id: UUID | None = None
    consumer_name: str | None = None
    status: OutboxDeliveryStatus
    attempt_count: int
    next_attempt_at: datetime | None = None
    lease_owner: str | None = None
    lease_token: UUID | None = None
    lease_expires_at: datetime | None = None
    last_error_code: str | None = None
    delivered_at: datetime | None = None
    quarantined_at: datetime | None = None
    version: int

    _validate_required_ids = field_validator("event_id", "workspace_id")(
        _require_nonzero_uuid
    )

    @field_validator("environment_id", "lease_token")
    @classmethod
    def validate_optional_id(cls, value: UUID | None) -> UUID | None:
        return None if value is None else _require_nonzero_uuid(value)

    _validate_consumer = field_validator("consumer_name")(
        _require_optional_consumer
    )

    @field_validator("lease_owner")
    @classmethod
    def validate_lease_owner(cls, value: str | None) -> str | None:
        if value is not None and _CONSUMER_NAME.fullmatch(value) is None:
            raise ValueError("lease_owner must be a bounded identifier.")
        return value

    @field_validator("last_error_code")
    @classmethod
    def validate_error_code(cls, value: str | None) -> str | None:
        if value is not None and _EVENT_TYPE.fullmatch(value) is None:
            raise ValueError("last_error_code must be a bounded safe code.")
        return value

    @field_validator(
        "next_attempt_at",
        "lease_expires_at",
        "delivered_at",
        "quarantined_at",
    )
    @classmethod
    def validate_optional_timestamp(
        cls,
        value: datetime | None,
    ) -> datetime | None:
        return None if value is None else _require_aware(value)

    @field_validator("attempt_count", "version")
    @classmethod
    def validate_nonnegative_integer(cls, value: int) -> int:
        if isinstance(value, bool) or value < 0:
            raise ValueError("counter must be a non-negative integer.")
        return value

    @model_validator(mode="after")
    def validate_state(self) -> "OutboxDelivery":
        lease_values = (self.lease_owner, self.lease_token, self.lease_expires_at)
        has_complete_lease = all(value is not None for value in lease_values)
        has_any_lease = any(value is not None for value in lease_values)
        if self.status is OutboxDeliveryStatus.IN_FLIGHT:
            if not has_complete_lease or self.attempt_count < 1:
                raise ValueError("in-flight delivery requires one complete lease.")
            if any(
                value is not None
                for value in (
                    self.next_attempt_at,
                    self.delivered_at,
                    self.quarantined_at,
                )
            ):
                raise ValueError("in-flight delivery contains incompatible state.")
        elif has_any_lease:
            raise ValueError("only in-flight delivery may retain lease state.")

        if self.status is OutboxDeliveryStatus.PENDING:
            if self.attempt_count != 0 or self.next_attempt_at is None:
                raise ValueError("pending delivery must be due with no attempts.")
            if any(
                value is not None
                for value in (
                    self.last_error_code,
                    self.delivered_at,
                    self.quarantined_at,
                )
            ):
                raise ValueError("pending delivery contains incompatible state.")
        elif self.status is OutboxDeliveryStatus.RETRY_SCHEDULED:
            if (
                self.attempt_count < 1
                or self.next_attempt_at is None
                or self.last_error_code is None
                or self.delivered_at is not None
                or self.quarantined_at is not None
            ):
                raise ValueError("retry delivery state is incomplete.")
        elif self.status is OutboxDeliveryStatus.DELIVERED:
            if (
                self.attempt_count < 1
                or self.delivered_at is None
                or self.next_attempt_at is not None
                or self.quarantined_at is not None
                or self.last_error_code is not None
            ):
                raise ValueError("delivered state is incomplete.")
        elif self.status is OutboxDeliveryStatus.QUARANTINED:
            if (
                self.attempt_count < 1
                or self.quarantined_at is None
                or self.next_attempt_at is not None
                or self.delivered_at is not None
                or self.last_error_code is None
            ):
                raise ValueError("quarantined state is incomplete.")
        return self


class ClaimedOutboxDelivery(BaseModel):
    """Validated intent plus lease authority returned by one committed claim."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        arbitrary_types_allowed=True,
    )

    intent: OutboxIntent | UnresolvedOutboxIntent
    consumer_name: str | None = None
    worker_identity: str
    lease_token: UUID
    claimed_at: datetime
    lease_expires_at: datetime
    attempt_number: int
    delivery_version: int

    _validate_consumer = field_validator("consumer_name")(
        _require_optional_consumer
    )
    _validate_lease_token = field_validator("lease_token")(_require_nonzero_uuid)
    _validate_timestamps = field_validator("claimed_at", "lease_expires_at")(
        _require_aware
    )

    @field_validator("worker_identity")
    @classmethod
    def validate_worker_identity(cls, value: str) -> str:
        if _CONSUMER_NAME.fullmatch(value) is None:
            raise ValueError("worker_identity must be a bounded identifier.")
        return value

    @field_validator("attempt_number")
    @classmethod
    def validate_attempt_number(cls, value: int) -> int:
        if isinstance(value, bool) or value <= 0:
            raise ValueError("attempt_number must be a positive integer.")
        return value

    @field_validator("delivery_version")
    @classmethod
    def validate_delivery_version(cls, value: int) -> int:
        if isinstance(value, bool) or value <= 0:
            raise ValueError("delivery_version must be a positive integer.")
        return value

    @model_validator(mode="after")
    def validate_claim(self) -> "ClaimedOutboxDelivery":
        if type(self.intent) not in (OutboxIntent, UnresolvedOutboxIntent):
            raise ValueError("claimed delivery requires a canonical intent envelope.")
        if self.intent.event_id is None:
            raise ValueError("claimed delivery requires a persisted intent.")
        if self.lease_expires_at <= self.claimed_at:
            raise ValueError("lease expiry must follow claim time.")
        return self


class OutboxDeliveryRepository(Protocol):
    """Atomic operational persistence for outbox claims and transitions."""

    async def materialize(
        self,
        *,
        limit: int,
        materialized_at: datetime,
    ) -> tuple[OutboxDelivery, ...]: ...

    async def claim_due(
        self,
        *,
        limit: int,
        worker_identity: str,
        claimed_at: datetime,
        lease_duration: timedelta,
    ) -> tuple[ClaimedOutboxDelivery, ...]: ...

    async def resolve(self, event_id: UUID) -> OutboxDelivery | None: ...

    async def acknowledge(
        self,
        claim: ClaimedOutboxDelivery,
        *,
        completed_at: datetime,
    ) -> OutboxDelivery: ...

    async def reschedule(
        self,
        claim: ClaimedOutboxDelivery,
        *,
        failed_at: datetime,
        next_attempt_at: datetime,
        error_code: str,
    ) -> OutboxDelivery: ...

    async def quarantine(
        self,
        claim: ClaimedOutboxDelivery,
        *,
        quarantined_at: datetime,
        error_code: str,
    ) -> OutboxDelivery: ...

    async def renew(
        self,
        claim: ClaimedOutboxDelivery,
        *,
        renewed_at: datetime,
        lease_duration: timedelta,
    ) -> ClaimedOutboxDelivery: ...


@dataclass(frozen=True, slots=True)
class OutboxDeliveryRoute:
    """One explicit event-version to consumer binding."""

    event_type: str
    schema_version: int
    consumer_name: str

    def __post_init__(self) -> None:
        if _EVENT_TYPE.fullmatch(self.event_type) is None:
            raise ValueError("event_type must be a bounded identifier.")
        if isinstance(self.schema_version, bool) or self.schema_version <= 0:
            raise ValueError("schema_version must be a positive integer.")
        if _CONSUMER_NAME.fullmatch(self.consumer_name) is None:
            raise ValueError("consumer_name must be a bounded identifier.")


class OutboxDeliveryRouteRegistry:
    """Explicit single-consumer routes for versioned outbox events."""

    def __init__(self, *, version: str) -> None:
        if _EVENT_TYPE.fullmatch(version) is None:
            raise ValueError("route registry version must be a bounded identifier.")
        self._version = version
        self._routes: dict[tuple[str, int], OutboxDeliveryRoute] = {}
        self._sealed = False

    @property
    def version(self) -> str:
        return self._version

    def register(
        self,
        *,
        event_type: str,
        schema_version: int,
        consumer_name: str,
    ) -> None:
        if self._sealed:
            raise RuntimeError("Outbox delivery route registry is sealed.")
        route = OutboxDeliveryRoute(
            event_type=event_type,
            schema_version=schema_version,
            consumer_name=consumer_name,
        )
        key = (route.event_type, route.schema_version)
        if key in self._routes:
            raise DuplicateDeliveryRouteError(
                "Outbox delivery route is already registered."
            )
        self._routes[key] = route

    def seal(self) -> None:
        self._sealed = True

    def require_sealed(self) -> None:
        if not self._sealed:
            raise RuntimeError("Outbox delivery route registry must be sealed.")

    def resolve(self, event_type: str, schema_version: int) -> OutboxDeliveryRoute:
        route = self._routes.get((event_type, schema_version))
        if route is None:
            raise UnsupportedDeliveryRouteError(
                "Outbox delivery route is not registered."
            )
        return route

    def resolve_optional(
        self,
        event_type: str,
        schema_version: int,
    ) -> OutboxDeliveryRoute | None:
        """Resolve a route without making an unknown event unrepresentable."""

        return self._routes.get((event_type, schema_version))


__all__ = [
    "ClaimedOutboxDelivery",
    "DuplicateDeliveryRouteError",
    "OutboxDelivery",
    "OutboxDeliveryRoute",
    "OutboxDeliveryRouteRegistry",
    "OutboxDeliveryRepository",
    "OutboxDeliveryStatus",
    "StaleDeliveryLeaseError",
    "UnsupportedDeliveryRouteError",
    "UnresolvedOutboxIntent",
]
