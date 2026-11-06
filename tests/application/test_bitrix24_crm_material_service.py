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


class RepositorySpy:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def record_claim(self, claim: Bitrix24CrmMaterialClaim) -> Bitrix24CrmMaterialClaim:
        del claim
        self._fail_if_called("record_claim")

    async def resolve_claim(self, claim_id: UUID) -> Bitrix24CrmMaterialClaim | None:
        del claim_id
        self._fail_if_called("resolve_claim")

    async def validate_target(self, claim: Bitrix24CrmMaterialClaim) -> None:
        del claim
        self._fail_if_called("validate_target")

    async def accept_claim(
        self,
        *,
        claim_id: UUID,
        acceptance: Bitrix24CrmMaterialAcceptance,
    ) -> Bitrix24CrmMaterialHandoff:
        del claim_id, acceptance
        self._fail_if_called("accept_claim")

    async def revoke_claim(
        self, revocation: Bitrix24CrmMaterialRevocation
    ) -> Bitrix24CrmMaterialHandoff:
        del revocation
        self._fail_if_called("revoke_claim")

    async def resolve_handoff(self, claim_id: UUID) -> Bitrix24CrmMaterialHandoff | None:
        del claim_id
        self._fail_if_called("resolve_handoff")

    def _fail_if_called(self, method: str) -> None:
        self.calls.append(method)
        pytest.fail(f"legacy CRM service unexpectedly called repository.{method}")


@pytest.mark.asyncio
async def test_legacy_observation_denies_before_repository_access() -> None:
    repository = RepositorySpy()
    service = Bitrix24CrmMaterialAssociationService(repository)
    with pytest.raises(Bitrix24CrmMaterialV1OperationDeniedError):
        await service.observe(_claim())
    assert repository.calls == []


@pytest.mark.asyncio
async def test_legacy_acceptance_denies_before_repository_access() -> None:
    repository = RepositorySpy()
    service = Bitrix24CrmMaterialAssociationService(repository)
    with pytest.raises(Bitrix24CrmMaterialV1OperationDeniedError):
        await service.accept(claim_id=_id(1), acceptance=_acceptance())
    assert repository.calls == []


@pytest.mark.asyncio
async def test_legacy_revocation_denies_before_repository_access() -> None:
    repository = RepositorySpy()
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
