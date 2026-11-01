"""Deterministic processing-disposition fake for offline boundary tests."""

from __future__ import annotations

from uuid import UUID

from spine.application.processing_disposition import (
    OrdinaryProcessingEligibility,
    OrdinaryProcessingEligibilityReason,
)


class DeterministicCurrentProcessingDispositionAuthority:
    def __init__(self, *, eligible: bool = True) -> None:
        self.eligible = eligible
        self.calls: list[tuple[UUID, UUID, UUID, UUID]] = []

    async def resolve(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        source_object_id: UUID,
        source_revision_id: UUID,
    ) -> OrdinaryProcessingEligibility:
        self.calls.append(
            (
                workspace_id,
                environment_id,
                source_object_id,
                source_revision_id,
            )
        )
        return OrdinaryProcessingEligibility(
            eligible=self.eligible,
            reason=(
                OrdinaryProcessingEligibilityReason.ELIGIBLE
                if self.eligible
                else OrdinaryProcessingEligibilityReason.OPAQUE_ACCEPTANCE
            ),
        )


__all__ = ["DeterministicCurrentProcessingDispositionAuthority"]
