"""Fail-closed current processing-disposition authority.

Ordinary projection and disclosure must resolve the canonical source head and
the exact acceptance selected by that head.  A v2 acceptance is eligible only
when its immutable processing admission is ``PROCESSOR_REQUIRED``.  Historical
v1 acceptances retain their processed-content semantics, but are still required
to be the exact canonical current acceptance.
"""

from __future__ import annotations

from enum import Enum
from typing import Protocol
from uuid import UUID

from pydantic import ConfigDict

from spine.application.persistence.canonical import CanonicalAcceptanceReadResult
from spine.domain.common import DefinitionModel
from spine.domain.sources import CurrentSourceHead, ProcessingAdmissionMode, RevisionKind


class OrdinaryProcessingEligibilityReason(str, Enum):
    ELIGIBLE = "eligible"
    CURRENT_AUTHORITY_UNAVAILABLE = "current_authority_unavailable"
    CURRENT_REVISION_MISMATCH = "current_revision_mismatch"
    ACCEPTANCE_UNAVAILABLE = "acceptance_unavailable"
    PROCESSING_DISPOSITION_UNAVAILABLE = "processing_disposition_unavailable"
    OPAQUE_ACCEPTANCE = "opaque_acceptance"


class OrdinaryProcessingEligibility(DefinitionModel):
    """Content-free result reusable at projection and disclosure seams."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    eligible: bool
    reason: OrdinaryProcessingEligibilityReason
    canonical_acceptance_id: UUID | None = None
    processing_admission_id: UUID | None = None
    processing_mode: ProcessingAdmissionMode | None = None


class CanonicalProcessingDispositionReader(Protocol):
    async def resolve_current(
        self, source_object_id: UUID
    ) -> CurrentSourceHead | None: ...

    async def resolve_acceptance_read(
        self, canonical_acceptance_id: UUID
    ) -> CanonicalAcceptanceReadResult | None: ...


class CurrentProcessingDispositionAuthority(Protocol):
    async def resolve(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        source_object_id: UUID,
        source_revision_id: UUID,
    ) -> OrdinaryProcessingEligibility: ...


class CanonicalCurrentProcessingDispositionAuthority:
    """Resolve exact current acceptance/disposition through a canonical reader."""

    def __init__(self, canonical: CanonicalProcessingDispositionReader) -> None:
        self._canonical = canonical

    async def resolve(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        source_object_id: UUID,
        source_revision_id: UUID,
    ) -> OrdinaryProcessingEligibility:
        try:
            current = await self._canonical.resolve_current(source_object_id)
        except Exception:
            return self._reject(
                OrdinaryProcessingEligibilityReason.CURRENT_AUTHORITY_UNAVAILABLE
            )
        if current is None:
            return self._reject(
                OrdinaryProcessingEligibilityReason.CURRENT_AUTHORITY_UNAVAILABLE
            )
        if (
            getattr(current, "workspace_id", None) != workspace_id
            or getattr(current, "environment_id", None) != environment_id
            or getattr(current, "source_object_id", None) != source_object_id
            or getattr(current, "current_revision_id", None) != source_revision_id
            or getattr(current, "current_revision_kind", None) is not RevisionKind.CONTENT
        ):
            return self._reject(
                OrdinaryProcessingEligibilityReason.CURRENT_REVISION_MISMATCH,
                canonical_acceptance_id=getattr(current, "acceptance_id", None),
            )
        try:
            resolved = await self._canonical.resolve_acceptance_read(
                current.acceptance_id
            )
        except Exception:
            return self._reject(
                OrdinaryProcessingEligibilityReason.ACCEPTANCE_UNAVAILABLE,
                canonical_acceptance_id=current.acceptance_id,
            )
        if resolved is None:
            return self._reject(
                OrdinaryProcessingEligibilityReason.ACCEPTANCE_UNAVAILABLE,
                canonical_acceptance_id=current.acceptance_id,
            )
        acceptance = resolved.acceptance
        if (
            not resolved.is_current
            or resolved.current != current
            or acceptance.canonical_acceptance_id != current.acceptance_id
            or acceptance.workspace_id != workspace_id
            or acceptance.environment_id != environment_id
            or acceptance.source_object_id != source_object_id
            or acceptance.source_revision_id != source_revision_id
        ):
            return self._reject(
                OrdinaryProcessingEligibilityReason.ACCEPTANCE_UNAVAILABLE,
                canonical_acceptance_id=current.acceptance_id,
            )
        if acceptance.canonical_acceptance_schema_version == 1:
            return OrdinaryProcessingEligibility(
                eligible=True,
                reason=OrdinaryProcessingEligibilityReason.ELIGIBLE,
                canonical_acceptance_id=acceptance.canonical_acceptance_id,
            )
        admission = resolved.processing_admission
        if admission is None:
            return self._reject(
                OrdinaryProcessingEligibilityReason.PROCESSING_DISPOSITION_UNAVAILABLE,
                canonical_acceptance_id=acceptance.canonical_acceptance_id,
            )
        if (
            admission.processing_admission_id != acceptance.processing_admission_id
            or admission.workspace_id != workspace_id
            or admission.environment_id != environment_id
            or admission.source_object_id != source_object_id
            or admission.source_revision_id != source_revision_id
        ):
            return self._reject(
                OrdinaryProcessingEligibilityReason.PROCESSING_DISPOSITION_UNAVAILABLE,
                canonical_acceptance_id=acceptance.canonical_acceptance_id,
            )
        if admission.mode is ProcessingAdmissionMode.OPAQUE_ACCEPTANCE:
            return self._reject(
                OrdinaryProcessingEligibilityReason.OPAQUE_ACCEPTANCE,
                canonical_acceptance_id=acceptance.canonical_acceptance_id,
                processing_admission_id=admission.processing_admission_id,
                processing_mode=admission.mode,
            )
        if admission.mode is not ProcessingAdmissionMode.PROCESSOR_REQUIRED:
            return self._reject(
                OrdinaryProcessingEligibilityReason.PROCESSING_DISPOSITION_UNAVAILABLE,
                canonical_acceptance_id=acceptance.canonical_acceptance_id,
                processing_admission_id=admission.processing_admission_id,
            )
        return OrdinaryProcessingEligibility(
            eligible=True,
            reason=OrdinaryProcessingEligibilityReason.ELIGIBLE,
            canonical_acceptance_id=acceptance.canonical_acceptance_id,
            processing_admission_id=admission.processing_admission_id,
            processing_mode=admission.mode,
        )

    @staticmethod
    def _reject(
        reason: OrdinaryProcessingEligibilityReason,
        *,
        canonical_acceptance_id: UUID | None = None,
        processing_admission_id: UUID | None = None,
        processing_mode: ProcessingAdmissionMode | None = None,
    ) -> OrdinaryProcessingEligibility:
        return OrdinaryProcessingEligibility(
            eligible=False,
            reason=reason,
            canonical_acceptance_id=canonical_acceptance_id,
            processing_admission_id=processing_admission_id,
            processing_mode=processing_mode,
        )


__all__ = [
    "CanonicalCurrentProcessingDispositionAuthority",
    "CanonicalProcessingDispositionReader",
    "CurrentProcessingDispositionAuthority",
    "OrdinaryProcessingEligibility",
    "OrdinaryProcessingEligibilityReason",
]
