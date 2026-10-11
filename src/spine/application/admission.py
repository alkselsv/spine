"""Application seam for recording source admission outcomes."""

from __future__ import annotations

from datetime import datetime
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID, uuid4

from pydantic import ConfigDict, Field

from spine.application.persistence.repositories import AdmissionRepository
from spine.domain.common import DefinitionModel, VersionIdentifier
from spine.domain.sources import (
    AdmissionDecision,
    AdmissionDisposition,
    AdmissionReference,
    ReasonCode,
    SourceRevision,
    SourceRevisionProvenance,
)


class AdmissionValidationEvidence(DefinitionModel):
    """Typed result of the isolated security admission validator."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    disposition: AdmissionDisposition
    validator_profile_version: VersionIdentifier
    reason_schema_version: VersionIdentifier
    reason_codes: tuple[ReasonCode, ...] = Field(min_length=1)
    authority_reference: AdmissionReference
    trace_id: UUID
    audit_event_id: UUID
    decided_at: datetime


@dataclass(frozen=True, slots=True)
class AdmissionDecisionService:
    """Turn validator evidence into an append-only decision chain node."""

    repository: AdmissionRepository
    id_factory: Callable[[], UUID] = uuid4

    async def record(
        self,
        *,
        revision: SourceRevision,
        evidence: AdmissionValidationEvidence,
        provenance: SourceRevisionProvenance | None = None,
        decision_id: UUID | None = None,
    ) -> AdmissionDecision:
        """Append a decision, quarantining observations without provenance.

        A missing or mismatched provenance record is never promoted to
        ``admitted``.  This keeps the security boundary independent from later
        parser/projection work.
        """

        effective = await self.repository.resolve_effective(revision.revision_id)
        if effective is None:
            version = 1
            predecessor = None
        else:
            version = effective.decision_version + 1
            predecessor = effective.decision_id

        disposition = evidence.disposition
        reason_codes = evidence.reason_codes
        if provenance is None or (
            provenance.revision_id != revision.revision_id
            or provenance.source_object_id != revision.source_object_id
            or provenance.workspace_id != revision.workspace_id
            or provenance.environment_id != revision.environment_id
        ):
            disposition = AdmissionDisposition.QUARANTINED
            reason_codes = ("missing_provenance",)

        decision = AdmissionDecision(
            decision_id=decision_id or self.id_factory(),
            source_object_id=revision.source_object_id,
            revision_id=revision.revision_id,
            workspace_id=revision.workspace_id,
            environment_id=revision.environment_id,
            decision_version=version,
            predecessor_decision_id=predecessor,
            disposition=disposition,
            validator_profile_version=evidence.validator_profile_version,
            reason_schema_version=evidence.reason_schema_version,
            reason_codes=reason_codes,
            authority_reference=evidence.authority_reference,
            trace_id=evidence.trace_id,
            audit_event_id=evidence.audit_event_id,
            decided_at=evidence.decided_at,
        )
        return await self.repository.append_decision(decision)


__all__ = ["AdmissionDecisionService", "AdmissionValidationEvidence"]
