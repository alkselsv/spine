"""Application contracts for canonical source acceptance (Issue #84)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
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
            "source_object_id": source.source_object_id,
            "revision_id": revision.revision_id,
            "revision_digest": revision.revision_digest,
            "provenance_id": provenance.provenance_id,
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
    "CANONICAL_ACCEPTANCE_SCHEMA_VERSION",
    "CanonicalAcceptanceCommand",
    "CanonicalAcceptanceRepository",
    "CanonicalAcceptanceResult",
]
