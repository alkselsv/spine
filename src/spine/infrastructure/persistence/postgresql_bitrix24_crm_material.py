"""Tenant-scoped persistence for Bitrix24 CRM material relationship evidence."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from spine.application.bitrix24_crm_material import (
    validate_bitrix24_crm_material_acceptance_authority,
)
from spine.application.persistence.context import PersistenceOperation, TrustedPersistenceContext
from spine.domain.sources import (
    Bitrix24CrmMaterialAcceptance,
    Bitrix24CrmMaterialClaim,
    Bitrix24CrmMaterialHandoff,
    Bitrix24CrmMaterialRevocation,
)
from spine.infrastructure.persistence.postgresql_mappings import (
    bitrix24_crm_material_acceptances,
    bitrix24_crm_material_claims,
    bitrix24_crm_material_revocations,
    canonical_acceptances,
    current_source_heads,
    current_access_policies,
    source_objects,
    source_revisions,
)


class PostgreSQLBitrix24CrmMaterialAssociationRepository:
    """Persist claims append-only and accept only the current exact Disk revision."""

    def __init__(
        self,
        uow_factory: Callable[[TrustedPersistenceContext], object],
        context_factory: Callable[[PersistenceOperation, UUID], TrustedPersistenceContext],
    ) -> None:
        self._uow_factory = uow_factory
        self._context_factory = context_factory

    async def record_claim(self, claim: Bitrix24CrmMaterialClaim) -> Bitrix24CrmMaterialClaim:
        async with self._uow("bitrix24_crm_material_observe", claim.claim_id) as uow:
            row = await self._load_claim(uow, claim.claim_id)
            if row is not None:
                persisted = _claim_from_row(row)
                if persisted != claim:
                    raise ValueError("CRM material claim identity conflicts with persisted evidence")
                await uow.rollback()
                return persisted
            await self._assert_target_scope(uow, claim)
            await uow._execute(
                insert(bitrix24_crm_material_claims)
                .values(
                    claim_id=claim.claim_id,
                    workspace_id=claim.workspace_id,
                    environment_id=claim.environment_id,
                    target_source_object_id=claim.target_source_object_id,
                    target_source_revision_id=claim.target_source_revision_id,
                    disk_file_id=claim.disk_file_id,
                    disk_file_revision=claim.disk_file_revision,
                    record=claim.model_dump(mode="json"),
                    observed_at=claim.observed_at,
                )
                .on_conflict_do_nothing(
                    index_elements=[bitrix24_crm_material_claims.c.claim_id]
                )
            )
            persisted_row = await self._load_claim(uow, claim.claim_id)
            if persisted_row is None or _claim_from_row(persisted_row) != claim:
                raise ValueError("CRM material claim identity conflicts with persisted evidence")
            await uow.commit()
            return claim

    async def resolve_claim(self, claim_id: UUID) -> Bitrix24CrmMaterialClaim | None:
        async with self._uow("bitrix24_crm_material_read", claim_id) as uow:
            row = await self._load_claim(uow, claim_id)
            await uow.rollback()
            return None if row is None else _claim_from_row(row)

    async def validate_target(self, claim: Bitrix24CrmMaterialClaim) -> None:
        async with self._uow("bitrix24_crm_material_target_validate", claim.claim_id) as uow:
            await self._assert_target(uow, claim)
            await uow.rollback()

    async def accept_claim(
        self,
        *,
        claim_id: UUID,
        acceptance: Bitrix24CrmMaterialAcceptance,
    ) -> Bitrix24CrmMaterialHandoff:
        async with self._uow("bitrix24_crm_material_accept", claim_id) as uow:
            row = await self._load_claim(uow, claim_id)
            if row is None:
                raise ValueError("CRM material claim is unavailable in this tenant")
            claim = _claim_from_row(row)
            validate_bitrix24_crm_material_acceptance_authority(
                uow._active_context, acceptance
            )
            expected = Bitrix24CrmMaterialHandoff(claim=claim, acceptance=acceptance)
            await self._assert_target(uow, claim)
            existing_result = await uow._execute(
                select(bitrix24_crm_material_acceptances).where(
                    bitrix24_crm_material_acceptances.c.claim_id == claim_id
                )
            )
            existing = existing_result.mappings().one_or_none()
            if existing is not None:
                persisted = _acceptance_from_row(existing)
                if persisted != acceptance:
                    raise ValueError("CRM material claim already has another acceptance")
                await uow.rollback()
                return Bitrix24CrmMaterialHandoff(claim=claim, acceptance=persisted)
            await uow._execute(
                insert(bitrix24_crm_material_acceptances)
                .values(
                    acceptance_id=acceptance.acceptance_id,
                    claim_id=claim_id,
                    workspace_id=acceptance.workspace_id,
                    environment_id=acceptance.environment_id,
                    target_source_object_id=acceptance.target_source_object_id,
                    target_source_revision_id=acceptance.target_source_revision_id,
                    record=acceptance.model_dump(mode="json"),
                    accepted_at=acceptance.accepted_at,
                )
                .on_conflict_do_nothing(
                    index_elements=[bitrix24_crm_material_acceptances.c.claim_id]
                )
            )
            persisted_result = await uow._execute(
                select(bitrix24_crm_material_acceptances).where(
                    bitrix24_crm_material_acceptances.c.claim_id == claim_id
                )
            )
            persisted_row = persisted_result.mappings().one_or_none()
            if persisted_row is None:
                raise ValueError("CRM material acceptance was not persisted")
            persisted = _acceptance_from_row(persisted_row)
            if persisted != acceptance:
                raise ValueError("CRM material claim already has another acceptance")
            await uow.commit()
            return expected

    async def revoke_claim(
        self, revocation: Bitrix24CrmMaterialRevocation
    ) -> Bitrix24CrmMaterialHandoff:
        async with self._uow("bitrix24_crm_material_revoke", revocation.claim_id) as uow:
            row = await self._load_claim(uow, revocation.claim_id)
            if row is None:
                raise ValueError("CRM material claim is unavailable in this tenant")
            claim = _claim_from_row(row)
            Bitrix24CrmMaterialHandoff(claim=claim, revocation=revocation)
            existing_result = await uow._execute(
                select(bitrix24_crm_material_revocations).where(
                    bitrix24_crm_material_revocations.c.revocation_id
                    == revocation.revocation_id
                )
            )
            existing_row = existing_result.mappings().one_or_none()
            if existing_row is not None:
                persisted = _revocation_from_row(existing_row)
                if persisted != revocation:
                    raise ValueError("CRM material revocation identity conflicts with persisted evidence")
                await uow.rollback()
                return Bitrix24CrmMaterialHandoff(claim=claim, revocation=persisted)
            await uow._execute(
                insert(bitrix24_crm_material_revocations)
                .values(
                    revocation_id=revocation.revocation_id,
                    claim_id=revocation.claim_id,
                    workspace_id=revocation.workspace_id,
                    environment_id=revocation.environment_id,
                    state=revocation.state.value,
                    record=revocation.model_dump(mode="json"),
                    observed_at=revocation.observed_at,
                )
                .on_conflict_do_nothing(
                    index_elements=[bitrix24_crm_material_revocations.c.revocation_id]
                )
            )
            await uow.commit()
            return Bitrix24CrmMaterialHandoff(claim=claim, revocation=revocation)

    async def resolve_handoff(self, claim_id: UUID) -> Bitrix24CrmMaterialHandoff | None:
        async with self._uow("bitrix24_crm_material_read", claim_id) as uow:
            row = await self._load_claim(uow, claim_id)
            if row is None:
                await uow.rollback()
                return None
            claim = _claim_from_row(row)
            revocation_result = await uow._execute(
                select(bitrix24_crm_material_revocations)
                .where(bitrix24_crm_material_revocations.c.claim_id == claim_id)
                .order_by(bitrix24_crm_material_revocations.c.observed_at.desc())
            )
            revocation_row = revocation_result.mappings().first()
            if revocation_row is not None:
                await uow.rollback()
                return Bitrix24CrmMaterialHandoff(
                    claim=claim, revocation=_revocation_from_row(revocation_row)
                )
            acceptance_result = await uow._execute(
                select(bitrix24_crm_material_acceptances).where(
                    bitrix24_crm_material_acceptances.c.claim_id == claim_id
                )
            )
            acceptance_row = acceptance_result.mappings().one_or_none()
            await uow.rollback()
            if acceptance_row is None:
                return None
            return Bitrix24CrmMaterialHandoff(
                claim=claim, acceptance=_acceptance_from_row(acceptance_row)
            )

    def _uow(self, operation: str, identity: UUID):
        return self._uow_factory(self._context_factory(PersistenceOperation(operation), identity))

    @staticmethod
    async def _load_claim(uow: Any, claim_id: UUID) -> Any | None:
        result = await uow._execute(
            select(bitrix24_crm_material_claims).where(
                bitrix24_crm_material_claims.c.claim_id == claim_id
            )
        )
        return result.mappings().one_or_none()

    @staticmethod
    async def _assert_target_scope(uow: Any, claim: Bitrix24CrmMaterialClaim) -> None:
        result = await uow._execute(
            select(source_objects.c.source_object_id).where(
                source_objects.c.source_object_id == claim.target_source_object_id,
                source_objects.c.workspace_id == claim.workspace_id,
                source_objects.c.environment_id == claim.environment_id,
            )
        )
        if result.first() is None:
            raise ValueError("CRM material target is outside the workspace scope")

    @classmethod
    async def _assert_target(cls, uow: Any, claim: Bitrix24CrmMaterialClaim) -> None:
        await cls._assert_target_scope(uow, claim)
        accepted = await uow._execute(
            select(canonical_acceptances.c.canonical_acceptance_id).where(
                canonical_acceptances.c.workspace_id == claim.workspace_id,
                canonical_acceptances.c.environment_id == claim.environment_id,
                canonical_acceptances.c.source_object_id == claim.target_source_object_id,
                canonical_acceptances.c.source_revision_id == claim.target_source_revision_id,
            )
        )
        if accepted.first() is None:
            raise ValueError("CRM material target revision is not canonically accepted")
        policy = await uow._execute(
            select(current_access_policies.c.access_policy_version_id).where(
                current_access_policies.c.workspace_id == claim.workspace_id,
                current_access_policies.c.environment_id == claim.environment_id,
                current_access_policies.c.source_object_id == claim.target_source_object_id,
            )
        )
        if policy.first() is None:
            raise ValueError("CRM material target is inaccessible under current policy")
        head = await uow._execute(
            select(current_source_heads.c.current_revision_id).where(
                current_source_heads.c.workspace_id == claim.workspace_id,
                current_source_heads.c.environment_id == claim.environment_id,
                current_source_heads.c.source_object_id == claim.target_source_object_id,
            )
        )
        head_row = head.first()
        if head_row is None or head_row[0] != claim.target_source_revision_id:
            raise ValueError("CRM material target revision is stale")
        revision = await uow._execute(
            select(source_revisions.c.revision_metadata).where(
                source_revisions.c.workspace_id == claim.workspace_id,
                source_revisions.c.environment_id == claim.environment_id,
                source_revisions.c.source_object_id == claim.target_source_object_id,
                source_revisions.c.revision_id == claim.target_source_revision_id,
            )
        )
        row = revision.mappings().one_or_none()
        metadata = None if row is None else row["revision_metadata"]
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        evidence = metadata.get("external_file_revision") if isinstance(metadata, dict) else None
        if not isinstance(evidence, dict):
            raise ValueError("CRM material target lacks Disk revision evidence")
        if (
            evidence.get("provider_kind") != "bitrix24"
            or evidence.get("provider_resource_id") != claim.disk_file_id
            or evidence.get("provider_revision") != claim.disk_file_revision
        ):
            raise ValueError("CRM material target Disk identity or revision does not match")


def _claim_from_row(row: Any) -> Bitrix24CrmMaterialClaim:
    return Bitrix24CrmMaterialClaim.model_validate_json(json.dumps(row["record"]))


def _acceptance_from_row(row: Any) -> Bitrix24CrmMaterialAcceptance:
    return Bitrix24CrmMaterialAcceptance.model_validate_json(json.dumps(row["record"]))


def _revocation_from_row(row: Any) -> Bitrix24CrmMaterialRevocation:
    return Bitrix24CrmMaterialRevocation.model_validate_json(json.dumps(row["record"]))


__all__ = ["PostgreSQLBitrix24CrmMaterialAssociationRepository"]
