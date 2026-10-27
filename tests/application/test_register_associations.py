from datetime import datetime, timezone
from uuid import UUID

import pytest

from spine.application.register_associations import RegisterAssociationService
from spine.application.register_associations import (
    RegisterLinkExtractionProfile,
    RegisterLinkField,
    extract_register_link_claims,
    validate_register_acceptance_authority,
)
from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
    TrustedContextProvenance,
    TrustedPersistenceContext,
)
from spine.domain.sources import (
    GoogleDriveFileKey,
    GoogleRegisterRowKey,
    GoogleSheetCell,
    GoogleSheetCellValue,
    GoogleSheetNumberFormat,
    GoogleSheetRowOriginal,
    RegisterAssociationAcceptance,
    RegisterLinkRole,
    RegisterObservedLinkClaim,
)
from spine.domain.common import ContextOrigin

NOW = datetime(2026, 10, 27, tzinfo=timezone.utc)


def _claim() -> RegisterObservedLinkClaim:
    return RegisterObservedLinkClaim(
        claim_id=UUID("60000000-0000-4000-8000-000000000001"),
        workspace_id=UUID("10000000-0000-4000-8000-000000000001"),
        environment_id=UUID("20000000-0000-4000-8000-000000000001"),
        row_source_object_id=UUID("30000000-0000-4000-8000-000000000001"),
        row_source_revision_id=UUID("40000000-0000-4000-8000-000000000001"),
        row_provider_identity=GoogleRegisterRowKey(
            connection_id=UUID("e0000000-0000-4000-8000-000000000001"),
            spreadsheet_id="spreadsheet-456",
            sheet_id=17,
            immutable_business_id="ROW-001",
        ),
        row_revision_digest="a" * 64,
        row_original_sha256="c" * 64,
        role=RegisterLinkRole.DRIVE_MATERIAL,
        claimed_external_identifier="drive-file-123",
        external_identifier_type="google_drive_file_id",
        extraction_profile="google-sheets-row-extraction:v1",
        evidence_locator="column:final_document_id",
        evidence_digest="a" * 64,
        provenance_id=UUID("50000000-0000-4000-8000-000000000001"),
        observed_at=NOW,
    )


def _acceptance() -> RegisterAssociationAcceptance:
    return RegisterAssociationAcceptance(
        acceptance_id=UUID("a0000000-0000-4000-8000-000000000001"),
        claim_id=UUID("60000000-0000-4000-8000-000000000001"),
        workspace_id=UUID("10000000-0000-4000-8000-000000000001"),
        environment_id=UUID("20000000-0000-4000-8000-000000000001"),
        row_source_object_id=UUID("30000000-0000-4000-8000-000000000001"),
        row_source_revision_id=UUID("40000000-0000-4000-8000-000000000001"),
        target_source_object_id=UUID("70000000-0000-4000-8000-000000000001"),
        target_source_revision_id=UUID("80000000-0000-4000-8000-000000000001"),
        target_provider_identity=GoogleDriveFileKey(
            connection_id=UUID("b0000000-0000-4000-8000-000000000001"),
            file_id="drive-file-123",
        ),
        role=RegisterLinkRole.DRIVE_MATERIAL,
        accepted_by=UUID("90000000-0000-4000-8000-000000000001"),
        accepted_at=NOW,
    )


def _trusted_context(**overrides: object) -> TrustedPersistenceContext:
    values: dict[str, object] = {
        "scope": EnvironmentScope(
            workspace_id=UUID("10000000-0000-4000-8000-000000000001"),
            environment_id=UUID("20000000-0000-4000-8000-000000000001"),
        ),
        "origin": ContextOrigin.INTERACTIVE,
        "purpose": PersistencePurpose("google_register_association"),
        "operation": PersistenceOperation("register_association_accept"),
        "trace_id": UUID("f0000000-0000-4000-8000-000000000001"),
        "acting_subject_id": UUID("90000000-0000-4000-8000-000000000001"),
        "service_principal_id": None,
        "provenance": TrustedContextProvenance(
            issuer_id=UUID("f1000000-0000-4000-8000-000000000001"),
            signature=b"trusted-test-context",
        ),
    }
    values.update(overrides)
    return TrustedPersistenceContext(**values)


class InMemoryAssociationRepository:
    def __init__(self) -> None:
        self.claims: dict[UUID, RegisterObservedLinkClaim] = {}
        self.acceptances: dict[UUID, RegisterAssociationAcceptance] = {}
        self.record_calls = 0

    async def record_claim(
        self, claim: RegisterObservedLinkClaim
    ) -> RegisterObservedLinkClaim:
        existing = self.claims.get(claim.claim_id)
        if existing is not None and existing != claim:
            raise ValueError("claim identity conflict")
        self.claims.setdefault(claim.claim_id, claim)
        self.record_calls += 1
        return self.claims[claim.claim_id]

    async def accept_claim(
        self, *, claim_id: UUID, acceptance: RegisterAssociationAcceptance
    ):
        from spine.domain.sources import RegisterAssociationHandoff

        claim = self.claims[claim_id]
        existing = self.acceptances.get(claim_id)
        if existing is not None and existing != acceptance:
            raise ValueError("claim has another accepted association")
        self.acceptances.setdefault(claim_id, acceptance)
        return RegisterAssociationHandoff(
            claim=claim, acceptance=self.acceptances[claim_id]
        )

    async def resolve_handoff(self, claim_id: UUID):
        from spine.domain.sources import RegisterAssociationHandoff

        claim = self.claims.get(claim_id)
        if claim is None:
            return None
        acceptance = self.acceptances.get(claim_id)
        return (
            None
            if acceptance is None
            else RegisterAssociationHandoff(claim=claim, acceptance=acceptance)
        )


