"""Tenant-scoped PostgreSQL persistence for register link handoffs."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from spine.application.persistence.context import (
    PersistenceOperation,
    TrustedPersistenceContext,
)
from spine.application.register_associations import (
    validate_register_acceptance_authority,
)
from spine.domain.sources import (
    RegisterAssociationAcceptance,
    RegisterAssociationHandoff,
    RegisterObservedLinkClaim,
)
from spine.infrastructure.persistence.postgresql_mappings import (
    canonical_acceptances,
    register_association_acceptances,
    register_link_claims,
    source_objects,
    source_revisions,
    source_revision_provenance,
)
from spine.domain.sources.google import google_source_identity


class PostgreSQLRegisterAssociationRepository:
    """Store claims and acceptances only within a verified tenant UoW."""

    def __init__(
        self,
        uow_factory: Callable[[TrustedPersistenceContext], object],
        context_factory: Callable[
            [PersistenceOperation, UUID], TrustedPersistenceContext
        ],
    ) -> None:
        self._uow_factory = uow_factory
        self._context_factory = context_factory

    async def record_claim(
        self, claim: RegisterObservedLinkClaim
    ) -> RegisterObservedLinkClaim:
        async with self._uow("register_link_claim", claim.claim_id) as uow:
            row = await self._load_claim(uow, claim.claim_id)
            if row is not None:
                existing = _claim_from_row(row)
                if existing != claim:
                    raise ValueError(
                        "register link claim identity conflicts with persisted evidence"
                    )
                await uow.rollback()
                return existing
            if not await self._is_accepted_revision(
                uow,
                claim.workspace_id,
                claim.environment_id,
                claim.row_source_object_id,
                claim.row_source_revision_id,
            ):
                raise ValueError(
                    "register row revision is not canonically accepted in this scope"
                )
            revision_result = await uow._execute(
                select(
                    source_revisions.c.revision_digest,
                    source_revisions.c.original_sha256,
                ).where(
                    source_revisions.c.workspace_id == claim.workspace_id,
                    source_revisions.c.environment_id == claim.environment_id,
                    source_revisions.c.source_object_id == claim.row_source_object_id,
                    source_revisions.c.revision_id == claim.row_source_revision_id,
                )
            )
            revision_row = revision_result.mappings().one_or_none()
            if (
                revision_row is None
                or revision_row["revision_digest"] != claim.row_revision_digest
                or revision_row["original_sha256"] != claim.row_original_sha256
            ):
                raise ValueError(
                    "claim row content does not match its accepted source revision"
                )
            provenance_result = await uow._execute(
                select(source_revision_provenance.c.provenance_id).where(
                    source_revision_provenance.c.workspace_id == claim.workspace_id,
                    source_revision_provenance.c.environment_id == claim.environment_id,
                    source_revision_provenance.c.source_object_id
                    == claim.row_source_object_id,
                    source_revision_provenance.c.revision_id
                    == claim.row_source_revision_id,
                    source_revision_provenance.c.provenance_id == claim.provenance_id,
                )
            )
            if provenance_result.first() is None:
                raise ValueError(
                    "claim provenance does not belong to its exact row revision"
                )
            row_identity = google_source_identity(claim.row_provider_identity)
            source_result = await uow._execute(
                select(source_objects.c.source_object_id).where(
                    source_objects.c.source_object_id == claim.row_source_object_id,
                    source_objects.c.workspace_id == claim.workspace_id,
                    source_objects.c.environment_id == claim.environment_id,
                    source_objects.c.connection_id
                    == claim.row_provider_identity.connection_id,
                    source_objects.c.external_namespace
                    == row_identity.external_namespace,
                    source_objects.c.external_generation
                    == row_identity.external_generation,
                    source_objects.c.external_object_id
                    == row_identity.external_object_id,
                )
            )
            if source_result.first() is None:
                raise ValueError(
                    "claim provider identity does not match the stored register row"
                )
            await uow._execute(
                insert(register_link_claims)
                .values(
                    claim_id=claim.claim_id,
                    workspace_id=claim.workspace_id,
                    environment_id=claim.environment_id,
                    row_source_object_id=claim.row_source_object_id,
                    row_source_revision_id=claim.row_source_revision_id,
                    provenance_id=claim.provenance_id,
                    role=claim.role.value,
                    record=claim.model_dump(mode="json"),
                    observed_at=claim.observed_at,
                )
                .on_conflict_do_nothing(
                    index_elements=[register_link_claims.c.claim_id]
                )
            )
            persisted_row = await self._load_claim(uow, claim.claim_id)
            if persisted_row is None or _claim_from_row(persisted_row) != claim:
                raise ValueError(
                    "register link claim identity conflicts with persisted evidence"
                )
            await uow.commit()
            return _claim_from_row(persisted_row)

    async def accept_claim(
        self,
        *,
        claim_id: UUID,
        acceptance: RegisterAssociationAcceptance,
    ) -> RegisterAssociationHandoff:
        async with self._uow("register_association_accept", claim_id) as uow:
            claim_row = await self._load_claim(uow, claim_id)
            if claim_row is None:
                raise ValueError("register link claim is unavailable in this tenant")
            claim = _claim_from_row(claim_row)
            validate_register_acceptance_authority(uow._active_context, acceptance)
            RegisterAssociationHandoff(claim=claim, acceptance=acceptance)
            existing_result = await uow._execute(
                select(register_association_acceptances).where(
                    register_association_acceptances.c.claim_id == claim_id
                )
            )
            existing = existing_result.mappings().one_or_none()
            if existing is not None:
                persisted = _acceptance_from_row(existing)
                if persisted != acceptance:
                    raise ValueError(
                        "register claim already has a different accepted association"
                    )
                await uow.rollback()
                return RegisterAssociationHandoff(claim=claim, acceptance=persisted)
            if not await self._is_accepted_revision(
                uow,
                claim.workspace_id,
                claim.environment_id,
                acceptance.target_source_object_id,
                acceptance.target_source_revision_id,
            ):
                raise ValueError(
                    "target revision is not canonically accepted in this tenant"
                )
            target_identity = acceptance.target_provider_identity
            expected_identity = google_source_identity(target_identity)
            target_result = await uow._execute(
                select(source_objects.c.source_object_id).where(
                    source_objects.c.source_object_id
                    == acceptance.target_source_object_id,
                    source_objects.c.workspace_id == acceptance.workspace_id,
                    source_objects.c.environment_id == acceptance.environment_id,
                    source_objects.c.connection_id == target_identity.connection_id,
                    source_objects.c.external_namespace
                    == expected_identity.external_namespace,
                    source_objects.c.external_generation
                    == expected_identity.external_generation,
                    source_objects.c.external_object_id
                    == expected_identity.external_object_id,
                )
            )
            if target_result.first() is None:
                raise ValueError(
                    "target source revision does not match the validated provider identity"
                )
            await uow._execute(
                insert(register_association_acceptances)
                .values(
                    acceptance_id=acceptance.acceptance_id,
                    claim_id=claim_id,
                    workspace_id=acceptance.workspace_id,
                    environment_id=acceptance.environment_id,
                    row_source_object_id=acceptance.row_source_object_id,
                    row_source_revision_id=acceptance.row_source_revision_id,
                    target_source_object_id=acceptance.target_source_object_id,
                    target_source_revision_id=acceptance.target_source_revision_id,
                    role=acceptance.role.value,
                    record=acceptance.model_dump(mode="json"),
                    accepted_at=acceptance.accepted_at,
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        register_association_acceptances.c.workspace_id,
                        register_association_acceptances.c.environment_id,
                        register_association_acceptances.c.claim_id,
                    ]
                )
            )
            persisted_result = await uow._execute(
                select(register_association_acceptances).where(
                    register_association_acceptances.c.claim_id == claim_id
                )
            )
            persisted_row = persisted_result.mappings().one_or_none()
            if (
                persisted_row is None
                or _acceptance_from_row(persisted_row) != acceptance
            ):
                raise ValueError(
                    "register claim already has a different accepted association"
                )
            await uow.commit()
            return RegisterAssociationHandoff(
                claim=claim, acceptance=_acceptance_from_row(persisted_row)
            )

    async def resolve_handoff(
        self, claim_id: UUID
    ) -> RegisterAssociationHandoff | None:
        async with self._uow("register_association_read", claim_id) as uow:
            claim_row = await self._load_claim(uow, claim_id)
            if claim_row is None:
                await uow.rollback()
                return None
            claim = _claim_from_row(claim_row)
            result = await uow._execute(
                select(register_association_acceptances).where(
                    register_association_acceptances.c.claim_id == claim_id
                )
            )
            row = result.mappings().one_or_none()
            await uow.rollback()
            if row is None:
                return None
            return RegisterAssociationHandoff(
                claim=claim, acceptance=_acceptance_from_row(row)
            )

    def _uow(self, operation: str, identity: UUID):
        context = self._context_factory(PersistenceOperation(operation), identity)
        return self._uow_factory(context)

    @staticmethod
    async def _load_claim(uow: Any, claim_id: UUID) -> Any | None:
        result = await uow._execute(
            select(register_link_claims).where(
                register_link_claims.c.claim_id == claim_id
            )
        )
        return result.mappings().one_or_none()

    @staticmethod
    async def _is_accepted_revision(
        uow: Any,
        workspace_id: UUID,
        environment_id: UUID,
        source_object_id: UUID,
        source_revision_id: UUID,
    ) -> bool:
        result = await uow._execute(
            select(canonical_acceptances.c.canonical_acceptance_id).where(
                canonical_acceptances.c.workspace_id == workspace_id,
                canonical_acceptances.c.environment_id == environment_id,
                canonical_acceptances.c.source_object_id == source_object_id,
                canonical_acceptances.c.source_revision_id == source_revision_id,
            )
        )
        return result.first() is not None


def _claim_from_row(row: Any) -> RegisterObservedLinkClaim:
    return RegisterObservedLinkClaim.model_validate_json(json.dumps(row["record"]))


def _acceptance_from_row(row: Any) -> RegisterAssociationAcceptance:
    return RegisterAssociationAcceptance.model_validate_json(json.dumps(row["record"]))


__all__ = ["PostgreSQLRegisterAssociationRepository"]
