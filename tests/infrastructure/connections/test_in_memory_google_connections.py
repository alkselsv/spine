from __future__ import annotations

from uuid import UUID

import pytest

from spine.application.connections.google import (
    GoogleConnectionCommand,
    GoogleConnectionConflictError,
)
from spine.application.persistence import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
)
from spine.domain.connections.google import (
    GoogleConnection,
    GoogleConnectionApproval,
    GoogleConnectionStatus,
    OpaqueGoogleReference,
)
from spine.infrastructure.connections.google import InMemoryGoogleConnectionRepository
from spine.infrastructure.persistence.contexts import TrustedContextBoundary
from tests.contracts.connections.google_connection_repository_contract import (
    OCCURRED_AT,
    GoogleConnectionRepositoryScenario,
    assert_google_connection_generation_fencing_contract,
    assert_google_connection_idempotency_conflict_contract,
    assert_google_connection_registration_replay_contract,
    assert_google_connection_successor_contract,
)


def _id(value: int) -> UUID:
    return UUID(f"00000000-0000-0000-0000-{value:012d}")


def _connection(*, version: int, predecessor: UUID | None = None) -> GoogleConnection:
    return GoogleConnection(
        connection_version_id=_id(100 + version),
        connection_id=_id(10),
        approval_id=_id(200 + version),
        version=version,
        predecessor_version_id=predecessor,
        workspace_id=_id(20),
        environment_id=_id(21),
        service_principal_id=_id(22),
        credential_reference=OpaqueGoogleReference(value=_id(300 + version)),
        grant_reference=OpaqueGoogleReference(value=_id(400 + version)),
        approved_resource_scope_references=(
            OpaqueGoogleReference(value=_id(500)),
        ),
        authorization_generation=version,
        configuration_generation=version,
        status=GoogleConnectionStatus.ACTIVE,
    )


def _approval(connection: GoogleConnection) -> GoogleConnectionApproval:
    return GoogleConnectionApproval(
        approval_id=connection.approval_id,
        workspace_id=connection.workspace_id,
        environment_id=connection.environment_id,
        service_principal_id=connection.service_principal_id,
        credential_reference=connection.credential_reference,
        grant_reference=connection.grant_reference,
        approved_resource_scope_references=connection.approved_resource_scope_references,
        authorization_generation=connection.authorization_generation,
        configuration_generation=connection.configuration_generation,
        status=connection.status,
    )


async def _scenario() -> GoogleConnectionRepositoryScenario:
    boundary = TrustedContextBoundary.for_testing(
        issuer_id=_id(30), secret=b"issue-599-repository-contract-secret"
    )
    initial = _connection(version=1)
    successor = _connection(version=2, predecessor=initial.connection_version_id)
    return GoogleConnectionRepositoryScenario(
        repository=InMemoryGoogleConnectionRepository(
            context_verifier=boundary,
            approvals=(_approval(initial), _approval(successor)),
        ),
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
        successor=successor,
    )


@pytest.mark.asyncio
async def test_in_memory_registration_replay_is_stable() -> None:
    await assert_google_connection_registration_replay_contract(_scenario)


@pytest.mark.asyncio
async def test_in_memory_idempotency_key_reuse_with_other_payload_conflicts() -> None:
    await assert_google_connection_idempotency_conflict_contract(_scenario)


@pytest.mark.asyncio
async def test_in_memory_unfenced_successor_conflicts() -> None:
    await assert_google_connection_generation_fencing_contract(_scenario)


@pytest.mark.asyncio
async def test_in_memory_fenced_successor_becomes_current() -> None:
    await assert_google_connection_successor_contract(_scenario)


@pytest.mark.asyncio
async def test_in_memory_unapproved_connection_conflicts() -> None:
    scenario = await _scenario()
    unapproved = scenario.initial.model_copy(update={"approval_id": _id(999)})

    with pytest.raises(GoogleConnectionConflictError):
        await scenario.repository.register(
            GoogleConnectionCommand(
                connection=unapproved,
                idempotency_key="unapproved-google-connection",
                audit_event_id=_id(998),
                occurred_at=OCCURRED_AT,
            ),
            scenario.context,
        )