@pytest.mark.asyncio
async def test_observe_register_claim_is_idempotent_and_returns_unresolved_handoff() -> (
    None
):
    repository = InMemoryAssociationRepository()
    service = RegisterAssociationService(repository)
    claim = _claim()

    first = await service.observe(claim)
    second = await service.observe(claim)

    assert first == second
    assert first.unresolved_owner == "commercial_drive_material"
    assert first.unresolved_reason == "target_entity_not_modeled"
    assert len(repository.claims) == 1


@pytest.mark.asyncio
async def test_accept_register_claim_replay_returns_same_revision_scoped_handoff() -> (
    None
):
    repository = InMemoryAssociationRepository()
    service = RegisterAssociationService(repository)
    claim = _claim()
    await service.observe(claim)

    first = await service.accept(claim_id=claim.claim_id, acceptance=_acceptance())
    replay = await service.accept(claim_id=claim.claim_id, acceptance=_acceptance())

    assert first == replay
    assert first.acceptance.target_source_revision_id == UUID(
        "80000000-0000-4000-8000-000000000001"
    )


@pytest.mark.asyncio
async def test_observe_returns_persisted_acceptance_after_replay() -> None:
    repository = InMemoryAssociationRepository()
    service = RegisterAssociationService(repository)
    claim = _claim()
    await service.observe(claim)
    accepted = await service.accept(claim_id=claim.claim_id, acceptance=_acceptance())

    replay = await service.observe(claim)

    assert replay == accepted


def test_acceptance_requires_authenticated_actor_and_matching_scope() -> None:
    acceptance = _acceptance()

    validate_register_acceptance_authority(_trusted_context(), acceptance)

    with pytest.raises(PermissionError, match="actor does not match"):
        validate_register_acceptance_authority(
            _trusted_context(
                acting_subject_id=UUID("f2000000-0000-4000-8000-000000000001")
            ),
            acceptance,
        )
    with pytest.raises(PermissionError, match="outside the trusted scope"):
        validate_register_acceptance_authority(
            _trusted_context(
                scope=EnvironmentScope(
                    workspace_id=UUID("f3000000-0000-4000-8000-000000000001"),
                    environment_id=UUID("20000000-0000-4000-8000-000000000001"),
                )
            ),
            acceptance,
        )


def test_extract_register_claims_from_effective_cells_is_revision_scoped_and_replayable() -> (
    None
):
    row = GoogleSheetRowOriginal(
        key=GoogleRegisterRowKey(
            connection_id=UUID("e0000000-0000-4000-8000-000000000001"),
            spreadsheet_id="spreadsheet-456",
            sheet_id=17,
            immutable_business_id="ROW-001",
        ),
        spreadsheet_locale="en_US",
        spreadsheet_time_zone="UTC",
        cells=(
            GoogleSheetCell(
                column_key="final_document_id",
                entered=GoogleSheetCellValue(kind="string", value="drive-file-123"),
                effective=GoogleSheetCellValue(kind="string", value="drive-file-123"),
                formatted="Drive file",
                number_format=GoogleSheetNumberFormat(type="TEXT"),
            ),
        ),
    )
    profile = RegisterLinkExtractionProfile(
        profile_id="register-links:v1",
        fields=(
            RegisterLinkField(
                role=RegisterLinkRole.DRIVE_MATERIAL,
                column_key="final_document_id",
                external_identifier_type="google_drive_file_id",
            ),
        ),
    )
    inputs = {
        "row": row,
        "workspace_id": UUID("10000000-0000-4000-8000-000000000001"),
        "environment_id": UUID("20000000-0000-4000-8000-000000000001"),
        "row_source_object_id": UUID("30000000-0000-4000-8000-000000000001"),
        "row_source_revision_id": UUID("40000000-0000-4000-8000-000000000001"),
        "row_revision_digest": "a" * 64,
        "provenance_id": UUID("50000000-0000-4000-8000-000000000001"),
        "profile": profile,
        "observed_at": NOW,
    }

    first = extract_register_link_claims(**inputs)
    replay = extract_register_link_claims(**inputs)

    assert first == replay
    assert first[0].claimed_external_identifier == "drive-file-123"
    assert first[0].row_provider_identity == row.key
    assert first[0].row_source_revision_id == inputs["row_source_revision_id"]
