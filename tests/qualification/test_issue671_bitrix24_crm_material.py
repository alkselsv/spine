"""Executable offline qualification evidence for issue #671."""

from datetime import datetime, timezone
from uuid import UUID

from spine.domain.sources import (
    Bitrix24CrmMaterialAcceptance,
    Bitrix24CrmMaterialClaim,
    Bitrix24CrmMaterialHandoff,
)


NOW = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)


def _id(value: int) -> UUID:
    return UUID(f"00000000-0000-4000-8000-{value:012d}")


def test_issue671_accepted_handoff_has_no_disclosure_or_pricing_authority() -> None:
    claim = Bitrix24CrmMaterialClaim(
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
    acceptance = Bitrix24CrmMaterialAcceptance(
        acceptance_id=_id(10),
        claim_id=claim.claim_id,
        workspace_id=claim.workspace_id,
        environment_id=claim.environment_id,
        target_source_object_id=claim.target_source_object_id,
        target_source_revision_id=claim.target_source_revision_id,
        disk_file_id=claim.disk_file_id,
        disk_file_revision=claim.disk_file_revision,
        accepted_by=_id(11),
        accepted_at=NOW,
    )
    handoff = Bitrix24CrmMaterialHandoff(claim=claim, acceptance=acceptance)

    assert handoff.grants_source_disclosure is False
    assert handoff.grants_pricing_basis_authority is False
