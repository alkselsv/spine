from datetime import datetime, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.domain.sources import (
    GoogleDriveFileKey,
    GoogleRegisterRowKey,
    RegisterAssociationAcceptance,
    RegisterAssociationHandoff,
    RegisterLinkRole,
    RegisterObservedLinkClaim,
)

WORKSPACE = UUID("10000000-0000-4000-8000-000000000001")
ENVIRONMENT = UUID("20000000-0000-4000-8000-000000000001")
ROW_OBJECT = UUID("30000000-0000-4000-8000-000000000001")
ROW_REVISION = UUID("40000000-0000-4000-8000-000000000001")
PROVENANCE = UUID("50000000-0000-4000-8000-000000000001")
CLAIM = UUID("60000000-0000-4000-8000-000000000001")
TARGET_OBJECT = UUID("70000000-0000-4000-8000-000000000001")
TARGET_REVISION = UUID("80000000-0000-4000-8000-000000000001")
ACTOR = UUID("90000000-0000-4000-8000-000000000001")
NOW = datetime(2026, 10, 27, tzinfo=timezone.utc)
DIGEST = "a" * 64


def _claim(
    role: RegisterLinkRole = RegisterLinkRole.DRIVE_MATERIAL,
) -> RegisterObservedLinkClaim:
    return RegisterObservedLinkClaim(
        claim_id=CLAIM,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        row_source_object_id=ROW_OBJECT,
        row_source_revision_id=ROW_REVISION,
        row_provider_identity=GoogleRegisterRowKey(
            connection_id=UUID("e0000000-0000-4000-8000-000000000001"),
            spreadsheet_id="spreadsheet-456",
            sheet_id=17,
            immutable_business_id="ROW-001",
        ),
        row_revision_digest=DIGEST,
        row_original_sha256=DIGEST,
        role=role,
        claimed_external_identifier="drive-file-123",
        external_identifier_type="google_drive_file_id",
        extraction_profile="google-sheets-row-extraction:v1",
        evidence_locator="column:final_document_id",
        evidence_digest=DIGEST,
        provenance_id=PROVENANCE,
        observed_at=NOW,
    )


def _acceptance(**changes: object) -> RegisterAssociationAcceptance:
    fields: dict[str, object] = {
        "acceptance_id": UUID("a0000000-0000-4000-8000-000000000001"),
        "claim_id": CLAIM,
        "workspace_id": WORKSPACE,
        "environment_id": ENVIRONMENT,
        "row_source_object_id": ROW_OBJECT,
        "row_source_revision_id": ROW_REVISION,
        "target_source_object_id": TARGET_OBJECT,
        "target_source_revision_id": TARGET_REVISION,
        "target_provider_identity": GoogleDriveFileKey(
            connection_id=UUID("b0000000-0000-4000-8000-000000000001"),
            file_id="drive-file-123",
        ),
        "role": RegisterLinkRole.DRIVE_MATERIAL,
        "accepted_by": ACTOR,
        "accepted_at": NOW,
    }
    fields.update(changes)
    return RegisterAssociationAcceptance.model_validate(fields)


def test_register_association_handoff_pins_exact_row_and_target_revisions() -> None:
    claim = _claim()
    acceptance = _acceptance()

    handoff = RegisterAssociationHandoff(claim=claim, acceptance=acceptance)

    assert handoff.acceptance.row_source_revision_id == ROW_REVISION
    assert handoff.acceptance.target_source_revision_id == TARGET_REVISION
    assert handoff.acceptance.target_provider_identity.file_id == "drive-file-123"


@pytest.mark.parametrize(
    "role",
    tuple(RegisterLinkRole),
)
def test_unresolved_register_claim_preserves_supported_role_and_future_owner(
    role: RegisterLinkRole,
) -> None:
    owner_by_role = {
        RegisterLinkRole.REQUEST: "commercial_request",
        RegisterLinkRole.MEETING: "commercial_meeting",
        RegisterLinkRole.CLIENT: "commercial_client",
        RegisterLinkRole.PROJECT: "commercial_project",
        RegisterLinkRole.HISTORICAL_ESTIMATE: "commercial_estimate_history",
        RegisterLinkRole.SOURCE_ESTIMATE: "commercial_source_estimate",
        RegisterLinkRole.FINAL_ESTIMATE: "commercial_final_estimate",
        RegisterLinkRole.DRIVE_MATERIAL: "commercial_drive_material",
    }

    handoff = RegisterAssociationHandoff(
        claim=_claim(role),
        unresolved_owner=owner_by_role[role],
        unresolved_reason="target_entity_not_modeled",
    )

    assert handoff.claim.role is role
    assert handoff.unresolved_owner == owner_by_role[role]


def test_register_association_rejects_claim_with_different_row_revision() -> None:
    claim = _claim()
    acceptance = _acceptance(
        row_source_revision_id=UUID("c0000000-0000-4000-8000-000000000001")
    )

    with pytest.raises(ValidationError, match="exact row revision"):
        RegisterAssociationHandoff(claim=claim, acceptance=acceptance)


def test_register_association_rejects_text_only_or_unvalidated_identifier() -> None:
    claim = _claim().model_copy(update={"external_identifier_type": "display_name"})

    with pytest.raises(ValidationError, match="exact row revision and scope"):
        RegisterAssociationHandoff(claim=claim, acceptance=_acceptance())


def test_register_association_rejects_cross_scope_acceptance() -> None:
    acceptance = _acceptance(workspace_id=UUID("d0000000-0000-4000-8000-000000000001"))

    with pytest.raises(ValidationError, match="exact row revision and scope"):
        RegisterAssociationHandoff(claim=_claim(), acceptance=acceptance)
