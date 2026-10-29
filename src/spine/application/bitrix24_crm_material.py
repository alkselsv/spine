"""Application seam for accepting governed Bitrix24 CRM material claims."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from spine.application.persistence.context import (
    EnvironmentScope,
    TrustedPersistenceContext,
)
from spine.domain.common import ContextOrigin
from spine.domain.sources import (
    Bitrix24CrmMaterialAcceptance,
    Bitrix24CrmMaterialClaim,
    Bitrix24CrmMaterialHandoff,
    Bitrix24CrmMaterialRevocation,
)


class Bitrix24CrmMaterialAssociationRepository(Protocol):
    async def record_claim(
        self, claim: Bitrix24CrmMaterialClaim
    ) -> Bitrix24CrmMaterialClaim: ...

    async def resolve_claim(
        self, claim_id: UUID
    ) -> Bitrix24CrmMaterialClaim | None: ...

    async def validate_target(
        self, claim: Bitrix24CrmMaterialClaim
    ) -> None: ...

    async def accept_claim(
        self,
        *,
        claim_id: UUID,
        acceptance: Bitrix24CrmMaterialAcceptance,
    ) -> Bitrix24CrmMaterialHandoff: ...

    async def revoke_claim(
        self, revocation: Bitrix24CrmMaterialRevocation
    ) -> Bitrix24CrmMaterialHandoff: ...

    async def resolve_handoff(
        self, claim_id: UUID
    ) -> Bitrix24CrmMaterialHandoff | None: ...


def validate_bitrix24_crm_material_acceptance_authority(
    context: TrustedPersistenceContext,
    acceptance: Bitrix24CrmMaterialAcceptance,
) -> None:
    """Bind acceptance to the trusted actor and exact workspace environment."""

    context.validate_shape()
    if (
        not isinstance(context.scope, EnvironmentScope)
        or context.scope.workspace_id != acceptance.workspace_id
        or context.scope.environment_id != acceptance.environment_id
    ):
        raise PermissionError("CRM material acceptance is outside the trusted scope")
    actor_id = (
        context.acting_subject_id
        if context.origin is ContextOrigin.INTERACTIVE
        else context.service_principal_id
    )
    if actor_id != acceptance.accepted_by:
        raise PermissionError("CRM material acceptance actor does not match trusted context")


class Bitrix24CrmMaterialAssociationService:
    """Keep observation, target validation, acceptance and revocation separate."""

    def __init__(
        self,
        repository: Bitrix24CrmMaterialAssociationRepository,
        *,
        context: TrustedPersistenceContext | None = None,
    ) -> None:
        self._repository = repository
        self._context = context

    async def observe(self, claim: Bitrix24CrmMaterialClaim) -> Bitrix24CrmMaterialHandoff:
        persisted = await self._repository.record_claim(claim)
        existing = await self._repository.resolve_handoff(persisted.claim_id)
        if existing is not None:
            return existing
        return Bitrix24CrmMaterialHandoff(claim=persisted)

    async def accept(
        self,
        *,
        claim_id: UUID,
        acceptance: Bitrix24CrmMaterialAcceptance,
    ) -> Bitrix24CrmMaterialHandoff:
        if self._context is not None:
            validate_bitrix24_crm_material_acceptance_authority(
                self._context, acceptance
            )
        claim = await self._repository.resolve_claim(claim_id)
        if claim is None:
            raise ValueError("CRM material claim is unavailable in this scope")
        if acceptance.claim_id != claim_id:
            raise ValueError("CRM material acceptance claim identity is invalid")
        await self._repository.validate_target(claim)
        return await self._repository.accept_claim(
            claim_id=claim_id, acceptance=acceptance
        )

    async def revoke(
        self, revocation: Bitrix24CrmMaterialRevocation
    ) -> Bitrix24CrmMaterialHandoff:
        return await self._repository.revoke_claim(revocation)


__all__ = [
    "Bitrix24CrmMaterialAssociationRepository",
    "Bitrix24CrmMaterialAssociationService",
    "validate_bitrix24_crm_material_acceptance_authority",
]
