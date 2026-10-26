from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

import pytest

from spine.application.connections.google import (
    GoogleConnectionCommand,
    GoogleConnectionConflictError,
    GoogleConnectionRepository,
)
from spine.application.persistence.context import TrustedPersistenceContext
from spine.domain.connections.google import GoogleConnection, GoogleConnectionStatus


OCCURRED_AT = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)


@dataclass(frozen=True, slots=True)
class GoogleConnectionRepositoryScenario:
    repository: GoogleConnectionRepository
    context: TrustedPersistenceContext
    initial: GoogleConnection
    successor: GoogleConnection


ScenarioFactory = Callable[[], Awaitable[GoogleConnectionRepositoryScenario]]


def _command(
    connection: GoogleConnection,
    *,
    idempotency_key: str,
    audit_event_id: UUID,
) -> GoogleConnectionCommand:
    return GoogleConnectionCommand(
        connection=connection,
        idempotency_key=idempotency_key,
        audit_event_id=audit_event_id,
        occurred_at=OCCURRED_AT,
    )


async def assert_google_connection_registration_replay_contract(
    factory: ScenarioFactory,
) -> None:
    scenario = await factory()
    command = _command(
        scenario.initial,
        idempotency_key="register-google-connection-v1",
        audit_event_id=UUID("00000000-0000-0000-0000-000000009901"),
    )

    created = await scenario.repository.register(command, scenario.context)
    replayed = await scenario.repository.register(command, scenario.context)

    assert created.connection == scenario.initial
    assert created.replay is False
    assert replayed.connection == scenario.initial
    assert replayed.replay is True
    assert replayed.audit == created.audit
    assert created.audit.event_type == "command.outcome"
    assert created.audit.target is not None
    assert created.audit.target.object_id == scenario.initial.connection_version_id
    assert created.audit.payload_json() == {
        "command_type": "google_connection_registration"
    }
    assert "vault" not in repr(created.audit)
    assert "grant" not in repr(created.audit)


async def assert_google_connection_idempotency_conflict_contract(
    factory: ScenarioFactory,
) -> None:
    scenario = await factory()
    first = _command(
        scenario.initial,
        idempotency_key="conflicting-google-connection-command",
        audit_event_id=UUID("00000000-0000-0000-0000-000000009911"),
    )
    await scenario.repository.register(first, scenario.context)
    conflicting = _command(
        scenario.successor,
        idempotency_key=first.idempotency_key,
        audit_event_id=first.audit_event_id,
    )

    with pytest.raises(GoogleConnectionConflictError):
        await scenario.repository.register(conflicting, scenario.context)


async def assert_google_connection_generation_fencing_contract(
    factory: ScenarioFactory,
) -> None:
    scenario = await factory()
    await scenario.repository.register(
        _command(
            scenario.initial,
            idempotency_key="generation-fence-initial-v1",
            audit_event_id=UUID("00000000-0000-0000-0000-000000009921"),
        ),
        scenario.context,
    )
    unfenced_update = scenario.successor.model_copy(
        update={
            "authorization_generation": scenario.initial.authorization_generation,
            "configuration_generation": scenario.initial.configuration_generation,
        }
    )

    with pytest.raises(GoogleConnectionConflictError):
        await scenario.repository.register(
            _command(
                unfenced_update,
                idempotency_key="generation-fence-invalid-v2",
                audit_event_id=UUID("00000000-0000-0000-0000-000000009922"),
            ),
            scenario.context,
        )


async def assert_google_connection_successor_contract(
    factory: ScenarioFactory,
) -> None:
    scenario = await factory()
    await scenario.repository.register(
        _command(
            scenario.initial,
            idempotency_key="successor-initial-v1",
            audit_event_id=UUID("00000000-0000-0000-0000-000000009931"),
        ),
        scenario.context,
    )
    updated = await scenario.repository.register(
        _command(
            scenario.successor,
            idempotency_key="successor-update-v2",
            audit_event_id=UUID("00000000-0000-0000-0000-000000009932"),
        ),
        scenario.context,
    )

    assert updated.connection == scenario.successor
    assert updated.connection.status is GoogleConnectionStatus.ACTIVE
    assert updated.audit.target is not None
    assert updated.audit.target.object_id == scenario.successor.connection_version_id


__all__ = [
    "GoogleConnectionRepositoryScenario",
    "OCCURRED_AT",
    "ScenarioFactory",
    "assert_google_connection_generation_fencing_contract",
    "assert_google_connection_idempotency_conflict_contract",
    "assert_google_connection_registration_replay_contract",
    "assert_google_connection_successor_contract",
]
