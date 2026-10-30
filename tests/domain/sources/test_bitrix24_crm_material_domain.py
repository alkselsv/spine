from datetime import datetime, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.domain.sources import (
    Bitrix24CrmMaterialAcceptance,
    Bitrix24CrmMaterialClaim,
    Bitrix24CrmMaterialHandoff,
    Bitrix24CrmMaterialState,
    Bitrix24CrmMaterialRevocation,
)

NOW = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)


def _id(value: int) -> UUID:
    return UUID(f"00000000-0000-4000-8000-{value:012d}")


def _claim(**changes: object) -> Bitrix24CrmMaterialClaim:
    values: dict[str, object] = {
        "claim_id": _id(1),
        "workspace_id": _id(2),
        "environment_id": _id(3),
        "connection_id": _id(4),
        "connection_version_id": _id(5),
        "resource_scope_reference": _id(6),
        "crm_entity_type": "deal",
        "crm_entity_id": "932",
        "crm_activity_id": "501",
        "crm_activity_revision": "activity-v7",
        "crm_attachment_id": "attachment-1",
        "relationship_revision": "link-v3",
        "target_source_object_id": _id(7),
        "target_source_revision_id": _id(8),
        "disk_file_id": "9043",
        "disk_file_revision": "42",
        "binding_evidence_digest": "a" * 64,
        "observed_at": NOW,
    }
    values.update(changes)
    return Bitrix24CrmMaterialClaim(**values)


def _acceptance(**changes: object) -> Bitrix24CrmMaterialAcceptance:
    values: dict[str, object] = {
        "acceptance_id": _id(10),
        "claim_id": _id(1),
        "workspace_id": _id(2),
        "environment_id": _id(3),
        "target_source_object_id": _id(7),
        "target_source_revision_id": _id(8),
        "disk_file_id": "9043",
        "disk_file_revision": "42",
        "accepted_by": _id(11),
        "accepted_at": NOW,
    }
    values.update(changes)
    return Bitrix24CrmMaterialAcceptance(**values)


def test_claim_acceptance_pins_crm_binding_and_exact_disk_revision() -> None:
    claim = _claim()
    handoff = Bitrix24CrmMaterialHandoff(
        claim=claim,
        acceptance=_acceptance(),
    )

    assert handoff.acceptance is not None
    assert handoff.acceptance.target_source_revision_id == _id(8)
    assert handoff.grants_source_disclosure is False
    assert handoff.grants_pricing_basis_authority is False


def test_acceptance_rejects_mismatched_disk_revision_or_scope() -> None:
    with pytest.raises(ValidationError, match="exact CRM binding"):
        Bitrix24CrmMaterialHandoff(
            claim=_claim(),
            acceptance=_acceptance(disk_file_revision="stale"),
        )
    with pytest.raises(ValidationError, match="exact CRM binding"):
        Bitrix24CrmMaterialHandoff(
            claim=_claim(),
            acceptance=_acceptance(workspace_id=_id(99)),
        )


def test_revocation_is_explicit_and_does_not_delete_the_original_claim() -> None:
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
    handoff = Bitrix24CrmMaterialHandoff(claim=claim, revocation=revocation)

    assert handoff.claim is claim
    assert handoff.state is Bitrix24CrmMaterialState.RELATION_REVOKED
