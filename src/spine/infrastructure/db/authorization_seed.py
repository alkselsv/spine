"""Migration-owner authorization seed helper for deterministic tests only.

This module is deliberately not exported from a runtime composition root. It
does not represent a provisioning API and must be used with migration-owner
credentials against disposable test databases.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import insert, text, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert

from spine.auth.records import AuthorizationDirectoryRecords
from spine.infrastructure.db.engine import SessionFactory
from spine.infrastructure.persistence.postgresql_authorization_mappings import (
    authentication_alias_bindings,
    authorization_generations,
    canonical_human_identities,
    canonical_human_identity_states,
    environment_memberships,
    environment_role_bindings,
    workspace_memberships,
)


_VERIFY_MIGRATION_OWNER = text(
    "SELECT current_user = pg_get_userbyid(relation.relowner) "
    "FROM pg_class AS relation "
    "JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace "
    "WHERE namespace.nspname = 'spine' "
    "AND relation.relname = 'canonical_human_identities'"
)


class PostgreSQLAuthorizationSeedHelper:
    """Append immutable test records under the migration-owner role."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def append(self, records: AuthorizationDirectoryRecords) -> None:
        if type(records) is not AuthorizationDirectoryRecords:
            raise TypeError("authorization records must use the canonical contract")
        session = self._session_factory()
        try:
            async with session.begin():
                is_owner = await session.scalar(_VERIFY_MIGRATION_OWNER)
                if is_owner is not True:
                    raise RuntimeError(
                        "authorization test seeding requires the migration owner"
                    )

                for record in sorted(
                    records.identities,
                    key=lambda value: (value.identity_id.int, value.version),
                ):
                    await session.execute(
                        postgresql_insert(canonical_human_identities)
                        .values(
                            identity_id=record.identity_id,
                            created_change_id=record.provenance.change_id,
                            created_by=record.provenance.recorded_by,
                        )
                        .on_conflict_do_nothing(
                            index_elements=[canonical_human_identities.c.identity_id]
                        )
                    )
                    await session.execute(
                        insert(canonical_human_identity_states).values(
                            identity_id=record.identity_id,
                            version=record.version,
                            status=record.status.value,
                            change_id=record.provenance.change_id,
                            recorded_by=record.provenance.recorded_by,
                        )
                    )

                for record in sorted(
                    records.alias_bindings,
                    key=lambda value: (
                        value.alias.issuer,
                        value.alias.subject,
                        value.version,
                    ),
                ):
                    await session.execute(
                        insert(authentication_alias_bindings).values(
                            binding_id=record.binding_id,
                            issuer=record.alias.issuer,
                            subject=record.alias.subject,
                            canonical_human_identity_id=(
                                record.canonical_human_identity_id
                            ),
                            version=record.version,
                            status=record.status.value,
                            change_id=record.provenance.change_id,
                            recorded_by=record.provenance.recorded_by,
                        )
                    )

                for record in sorted(
                    records.workspace_memberships,
                    key=lambda value: (
                        value.workspace_id.int,
                        value.canonical_human_identity_id.int,
                        value.version,
                    ),
                ):
                    await session.execute(
                        insert(workspace_memberships).values(
                            membership_id=record.membership_id,
                            workspace_id=record.workspace_id,
                            canonical_human_identity_id=(
                                record.canonical_human_identity_id
                            ),
                            version=record.version,
                            status=record.status.value,
                            change_id=record.provenance.change_id,
                            recorded_by=record.provenance.recorded_by,
                        )
                    )

                for record in sorted(
                    records.environment_memberships,
                    key=lambda value: (
                        value.workspace_id.int,
                        value.environment_id.int,
                        value.canonical_human_identity_id.int,
                        value.version,
                    ),
                ):
                    await session.execute(
                        insert(environment_memberships).values(
                            membership_id=record.membership_id,
                            workspace_id=record.workspace_id,
                            environment_id=record.environment_id,
                            canonical_human_identity_id=(
                                record.canonical_human_identity_id
                            ),
                            version=record.version,
                            status=record.status.value,
                            change_id=record.provenance.change_id,
                            recorded_by=record.provenance.recorded_by,
                        )
                    )

                for record in sorted(
                    records.role_bindings,
                    key=lambda value: (
                        value.workspace_id.int,
                        value.environment_id.int,
                        value.canonical_human_identity_id.int,
                        value.role.value,
                        value.version,
                    ),
                ):
                    await session.execute(
                        insert(environment_role_bindings).values(
                            binding_id=record.binding_id,
                            workspace_id=record.workspace_id,
                            environment_id=record.environment_id,
                            canonical_human_identity_id=(
                                record.canonical_human_identity_id
                            ),
                            role=record.role.value,
                            version=record.version,
                            status=record.status.value,
                            change_id=record.provenance.change_id,
                            recorded_by=record.provenance.recorded_by,
                        )
                    )

                for record in records.generations:
                    await session.execute(
                        update(authorization_generations)
                        .where(
                            authorization_generations.c.workspace_id
                            == record.workspace_id,
                            authorization_generations.c.environment_id
                            == record.environment_id,
                            authorization_generations.c.generation
                            <= record.generation,
                        )
                        .values(generation=record.generation)
                    )
        except asyncio.CancelledError:
            raise
        finally:
            await session.close()


__all__ = ["PostgreSQLAuthorizationSeedHelper"]
