"""Migration-owner adapter for explicit qualification fixture provisioning."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Table, select, text
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.exc import IntegrityError

from spine.auth import QualificationTargetBinding, qualification_target_binding_digest
from spine.auth.records import AuthorizationDirectoryRecords, EnvironmentRoleBinding
from spine.domain.context_profiles import ContextProfileVersion
from spine.infrastructure.db.engine import SessionFactory
from spine.infrastructure.persistence.postgresql_authorization_mappings import (
    authentication_alias_bindings,
    canonical_human_identities,
    canonical_human_identity_states,
    environment_memberships,
    environment_role_bindings,
    qualification_authority_bindings,
    qualification_target_bindings,
    workspace_memberships,
)
from spine.infrastructure.persistence.postgresql_mappings import (
    access_policy_grants,
    access_policy_versions,
    context_profiles,
    environments,
    source_objects,
    workspaces,
)

_VERIFY_MIGRATION_OWNER = text(
    "SELECT current_user = pg_get_userbyid(relation.relowner) "
    "FROM pg_class AS relation "
    "JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace "
    "WHERE namespace.nspname = 'spine' "
    "AND relation.relname = 'canonical_human_identities'"
)


class QualificationFixtureConflictError(RuntimeError):
    """An immutable fixture identity already exists with different content."""


@dataclass(frozen=True)
class QualificationWorkspaceFixture:
    workspace_id: UUID
    slug: str
    display_name: str


@dataclass(frozen=True)
class QualificationEnvironmentFixture:
    environment_id: UUID
    workspace_id: UUID
    display_name: str


@dataclass(frozen=True)
class QualificationSourceAccessFixture:
    source_object_id: UUID
    policy_id: UUID
    grant_id: UUID
    workspace_id: UUID
    environment_id: UUID
    principal_id: UUID
    origin_reference: UUID
    created_at: datetime
    principal_kind: str = "human"
    permission: str = "read_content"


@dataclass(frozen=True)
class QualificationFixturePlan:
    workspaces: tuple[QualificationWorkspaceFixture, ...]
    environments: tuple[QualificationEnvironmentFixture, ...]
    context_profiles: tuple[ContextProfileVersion, ...]
    authorization_records: AuthorizationDirectoryRecords
    source_access: QualificationSourceAccessFixture
    target_binding: QualificationTargetBinding


class PostgreSQLQualificationFixtureProvisioner:
    """Append and verify one exact, immutable qualification fixture plan."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def provision(self, plan: QualificationFixturePlan) -> None:
        if type(plan) is not QualificationFixturePlan:
            raise TypeError("qualification fixture must use the canonical plan")
        session = self._session_factory()
        try:
            async with session.begin():
                await self._require_migration_owner(session)
                await self._append_scopes(session, plan)
                await self._append_authorization(session, plan.authorization_records)
                await self._append_source_access(session, plan.source_access)
                await self._append_target_binding(session, plan.target_binding)
        except asyncio.CancelledError:
            raise
        finally:
            await session.close()

    async def advance_generation(self, record: EnvironmentRoleBinding) -> None:
        """Append one deterministic authority version for a stale-generation test."""

        session = self._session_factory()
        try:
            async with session.begin():
                await self._require_migration_owner(session)
                await _append_exact(
                    session,
                    environment_role_bindings,
                    _role_binding_values(record),
                    ("binding_id",),
                )
        except asyncio.CancelledError:
            raise
        finally:
            await session.close()

    async def _require_migration_owner(self, session: Any) -> None:
        if await session.scalar(_VERIFY_MIGRATION_OWNER) is not True:
            raise RuntimeError(
                "qualification fixture provisioning requires the migration owner"
            )

    async def _append_scopes(
        self, session: Any, plan: QualificationFixturePlan
    ) -> None:
        for fixture in plan.workspaces:
            await _append_exact(
                session,
                workspaces,
                {
                    "id": fixture.workspace_id,
                    "slug": fixture.slug,
                    "display_name": fixture.display_name,
                },
                ("id",),
            )
        for fixture in plan.environments:
            await _append_exact(
                session,
                environments,
                {
                    "id": fixture.environment_id,
                    "workspace_id": fixture.workspace_id,
                    "kind": "production",
                    "display_name": fixture.display_name,
                },
                ("id",),
            )
        for profile in plan.context_profiles:
            values = profile.model_dump(mode="python")
            values.update(
                retrieval_strategy=profile.retrieval_strategy.value,
                completeness_requirement=profile.completeness_requirement.value,
                partial_answer_fallback=profile.partial_answer_fallback.value,
            )
            await _append_exact(
                session,
                context_profiles,
                values,
                ("workspace_id", "environment_id", "profile_id", "profile_version"),
            )

    async def _append_authorization(
        self, session: Any, records: AuthorizationDirectoryRecords
    ) -> None:
        for record in records.identities:
            await _append_exact(
                session,
                canonical_human_identities,
                {
                    "identity_id": record.identity_id,
                    "created_change_id": record.provenance.change_id,
                    "created_by": record.provenance.recorded_by,
                },
                ("identity_id",),
            )
            await _append_exact(
                session,
                canonical_human_identity_states,
                {
                    "identity_id": record.identity_id,
                    "version": record.version,
                    "status": record.status.value,
                    "change_id": record.provenance.change_id,
                    "recorded_by": record.provenance.recorded_by,
                },
                ("identity_id", "version"),
            )
        for record in records.alias_bindings:
            await _append_exact(
                session,
                authentication_alias_bindings,
                {
                    "binding_id": record.binding_id,
                    "issuer": record.alias.issuer,
                    "subject": record.alias.subject,
                    "canonical_human_identity_id": record.canonical_human_identity_id,
                    "version": record.version,
                    "status": record.status.value,
                    "change_id": record.provenance.change_id,
                    "recorded_by": record.provenance.recorded_by,
                },
                ("binding_id",),
            )
        for record in records.workspace_memberships:
            await _append_exact(
                session,
                workspace_memberships,
                {
                    "membership_id": record.membership_id,
                    "workspace_id": record.workspace_id,
                    "canonical_human_identity_id": record.canonical_human_identity_id,
                    "version": record.version,
                    "status": record.status.value,
                    "change_id": record.provenance.change_id,
                    "recorded_by": record.provenance.recorded_by,
                },
                ("membership_id",),
            )
        for record in records.environment_memberships:
            await _append_exact(
                session,
                environment_memberships,
                {
                    "membership_id": record.membership_id,
                    "workspace_id": record.workspace_id,
                    "environment_id": record.environment_id,
                    "canonical_human_identity_id": record.canonical_human_identity_id,
                    "version": record.version,
                    "status": record.status.value,
                    "change_id": record.provenance.change_id,
                    "recorded_by": record.provenance.recorded_by,
                },
                ("membership_id",),
            )
        for record in records.role_bindings:
            await _append_exact(
                session,
                environment_role_bindings,
                _role_binding_values(record),
                ("binding_id",),
            )
        for record in records.qualification_bindings:
            await _append_exact(
                session,
                qualification_authority_bindings,
                {
                    "binding_id": record.binding_id,
                    "version": record.version,
                    "qualification_id": record.qualification_id,
                    "workspace_id": record.workspace_id,
                    "environment_id": record.environment_id,
                    "principal_kind": record.principal_kind.value,
                    "principal_id": record.principal_id,
                    "purpose": record.purpose,
                    "operation": record.operation,
                    "expires_at": record.expires_at,
                    "status": record.status.value,
                    "change_id": record.provenance.change_id,
                    "recorded_by": record.provenance.recorded_by,
                },
                ("binding_id", "version"),
            )

    async def _append_source_access(
        self, session: Any, fixture: QualificationSourceAccessFixture
    ) -> None:
        await _append_exact(
            session,
            source_objects,
            {
                "source_object_id": fixture.source_object_id,
                "workspace_id": fixture.workspace_id,
                "environment_id": fixture.environment_id,
                "source_kind": "qualification",
                "identity_mode": "upload",
                "upload_identity": "qualification-source-authority-fixture",
                "created_at": fixture.created_at,
            },
            ("source_object_id",),
        )
        await _append_exact(
            session,
            access_policy_versions,
            {
                "access_policy_version_id": fixture.policy_id,
                "workspace_id": fixture.workspace_id,
                "environment_id": fixture.environment_id,
                "source_object_id": fixture.source_object_id,
                "version": 1,
                "predecessor_policy_version_id": None,
                "document_schema_version": "access-policy.v1",
                "document_hash": "a" * 64,
                "origin_kind": "approved_source_configuration",
                "origin_reference": fixture.origin_reference,
                "origin_version": 1,
                "origin_hash": "b" * 64,
                "created_by_canonical_human_id": fixture.principal_id,
                "created_at": fixture.created_at,
            },
            ("access_policy_version_id",),
        )
        await _append_exact(
            session,
            access_policy_grants,
            {
                "access_grant_id": fixture.grant_id,
                "workspace_id": fixture.workspace_id,
                "environment_id": fixture.environment_id,
                "source_object_id": fixture.source_object_id,
                "access_policy_version_id": fixture.policy_id,
                "principal_kind": fixture.principal_kind,
                "principal_id": fixture.principal_id,
                "permission": fixture.permission,
                "purpose": None,
                "operation": None,
                "valid_from": None,
                "valid_until": None,
            },
            ("access_grant_id",),
        )

    async def _append_target_binding(
        self, session: Any, binding: QualificationTargetBinding
    ) -> None:
        await _append_exact(
            session,
            qualification_target_bindings,
            {
                "binding_id": binding.binding_id,
                "binding_series_id": binding.binding_series_id,
                "binding_version": binding.binding_version,
                "predecessor_binding_id": binding.predecessor_binding_id,
                "authority_binding_id": binding.authority_binding_id,
                "authority_binding_version": binding.authority_binding_version,
                "qualification_id": binding.qualification_id,
                "workspace_id": binding.workspace_id,
                "environment_id": binding.environment_id,
                "target": binding.target.value,
                "target_reference": binding.target_reference,
                "target_scope_reference": binding.target_scope_reference,
                "principal_kind": binding.principal_kind.value,
                "principal_id": binding.principal_id,
                "purpose": binding.purpose,
                "operation": binding.operation,
                "alias_reference": binding.alias_reference,
                "secret_provider_reference": binding.secret.provider_reference,
                "secret_reference": binding.secret.secret_reference,
                "database_role_reference": binding.database_role_reference,
                "target_role": binding.target_role.value,
                "database_access": binding.database_access.value,
                "access_policy_references": list(binding.access_policy_references),
                "binding_digest": qualification_target_binding_digest(binding),
            },
            ("binding_id",),
        )


