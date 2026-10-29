from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import pytest

from spine.application.connections.management import (
    ProviderConnectionConfigurationRequest,
    ProviderConnectionConfigurationResult,
    ProviderConnectionManager,
)
from spine.application.persistence import (
    IdempotencyConflictError,
    OptimisticConflictError,
)
from spine.application.persistence.context import TrustedPersistenceContext


@dataclass(frozen=True, slots=True)
class ProviderConnectionManagementScenario:
    manager: ProviderConnectionManager
    context: TrustedPersistenceContext
    initial: ProviderConnectionConfigurationRequest
    conflicting_replay: ProviderConnectionConfigurationRequest
    stalled_successor: ProviderConnectionConfigurationRequest
    provider_call_count: Callable[[], int]
    fail_next_provider_call: Callable[[], None]


async def assert_provider_connection_management_contract(
    scenario: ProviderConnectionManagementScenario,
) -> ProviderConnectionConfigurationResult:
    scenario.fail_next_provider_call()
    with pytest.raises(RuntimeError, match="synthetic provider interruption"):
        await scenario.manager.configure(scenario.initial, scenario.context)
    created = await scenario.manager.configure(scenario.initial, scenario.context)
    replay = await scenario.manager.configure(scenario.initial, scenario.context)

    assert replay.replay is True
    assert replay.receipt_id == created.receipt_id
    assert replay.audit == created.audit
    assert created.audit.event_type == "command.outcome"
    assert created.audit.payload_json() == {
        "command_type": "provider_connection_configuration"
    }
    assert scenario.provider_call_count() == 2

    with pytest.raises(IdempotencyConflictError):
        await scenario.manager.configure(scenario.conflicting_replay, scenario.context)
    with pytest.raises(OptimisticConflictError):
        await scenario.manager.configure(scenario.stalled_successor, scenario.context)
    assert scenario.provider_call_count() == 2
    return created


__all__ = [
    "ProviderConnectionManagementScenario",
    "assert_provider_connection_management_contract",
]
