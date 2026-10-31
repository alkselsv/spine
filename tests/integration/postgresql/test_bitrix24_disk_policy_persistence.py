from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from spine.domain.connections.bitrix24_disk_scope import (
    Bitrix24DiskContentPolicy,
    Bitrix24DiskRootPolicy,
    Bitrix24DiskStorageClass,
)
from spine.domain.connections.provider import OpaqueProviderReference
from spine.infrastructure.persistence.postgresql_bitrix24_disk_policies import (
    Bitrix24DiskPolicyConflict,
    PostgreSQLBitrix24DiskPolicyRepository,
)
from tests.integration.postgresql.test_bitrix24_oauth import (
    NOW,
    _connection,
    _id,
    _seed_approval,
    oauth_database,
)

pytestmark = [pytest.mark.postgresql, pytest.mark.asyncio(loop_scope="session")]


class _Uow:
    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory

    async def __aenter__(self):
        self.session = self._session_factory()
        await self.session.begin()
        return self

    async def __aexit__(self, *_args):
        await self.session.rollback()
        await self.session.close()

    async def _execute(self, statement):
        return await self.session.execute(statement)

    async def commit(self):
        await self.session.commit()

    async def rollback(self):
        await self.session.rollback()


def _content_policy(connection, *, version: str = "v1") -> Bitrix24DiskContentPolicy:
    return Bitrix24DiskContentPolicy(
        policy_id=_id(41),
        version=version,
        workspace_id=connection.workspace_id,
        environment_id=connection.environment_id,
        connection_id=connection.connection_id,
        connection_version_id=connection.connection_version_id,
        resource_scope_reference=OpaqueProviderReference(value=_id(9)),
        allowed_formats=("pdf",),
    )


def _root_policy(connection, content, *, version: str = "v1") -> Bitrix24DiskRootPolicy:
    return Bitrix24DiskRootPolicy(
        policy_id=_id(42),
        version=version,
        workspace_id=connection.workspace_id,
        environment_id=connection.environment_id,
        connection_id=connection.connection_id,
        connection_version_id=connection.connection_version_id,
        resource_scope_reference=OpaqueProviderReference(value=_id(9)),
        storage_id="storage-1",
        storage_class=Bitrix24DiskStorageClass.COMMON,
        storage_root_object_id="storage-root",
        approved_root_folder_id="approved-root",
        recurse=False,
        future_discovery=False,
        manifest_object_ids=("file-1",),
        content_policy_id=content.policy_id,
        content_policy_version=content.version,
        content_policy_digest=content.digest,
        traversal_profile_reference=OpaqueProviderReference(value=_id(43)),
    )


async def _seed_connection(oauth_database, connection) -> None:
    await _seed_approval(oauth_database.migration_engine, connection)
    async with oauth_database.migration_engine.begin() as db:
        await db.execute(
            text(
                "INSERT INTO spine.provider_connection_versions ("
                "connection_version_id, connection_id, approval_id, version, "
                "predecessor_version_id, workspace_id, environment_id, service_principal_id, "
                "provider_kind, credential_reference, grant_reference, approved_resource_scope_references, "
                "authorization_generation, configuration_generation, status, capabilities, health, "
                "diagnostic_codes, observed_at, cursor_lag_seconds, last_successful_reconciliation_at, "
                "receipt_id, idempotency_key, command_digest, recorded_at) VALUES ("
                ":connection_version_id, :connection_id, :approval_id, 1, NULL, :workspace_id, "
                ":environment_id, :service_principal_id, 'bitrix24', :credential_reference, "
                ":grant_reference, ARRAY[:scope_reference]::uuid[], :authorization_generation, "
                ":configuration_generation, 'active', '[]'::jsonb, 'healthy', ARRAY[]::varchar[], "
                ":now, NULL, NULL, :receipt_id, 'disk-policy-test', :digest, :now)"
            ),
            {
                "connection_version_id": connection.connection_version_id,
                "connection_id": connection.connection_id,
                "approval_id": connection.approval_id,
                "workspace_id": connection.workspace_id,
                "environment_id": connection.environment_id,
                "service_principal_id": connection.service_principal_id,
                "credential_reference": connection.credential_reference.value,
                "grant_reference": connection.grant_reference.value,
                "scope_reference": _id(9),
                "authorization_generation": connection.authorization_generation,
                "configuration_generation": connection.configuration_generation,
                "now": NOW,
                "receipt_id": _id(44),
                "digest": "c" * 64,
            },
        )