def _role_binding_values(record: EnvironmentRoleBinding) -> dict[str, Any]:
    return {
        "binding_id": record.binding_id,
        "workspace_id": record.workspace_id,
        "environment_id": record.environment_id,
        "canonical_human_identity_id": record.canonical_human_identity_id,
        "role": record.role.value,
        "version": record.version,
        "status": record.status.value,
        "change_id": record.provenance.change_id,
        "recorded_by": record.provenance.recorded_by,
    }


async def _append_exact(
    session: Any,
    table: Table,
    values: dict[str, Any],
    identity_columns: tuple[str, ...],
) -> None:
    identity = [table.c[name] == values[name] for name in identity_columns]
    lock_key = ":".join(
        (table.name, *(f"{name}={values[name]}" for name in identity_columns))
    )
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
        {"lock_key": lock_key},
    )
    row = (
        (await session.execute(select(table).where(*identity))).mappings().one_or_none()
    )
    if row is None:
        try:
            await session.execute(
                postgresql_insert(table).values(**values).on_conflict_do_nothing()
            )
        except IntegrityError:
            raise QualificationFixtureConflictError(
                f"qualification fixture conflicts with immutable {table.name} record"
            ) from None
        row = (
            (await session.execute(select(table).where(*identity)))
            .mappings()
            .one_or_none()
        )
    if row is None or any(
        _comparable(row[name]) != _comparable(value) for name, value in values.items()
    ):
        raise QualificationFixtureConflictError(
            f"qualification fixture conflicts with immutable {table.name} record"
        )


def _comparable(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        return tuple(value)
    return value


__all__ = [
    "PostgreSQLQualificationFixtureProvisioner",
    "QualificationEnvironmentFixture",
    "QualificationFixtureConflictError",
    "QualificationFixturePlan",
    "QualificationSourceAccessFixture",
    "QualificationWorkspaceFixture",
]
