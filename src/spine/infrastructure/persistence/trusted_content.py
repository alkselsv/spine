"""Small in-memory adapter for the trusted-content evidence persistence seam."""

from __future__ import annotations

from uuid import UUID

from spine.application.persistence.context import EnvironmentScope
from spine.application.persistence.trusted_content import TrustedContentEvidenceConflict
from spine.domain.sources import TrustedContentVerificationEvidence


class InMemoryTrustedContentEvidenceRepository:
    def __init__(
        self,
        *,
        scope: EnvironmentScope,
        records: dict[UUID, TrustedContentVerificationEvidence] | None = None,
    ) -> None:
        self._scope = scope
        self._records = records if records is not None else {}

    def _in_scope(self, evidence: TrustedContentVerificationEvidence) -> bool:
        return (
            evidence.workspace_id == self._scope.workspace_id
            and evidence.environment_id == self._scope.environment_id
        )

    async def append(
        self, evidence: TrustedContentVerificationEvidence
    ) -> TrustedContentVerificationEvidence:
        if not self._in_scope(evidence):
            raise TrustedContentEvidenceConflict("trusted content evidence scope mismatch")
        existing = self._records.get(evidence.evidence_id)
        if existing is not None:
            if existing != evidence:
                raise TrustedContentEvidenceConflict(
                    "trusted content evidence is immutable"
                )
            return existing.model_copy(deep=True)
        if any(
            self._in_scope(item)
            and item.evidence_digest == evidence.evidence_digest
            for item in self._records.values()
        ):
            raise TrustedContentEvidenceConflict("evidence digest already exists")
        self._records[evidence.evidence_id] = evidence.model_copy(deep=True)
        return evidence.model_copy(deep=True)

    async def resolve(
        self, evidence_id: UUID
    ) -> TrustedContentVerificationEvidence | None:
        value = self._records.get(evidence_id)
        if value is None or not self._in_scope(value):
            return None
        return value.model_copy(deep=True)

    async def list_for_association(
        self, association_id: UUID
    ) -> tuple[TrustedContentVerificationEvidence, ...]:
        return tuple(
            item.model_copy(deep=True)
            for item in sorted(
                self._records.values(), key=lambda value: (value.verified_at, value.evidence_id)
            )
            if self._in_scope(item) and item.receipt_association_id == association_id
        )


__all__ = ["InMemoryTrustedContentEvidenceRepository"]
