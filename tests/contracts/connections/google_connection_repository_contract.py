from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import UUID

import pytest

from spine.application.connections.google import (
    GoogleConnectionCommand,
    GoogleConnectionConflictError,
    GoogleConnectionRepository,
)
from spine.application.persistence.context import TrustedPersistenceContext
from spine.domain.connections.google import GoogleConnection, GoogleConnectionStatus


@dataclass(frozen=True, slots=True)
class GoogleConnectionRepositoryScenario:
    repository: GoogleConnectionRepository
    context: TrustedPersistenceContext
    initial: GoogleConnection
    successor: GoogleConnection


ScenarioFactory = Callable[[], Awaitable[GoogleConnectionRepositoryScenario]]


async def assert_google_connection_repository_contract(
    factory: ScenarioFactory,
) -> None:
    scenario = await factory()
    first = GoogleConnectionCommand(
        connection=scenario.initial,
        idempotency_key="register-google-connection-v1",
        audit_event_id=UUID("00000000-0000-0000-0000-000000009901"),
    )

    created = await scenario.repository.register(first, scenario.context)
    replayed = await scenario.repository.register(first, scenario.context)

    assert created.connection == scenario.initial
    assert created.replay is False
    assert replayed.connection == scenario.initial
    assert replayed.replay is True
    assert replayed.audit == created.audit
    assert "vault" not in repr(created.audit)
    assert "grant" not in repr(created.audit)

    conflicting = GoogleConnectionCommand(
        connection=scenario.successor,
        idempotency_key=first.idempotency_key,
        audit_event_id=first.audit_event_id,
    )
    with pytest.raises(GoogleConnectionConflictError):
        await scenario.repository.register(conflicting, scenario.context)

    unfenced_update = scenario.successor.model_copy(
        update={
            "authorization_generation": scenario.initial.authorization_generation,
            "configuration_generation": scenario.initial.configuration_generation,
        }
    )
    with pytest.raises(GoogleConnectionConflictError):
        await scenario.repository.register(
            GoogleConnectionCommand(
                connection=unfenced_update,
                idempotency_key="unfenced-google-connection-v2",
                audit_event_id=UUID("00000000-0000-0000-0000-000000009903"),
            ),
            scenario.context,
        )

    updated = await scenario.repository.register(
        GoogleConnectionCommand(
            connection=scenario.successor,
            idempotency_key="update-google-connection-v2",
            audit_event_id=UUID("00000000-0000-0000-0000-000000009902"),
        ),
        scenario.context,
    )

    assert updated.connection == scenario.successor
    assert updated.connection.status is GoogleConnectionStatus.ACTIVE
    assert updated.audit.authorization_generation == 2
    assert updated.audit.configuration_generation == 2


__all__ = [
    "GoogleConnectionRepositoryScenario",
    "ScenarioFactory",
    "assert_google_connection_repository_contract",
]
