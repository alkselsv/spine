from __future__ import annotations

from uuid import UUID

import pytest

from spine.application.persistence import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
)
from spine.domain.connections.google import (
    GoogleConnection,
    GoogleConnectionStatus,
    OpaqueGoogleReference,
)
from spine.infrastructure.connections.google import InMemoryGoogleConnectionRepository
from spine.infrastructure.persistence.contexts import TrustedContextBoundary
from tests.contracts.connections.google_connection_repository_contract import (
    GoogleConnectionRepositoryScenario,
    assert_google_connection_repository_contract,
)


def _id(value: int) -> UUID:
    return UUID(f"00000000-0000-0000-0000-{value:012d}")


def _connection(*, version: int, predecessor: UUID | None = None) -> GoogleConnection:
    return GoogleConnection(
        connection_version_id=_id(100 + version),
        connection_id=_id(10),
        version=version,
        predecessor_version_id=predecessor,
        workspace_id=_id(20),
        environment_id=_id(21),
        service_principal_id=_id(22),
        credential_reference=OpaqueGoogleReference(value=f"vault:google-r2-v{version}"),
        grant_reference=OpaqueGoogleReference(value=f"grant:google-r2-v{version}"),
        approved_resource_scope_references=(
            OpaqueGoogleReference(value="scope:estimate-register-v1"),
        ),
        authorization_generation=version,
        configuration_generation=version,
        status=GoogleConnectionStatus.ACTIVE,
    )


@pytest.mark.asyncio
async def test_in_memory_google_connection_repository_contract() -> None:
    async def scenario() -> GoogleConnectionRepositoryScenario:
        boundary = TrustedContextBoundary.for_testing(
            issuer_id=_id(30), secret=b"issue-599-repository-contract-secret"
        )
        initial = _connection(version=1)
        return GoogleConnectionRepositoryScenario(
            repository=InMemoryGoogleConnectionRepository(context_verifier=boundary),
            context=boundary.worker(
                scope=EnvironmentScope(
                    workspace_id=initial.workspace_id,
                    environment_id=initial.environment_id,
                ),
                service_principal_id=initial.service_principal_id,
                purpose=PersistencePurpose("google_connector"),
                operation=PersistenceOperation("google_connection_register"),
                trace_id=_id(31),
            ),
            initial=initial,
            successor=_connection(
                version=2, predecessor=initial.connection_version_id
            ),
        )

    await assert_google_connection_repository_contract(scenario)