@pytest.mark.asyncio
async def test_postgresql_policy_heads_are_tenant_scoped_immutable_and_cas_fenced(oauth_database) -> None:
    connection = _connection()
    await _seed_connection(oauth_database, connection)
    sessions = async_sessionmaker(oauth_database.migration_engine, expire_on_commit=False)
    repository = PostgreSQLBitrix24DiskPolicyRepository(
        uow_factory=lambda _context: _Uow(sessions),
        context_factory=lambda operation, workspace_id, environment_id: (
            operation,
            workspace_id,
            environment_id,
        ),
    )
    content = _content_policy(connection)
    root = _root_policy(connection, content)
    await repository.publish_content_policy(
        content,
        expected_version=None,
        expected_digest=None,
        recorded_at=NOW,
    )
    await repository.publish_root_policy(
        root,
        expected_version=None,
        expected_digest=None,
        recorded_at=NOW,
    )

    assert await repository.get_current_content_policy(
        workspace_id=connection.workspace_id,
        environment_id=connection.environment_id,
        policy_id=content.policy_id,
    ) == content
    assert await repository.get_current_root_policy(
        workspace_id=connection.workspace_id,
        environment_id=connection.environment_id,
        policy_id=root.policy_id,
    ) == root
    assert await repository.get_current_root_policy(
        workspace_id=_id(90),
        environment_id=connection.environment_id,
        policy_id=root.policy_id,
    ) is None

    async with oauth_database.runtime_engine.begin() as runtime:
        await runtime.execute(
            text(
                "SELECT set_config('spine.workspace_id', :workspace_id, true), "
                "set_config('spine.environment_id', :environment_id, true)"
            ),
            {
                "workspace_id": str(connection.workspace_id),
                "environment_id": str(connection.environment_id),
            },
        )
        visible = await runtime.execute(
            text(
                "SELECT policy_id FROM spine.current_bitrix24_disk_root_policies "
                "WHERE workspace_id = :workspace_id AND environment_id = :environment_id"
            ),
            {
                "workspace_id": connection.workspace_id,
                "environment_id": connection.environment_id,
            },
        )
        hidden = await runtime.execute(
            text(
                "SELECT policy_id FROM spine.current_bitrix24_disk_root_policies "
                "WHERE workspace_id = :workspace_id AND environment_id = :environment_id"
            ),
            {"workspace_id": _id(90), "environment_id": connection.environment_id},
        )
        assert visible.scalar_one() == root.policy_id
        assert hidden.scalar_one_or_none() is None

    successor_a = _root_policy(connection, content, version="v2")
    successor_b = _root_policy(connection, content, version="v3")
    outcomes = await asyncio.gather(
        repository.publish_root_policy(
            successor_a,
            expected_version=root.version,
            expected_digest=root.digest,
            recorded_at=NOW,
        ),
        repository.publish_root_policy(
            successor_b,
            expected_version=root.version,
            expected_digest=root.digest,
            recorded_at=NOW,
        ),
        return_exceptions=True,
    )
    assert sum(outcome is None for outcome in outcomes) == 1
    assert sum(isinstance(outcome, Bitrix24DiskPolicyConflict) for outcome in outcomes) == 1

    with pytest.raises(Bitrix24DiskPolicyConflict):
        await repository.publish_root_policy(
            _root_policy(connection, content, version="v4"),
            expected_version=root.version,
            expected_digest=root.digest,
            recorded_at=NOW,
        )
