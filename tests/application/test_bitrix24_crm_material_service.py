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
        self.claims: dict[UUID, Bitrix24CrmMaterialClaim] = {}
        self.handoffs: dict[UUID, Bitrix24CrmMaterialHandoff] = {}
        self.target_valid = True

    async def record_claim(self, claim):
        existing = self.claims.setdefault(claim.claim_id, claim)
        if existing != claim:
            raise ValueError("claim identity conflict")
        return existing

    async def resolve_claim(self, claim_id):
        return self.claims.get(claim_id)

    async def validate_target(self, claim):
        if not self.target_valid:
            raise ValueError("target Disk revision is stale or inaccessible")

    async def accept_claim(self, *, claim_id, acceptance):
        claim = self.claims[claim_id]
        existing = self.handoffs.get(claim_id)
        handoff = Bitrix24CrmMaterialHandoff(claim=claim, acceptance=acceptance)
        if existing is not None and existing != handoff:
            raise ValueError("claim already has another accepted association")
        self.handoffs[claim_id] = handoff
        return handoff

    async def revoke_claim(self, revocation):
        claim = self.claims[revocation.claim_id]
        handoff = Bitrix24CrmMaterialHandoff(claim=claim, revocation=revocation)
        self.handoffs[revocation.claim_id] = handoff
        return handoff

    async def resolve_handoff(self, claim_id):
        return self.handoffs.get(claim_id)


@pytest.mark.asyncio
async def test_observation_and_acceptance_are_idempotent_and_revision_scoped() -> None:
    repository = Repository()
    service = Bitrix24CrmMaterialAssociationService(repository)
    claim = _claim()

    first = await service.observe(claim)
    replay = await service.observe(claim)
    accepted = await service.accept(claim_id=claim.claim_id, acceptance=_acceptance())
    accepted_replay = await service.accept(
        claim_id=claim.claim_id, acceptance=_acceptance()
    )

    assert first == replay
    assert accepted == accepted_replay
    assert accepted.acceptance is not None
    assert accepted.acceptance.target_source_revision_id == _id(8)


@pytest.mark.asyncio
async def test_stale_target_revision_cannot_be_accepted() -> None:
    repository = Repository()
    repository.target_valid = False
    service = Bitrix24CrmMaterialAssociationService(repository)
    claim = _claim()
    await service.observe(claim)

    with pytest.raises(ValueError, match="stale or inaccessible"):
        await service.accept(claim_id=claim.claim_id, acceptance=_acceptance())


@pytest.mark.asyncio
async def test_relation_revocation_remains_visible_after_acceptance() -> None:
    repository = Repository()
    service = Bitrix24CrmMaterialAssociationService(repository)
    claim = _claim()
    await service.observe(claim)
    await service.accept(claim_id=claim.claim_id, acceptance=_acceptance())

    revoked = await service.revoke(
        Bitrix24CrmMaterialRevocation(
            revocation_id=_id(12),
            claim_id=claim.claim_id,
            workspace_id=claim.workspace_id,
            environment_id=claim.environment_id,
            state=Bitrix24CrmMaterialState.RELATION_REVOKED,
            reason="activity_attachment_removed",
            observed_at=NOW,
        )
    )

    assert revoked.state is Bitrix24CrmMaterialState.RELATION_REVOKED
    assert revoked.claim == claim
