"""Persistence seam for immutable trusted-content verifier evidence."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from spine.domain.sources import TrustedContentVerificationEvidence


class TrustedContentEvidenceConflict(ValueError):
    pass


class TrustedContentEvidenceRepository(Protocol):
    async def append(
        self, evidence: TrustedContentVerificationEvidence
    ) -> TrustedContentVerificationEvidence: ...

    async def resolve(
        self, evidence_id: UUID
    ) -> TrustedContentVerificationEvidence | None: ...

    async def list_for_association(
        self, association_id: UUID
    ) -> tuple[TrustedContentVerificationEvidence, ...]: ...


__all__ = [
    "TrustedContentEvidenceConflict",
    "TrustedContentEvidenceRepository",
]
