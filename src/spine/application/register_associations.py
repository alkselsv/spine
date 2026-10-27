"""Use cases and repository contracts for register link claims."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Protocol
from uuid import UUID, uuid5

from pydantic import Field, model_validator

from spine.application.persistence.context import (
    EnvironmentScope,
    TrustedPersistenceContext,
)
from spine.domain.common import ContextOrigin, DefinitionModel, VersionIdentifier

from spine.domain.sources import (
    RegisterAssociationAcceptance,
    RegisterAssociationFutureOwner,
    RegisterAssociationHandoff,
    RegisterExternalIdentifierType,
    RegisterLinkRole,
    RegisterObservedLinkClaim,
    GoogleSheetRowOriginal,
    google_sheet_row_original_bytes,
)

_FUTURE_OWNERS = {
    RegisterLinkRole.REQUEST: RegisterAssociationFutureOwner.COMMERCIAL_REQUEST,
    RegisterLinkRole.MEETING: RegisterAssociationFutureOwner.COMMERCIAL_MEETING,
    RegisterLinkRole.CLIENT: RegisterAssociationFutureOwner.COMMERCIAL_CLIENT,
    RegisterLinkRole.PROJECT: RegisterAssociationFutureOwner.COMMERCIAL_PROJECT,
    RegisterLinkRole.HISTORICAL_ESTIMATE: RegisterAssociationFutureOwner.COMMERCIAL_ESTIMATE_HISTORY,
    RegisterLinkRole.SOURCE_ESTIMATE: RegisterAssociationFutureOwner.COMMERCIAL_SOURCE_ESTIMATE,
    RegisterLinkRole.FINAL_ESTIMATE: RegisterAssociationFutureOwner.COMMERCIAL_FINAL_ESTIMATE,
    RegisterLinkRole.DRIVE_MATERIAL: RegisterAssociationFutureOwner.COMMERCIAL_DRIVE_MATERIAL,
}
_CLAIM_NAMESPACE = UUID("6ae904ad-c3a9-4a33-9124-03aa8f51fa86")


class RegisterLinkField(DefinitionModel):
    """One configured row column that carries a typed link claim."""

    role: RegisterLinkRole
    column_key: str = Field(min_length=1, max_length=256)
    external_identifier_type: RegisterExternalIdentifierType


class RegisterLinkExtractionProfile(DefinitionModel):
    """Versioned mapping from accepted register columns to link roles."""

    profile_id: VersionIdentifier
    fields: tuple[RegisterLinkField, ...] = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_fields(self) -> "RegisterLinkExtractionProfile":
        keys = tuple((field.role, field.column_key) for field in self.fields)
        if len(set(keys)) != len(keys):
            raise ValueError("role and column mappings must be unique")
        return self


def extract_register_link_claims(
    *,
    row: GoogleSheetRowOriginal,
    workspace_id: UUID,
    environment_id: UUID,
    row_source_object_id: UUID,
    row_source_revision_id: UUID,
    row_revision_digest: str,
    provenance_id: UUID,
    profile: RegisterLinkExtractionProfile,
    observed_at: datetime,
) -> tuple[RegisterObservedLinkClaim, ...]:
    """Extract identifier claims from exact effective cell values and lineage."""

    cells = {cell.column_key: cell for cell in row.cells}
    if any(field.column_key not in cells for field in profile.fields):
        raise ValueError("link extraction profile references a missing row column")
    row_original_sha256 = hashlib.sha256(
        google_sheet_row_original_bytes(row)
    ).hexdigest()
    claims: list[RegisterObservedLinkClaim] = []
    for field in profile.fields:
        cell = cells[field.column_key]
        value = cell.effective
        if value.kind == "blank":
            continue
        if value.kind not in {"string", "number"} or not isinstance(value.value, str):
            raise ValueError(
                "link identifier cell must have a string or numeric effective value"
            )
        identifier = value.value.strip()
        if not identifier:
            continue
        cell_record = cell.model_dump(mode="json")
        evidence_digest = hashlib.sha256(
            json.dumps(
                cell_record,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        claim_id = uuid5(
            _CLAIM_NAMESPACE,
            f"{row_source_revision_id}:{field.role.value}:{field.column_key}:{evidence_digest}",
        )
        claims.append(
            RegisterObservedLinkClaim(
                claim_id=claim_id,
                workspace_id=workspace_id,
                environment_id=environment_id,
                row_source_object_id=row_source_object_id,
                row_source_revision_id=row_source_revision_id,
                row_provider_identity=row.key,
                row_revision_digest=row_revision_digest,
                row_original_sha256=row_original_sha256,
                role=field.role,
                claimed_external_identifier=identifier,
                external_identifier_type=field.external_identifier_type,
                extraction_profile=profile.profile_id,
                evidence_locator=f"column:{field.column_key}",
                evidence_digest=evidence_digest,
                provenance_id=provenance_id,
                observed_at=observed_at,
            )
        )
    return tuple(claims)


class RegisterAssociationRepository(Protocol):
    """Persist immutable claims and explicit revision-to-revision acceptance."""

    async def record_claim(
        self, claim: RegisterObservedLinkClaim
    ) -> RegisterObservedLinkClaim: ...

    async def accept_claim(
        self,
        *,
        claim_id: UUID,
        acceptance: RegisterAssociationAcceptance,
    ) -> RegisterAssociationHandoff: ...

    async def resolve_handoff(
        self, claim_id: UUID
    ) -> RegisterAssociationHandoff | None: ...


def validate_register_acceptance_authority(
    context: TrustedPersistenceContext,
    acceptance: RegisterAssociationAcceptance,
) -> None:
    """Bind acceptance to the authenticated actor and exact environment scope."""

    context.validate_shape()
    if (
        not isinstance(context.scope, EnvironmentScope)
        or context.scope.workspace_id != acceptance.workspace_id
        or context.scope.environment_id != acceptance.environment_id
    ):
        raise PermissionError(
            "register association acceptance is outside the trusted scope"
        )
    actor_id = (
        context.acting_subject_id
        if context.origin is ContextOrigin.INTERACTIVE
        else context.service_principal_id
    )
    if actor_id != acceptance.accepted_by:
        raise PermissionError(
            "register association actor does not match trusted context"
        )


class RegisterAssociationService:
    """Turn persisted row claims into typed accepted or unresolved handoffs."""

    def __init__(self, repository: RegisterAssociationRepository) -> None:
        self._repository = repository

    async def observe(
        self, claim: RegisterObservedLinkClaim
    ) -> RegisterAssociationHandoff:
        persisted = await self._repository.record_claim(claim)
        existing = await self._repository.resolve_handoff(persisted.claim_id)
        if existing is not None:
            return existing
        return RegisterAssociationHandoff(
            claim=persisted,
            unresolved_owner=_FUTURE_OWNERS[persisted.role],
            unresolved_reason="target_entity_not_modeled",
        )

    async def accept(
        self,
        *,
        claim_id: UUID,
        acceptance: RegisterAssociationAcceptance,
    ) -> RegisterAssociationHandoff:
        return await self._repository.accept_claim(
            claim_id=claim_id, acceptance=acceptance
        )


__all__ = [
    "RegisterAssociationRepository",
    "RegisterAssociationService",
    "RegisterLinkExtractionProfile",
    "RegisterLinkField",
    "extract_register_link_claims",
    "validate_register_acceptance_authority",
]
