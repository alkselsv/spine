from uuid import UUID

import pytest

from spine.application.crm_relationship_v2_source_match import (
    CanonicalDiskSourceQuery,
    CrmFilesItemIdentity,
    DiskRevisionIdentity,
)
from spine.infrastructure.persistence.postgresql_crm_relationship_v2_source_match import (
    PostgreSQLCanonicalDiskSourceFinder,
)


class EmptyRows:
    def mappings(self):
        return self

    def all(self):
        return []


class TransactionFake:
    def __init__(self) -> None:
        self.sources = object()
        self.statements = []

    async def _execute(self, statement):
        self.statements.append(statement)
        return EmptyRows()


@pytest.mark.asyncio
async def test_postgresql_finder_uses_exact_tenant_connection_object_and_head() -> None:
    transaction = TransactionFake()
    query = CanonicalDiskSourceQuery(
        workspace_id=UUID(int=1),
        environment_id=UUID(int=2),
        connection_id=UUID(int=3),
        connection_version_id=UUID(int=4),
        relationship_files_item=CrmFilesItemIdentity(
            namespace="bitrix24-crm-files-item",
            value="files-42",
        ),
        mapping_profile_id=UUID(int=5),
        mapping_profile_version=2,
        disk_revision=DiskRevisionIdentity(
            namespace="bitrix24-disk-object",
            object_id="disk-42",
            provider_revision="17",
        ),
    )

    result = await PostgreSQLCanonicalDiskSourceFinder().find_current(
        query, transaction
    )

    assert result == ()
    statement = transaction.statements[0]
    parameters = statement.compile().params
    assert query.workspace_id in parameters.values()
    assert query.environment_id in parameters.values()
    assert query.connection_id in parameters.values()
    assert query.disk_revision.object_id in parameters.values()
    sql = str(statement)
    assert "current_source_heads" in sql
    assert "current_lifecycle_authorities" in sql
    assert "lifecycle_decisions" in sql
