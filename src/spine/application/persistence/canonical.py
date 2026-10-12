"""Application contracts for canonical source acceptance (Issue #84)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import re
from typing import Protocol
from uuid import UUID

from spine.application.persistence.command_digest import CommandDigest, digest_command
from spine.application.persistence.context import PersistenceOperation
from spine.application.persistence.idempotency import (
    IdempotencyClaimResult,
    IdempotencyKey,
    OpaqueResultReference,
)
from spine.application.persistence.outbox import OutboxIntent
from spine.domain.audit import AuditEvent
from spine.domain.sources import (
    CanonicalAcceptance,
    CurrentSourceHead,
    SourceObject,
    SourceRevision,
    SourceRevisionProvenance,
)

CANONICAL_ACCEPTANCE_SCHEMA_VERSION = 1
ALLOWED_CANONICAL_ADMISSION_PROFILES = frozenset({"admission:r1"})


class SourceOrderComparison(str, Enum):
    LOWER = "lower"
    EQUAL = "equal"
    HIGHER = "higher"


class SourceOrderingPolicyError(ValueError):
    """An ordering scheme or token is unknown, invalid, or incomparable."""


SourceOrderComparator = Callable[[str, str], SourceOrderComparison]


class SourceOrderingPolicyRegistry:
    """Closed registry of versioned connector ordering comparators."""

    def __init__(self, policies: Mapping[str, SourceOrderComparator]) -> None:
        self._policies = dict(policies)
        if not self._policies:
            raise ValueError("at least one source ordering policy is required")

    def compare(
        self, scheme: str, incoming: str, current: str
    ) -> SourceOrderComparison:
        comparator = self._policies.get(scheme)
        if comparator is None:
            raise SourceOrderingPolicyError("source ordering scheme is unsupported")
        try:
            return comparator(incoming, current)
        except SourceOrderingPolicyError:
            raise
        except (TypeError, ValueError):
            raise SourceOrderingPolicyError(
                "source ordering token is invalid"
            ) from None

    @classmethod
    def r1(cls) -> "SourceOrderingPolicyRegistry":
        return cls({"integer.v1": _compare_canonical_decimal})


_CANONICAL_DECIMAL = re.compile(r"0|[1-9][0-9]*")


def _compare_canonical_decimal(incoming: str, current: str) -> SourceOrderComparison:
    if (
        _CANONICAL_DECIMAL.fullmatch(incoming) is None
        or _CANONICAL_DECIMAL.fullmatch(current) is None
    ):
        raise SourceOrderingPolicyError("source ordering token is invalid")
    incoming_value, current_value = int(incoming), int(current)
    if incoming_value < current_value:
        return SourceOrderComparison.LOWER
    if incoming_value > current_value:
        return SourceOrderComparison.HIGHER
    return SourceOrderComparison.EQUAL


@dataclass(frozen=True, slots=True)
class CanonicalAcceptanceCommand:
    source: SourceObject
    revision: SourceRevision
    provenance: SourceRevisionProvenance
    admission_decision_id: UUID | None
    access_policy_version_id: UUID | None
    expected_current_revision_id: UUID | None
    idempotency_key: IdempotencyKey
    canonical_acceptance_id: UUID
    audit_event_id: UUID
    outbox_event_id: UUID
    accepted_at: datetime
    audit_event: AuditEvent | None
    outbox_intent: OutboxIntent | None
    digest: CommandDigest

    def expected_digest(self) -> CommandDigest:
        return CanonicalAcceptanceCommand.create(
            source=self.source,
            revision=self.revision,
            provenance=self.provenance,
            admission_decision_id=self.admission_decision_id,
            access_policy_version_id=self.access_policy_version_id,
            expected_current_revision_id=self.expected_current_revision_id,
            idempotency_key=self.idempotency_key,
            canonical_acceptance_id=self.canonical_acceptance_id,
            audit_event_id=self.audit_event_id,
            outbox_event_id=self.outbox_event_id,
            accepted_at=self.accepted_at,
            audit_event=self.audit_event,
            outbox_intent=self.outbox_intent,
        ).digest

    @classmethod
    def create(
        cls,
        *,
        source: SourceObject,
        revision: SourceRevision,
        provenance: SourceRevisionProvenance,
        admission_decision_id: UUID | None = None,
        access_policy_version_id: UUID | None = None,
        expected_current_revision_id: UUID | None,
        idempotency_key: IdempotencyKey,
        canonical_acceptance_id: UUID,
        audit_event_id: UUID,
        outbox_event_id: UUID,
        accepted_at: datetime,
        audit_event: AuditEvent | None = None,
        outbox_intent: OutboxIntent | None = None,
    ) -> "CanonicalAcceptanceCommand":
        payload = {
            "source": source.model_dump(mode="json"),
            "revision": revision.model_dump(mode="json"),
            "provenance": provenance.model_dump(mode="json"),
            "admission_decision_id": admission_decision_id,
            "access_policy_version_id": access_policy_version_id,
            "event_identity": provenance.event_identity,
            "event_digest": provenance.event_digest,
            "expected_current_revision_id": expected_current_revision_id,
            "canonical_acceptance_id": canonical_acceptance_id,
            "audit_event_id": audit_event_id,
            "outbox_event_id": outbox_event_id,
            "accepted_at": accepted_at,
            "audit_event": (
                audit_event.model_dump(mode="json", exclude={"payload_snapshot"})
                if audit_event is not None
                else None
            ),
            "outbox_intent": (
                outbox_intent.model_dump(mode="json", exclude={"payload_snapshot"})
                if outbox_intent is not None
                else None
            ),
            "idempotency_key": idempotency_key.value,
        }
        digest = digest_command(
            operation=PersistenceOperation("canonical_acceptance"),
            operation_schema_version=CANONICAL_ACCEPTANCE_SCHEMA_VERSION,
            payload=payload,
        )
        return cls(
            source=source,
            revision=revision,
            provenance=provenance,
            admission_decision_id=admission_decision_id,
            access_policy_version_id=access_policy_version_id,
            expected_current_revision_id=expected_current_revision_id,
            idempotency_key=idempotency_key,
            canonical_acceptance_id=canonical_acceptance_id,
            audit_event_id=audit_event_id,
            outbox_event_id=outbox_event_id,
            accepted_at=accepted_at,
            audit_event=audit_event,
            outbox_intent=outbox_intent,
            digest=digest,
        )


@dataclass(frozen=True, slots=True)
class CanonicalAcceptanceResult:
    source: SourceObject
    revision: SourceRevision
    current: CurrentSourceHead
    acceptance: CanonicalAcceptance | None
    replay: bool
    advanced: bool
    claim: IdempotencyClaimResult
    result_reference: OpaqueResultReference


class CanonicalAcceptanceRepository(Protocol):
    async def accept(
        self, command: CanonicalAcceptanceCommand
    ) -> CanonicalAcceptanceResult: ...

    async def resolve_current(self, source_object_id: UUID) -> CurrentSourceHead | None: ...

    async def resolve_acceptance(
        self, canonical_acceptance_id: UUID
    ) -> CanonicalAcceptance | None: ...


__all__ = [
    "ALLOWED_CANONICAL_ADMISSION_PROFILES",
    "CANONICAL_ACCEPTANCE_SCHEMA_VERSION",
    "CanonicalAcceptanceCommand",
    "CanonicalAcceptanceRepository",
    "CanonicalAcceptanceResult",
    "SourceOrderComparison",
    "SourceOrderingPolicyError",
    "SourceOrderingPolicyRegistry",
]
