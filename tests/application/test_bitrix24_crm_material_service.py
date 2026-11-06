from datetime import datetime, timezone
from uuid import UUID

import pytest

from spine.application.bitrix24_crm_material import (
    Bitrix24CrmMaterialAssociationService,
)
from spine.domain.sources import (
    Bitrix24CrmMaterialAcceptance,
    Bitrix24CrmMaterialClaim,
    Bitrix24CrmMaterialHandoff,
    Bitrix24CrmMaterialRevocation,
    Bitrix24CrmMaterialState,
)
from spine.domain.sources.bitrix24_crm import Bitrix24CrmMaterialV1OperationDeniedError

NOW = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)


def _id(value: int) -> UUID:
    return UUID(f"00000000-0000-4000-8000-{value:012d}")


def _claim() -> Bitrix24CrmMaterialClaim:
    return Bitrix24CrmMaterialClaim(
        claim_id=_id(1),
        workspace_id=_id(2),
        environment_id=_id(3),
        connection_id=_id(4),
        connection_version_id=_id(5),
        resource_scope_reference=_id(6),
        crm_entity_type="deal",
        crm_entity_id="932",
        crm_activity_id="501",
        crm_activity_revision="activity-v7",
        crm_attachment_id="attachment-1",
        relationship_revision="link-v3",
        target_source_object_id=_id(7),
        target_source_revision_id=_id(8),
        disk_file_id="9043",
        disk_file_revision="42",
        binding_evidence_digest="a" * 64,
        observed_at=NOW,
    )


def _acceptance() -> Bitrix24CrmMaterialAcceptance:
    return Bitrix24CrmMaterialAcceptance(
        acceptance_id=_id(10),
        claim_id=_id(1),
        workspace_id=_id(2),
        environment_id=_id(3),
        target_source_object_id=_id(7),
        target_source_revision_id=_id(8),
        disk_file_id="9043",
        disk_file_revision="42",
        accepted_by=_id(11),
        accepted_at=NOW,
    )


class Repository:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.claims: dict[UUID, Bitrix24CrmMaterialClaim] = {}
        self.handoffs: dict[UUID, Bitrix24CrmMaterialHandoff] = {}
        self.target_valid = True

    async def record_claim(self, claim: Bitrix24CrmMaterialClaim) -> Bitrix24CrmMaterialClaim:
        self.calls.append("record_claim")
        existing = self.claims.setdefault(claim.claim_id, claim)
        if existing != claim:
            raise ValueError("claim identity conflict")
        return existing

    async def resolve_claim(self, claim_id: UUID) -> Bitrix24CrmMaterialClaim | None:
        self.calls.append("resolve_claim")
        return self.claims.get(claim_id)

    async def validate_target(self, claim: Bitrix24CrmMaterialClaim) -> None:
        self.calls.append("validate_target")
        if not self.target_valid:
            raise ValueError("target Disk revision is stale or inaccessible")

    async def accept_claim(
        self,
        *,
        claim_id: UUID,
        acceptance: Bitrix24CrmMaterialAcceptance,
    ) -> Bitrix24CrmMaterialHandoff:
        self.calls.append("accept_claim")
        claim = self.claims[claim_id]
        existing = self.handoffs.get(claim_id)
        handoff = Bitrix24CrmMaterialHandoff(claim=claim, acceptance=acceptance)
        if existing is not None and existing != handoff:
            raise ValueError("claim already has another accepted association")
        self.handoffs[claim_id] = handoff
        return handoff

    async def revoke_claim(
        self, revocation: Bitrix24CrmMaterialRevocation
    ) -> Bitrix24CrmMaterialHandoff:
        self.calls.append("revoke_claim")
        claim = self.claims[revocation.claim_id]
        existing = self.handoffs.get(revocation.claim_id)
        if existing is not None and existing.revocation is not None:
            if existing.revocation != revocation:
                raise ValueError("revocation identity conflict")
            return existing
        handoff = Bitrix24CrmMaterialHandoff(claim=claim, revocation=revocation)
        self.handoffs[revocation.claim_id] = handoff
        return handoff

    async def resolve_handoff(self, claim_id: UUID) -> Bitrix24CrmMaterialHandoff | None:
        self.calls.append("resolve_handoff")
        return self.handoffs.get(claim_id)


@pytest.mark.asyncio
async def test_legacy_observation_denies_before_repository_access() -> None:
    repository = Repository()
    service = Bitrix24CrmMaterialAssociationService(repository)
    with pytest.raises(Bitrix24CrmMaterialV1OperationDeniedError):
        await service.observe(_claim())
    assert repository.calls == []


@pytest.mark.asyncio
async def test_legacy_acceptance_denies_before_repository_access() -> None:
    repository = Repository()
    repository.target_valid = False
    service = Bitrix24CrmMaterialAssociationService(repository)
    with pytest.raises(Bitrix24CrmMaterialV1OperationDeniedError):
        await service.accept(claim_id=_id(1), acceptance=_acceptance())
    assert repository.calls == []


@pytest.mark.asyncio
async def test_legacy_revocation_denies_before_repository_access() -> None:
    repository = Repository()
    service = Bitrix24CrmMaterialAssociationService(repository)
    claim = _claim()
    revocation = Bitrix24CrmMaterialRevocation(
        revocation_id=_id(12),
        claim_id=claim.claim_id,
        workspace_id=claim.workspace_id,
        environment_id=claim.environment_id,
        state=Bitrix24CrmMaterialState.RELATION_REVOKED,
        reason="activity_attachment_removed",
        observed_at=NOW,
    )
    with pytest.raises(Bitrix24CrmMaterialV1OperationDeniedError):
        await service.revoke(revocation)
    assert repository.calls == []
