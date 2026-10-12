"""Deterministic Access Policy repository used by contract tests and composition."""

from __future__ import annotations

import asyncio
from uuid import UUID

from spine.application.access_policies import (
    AccessPolicyActivationCommand,
    AccessPolicyActivationResult,
    StalePolicyBaselineError,
)
from spine.domain.sources.access_policy import (
    AccessPolicyVersion,
    CurrentAccessPolicy,
    PolicyActivation,
)


class InMemoryAccessPolicyRepository:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._policies: dict[UUID, AccessPolicyVersion] = {}
        self._activations: dict[UUID, PolicyActivation] = {}
        self._current: dict[UUID, CurrentAccessPolicy] = {}
        self._replays: dict[UUID, AccessPolicyActivationResult] = {}
        self._audit_events: set[UUID] = set()
        self._outbox_events: set[UUID] = set()

    async def resolve_current(self, source_object_id: UUID) -> CurrentAccessPolicy | None:
        return self._current.get(source_object_id)

    async def activate(
        self,
        command: AccessPolicyActivationCommand,
        activation: PolicyActivation,
    ) -> AccessPolicyActivationResult:
        async with self._lock:
            replay = self._replays.get(command.idempotency_receipt_id)
            if replay is not None:
                if (
                    replay.activation.new_policy_version_id
                    != command.policy.access_policy_version_id
                ):
                    raise StalePolicyBaselineError("Idempotency identity conflicts.")
                return AccessPolicyActivationResult(
                    activation=replay.activation,
                    current=replay.current,
                    outbox_event_id=replay.outbox_event_id,
                    replay=True,
                )
            existing = self._current.get(command.policy.source_object_id)
            if existing is None:
                valid = (
                    command.expected_policy_version_id is None
                    and command.expected_policy_generation == 0
                    and command.expected_cas_version == 0
                )
                cas_version = 1
            else:
                valid = (
                    existing.access_policy_version_id
                    == command.expected_policy_version_id
                    and existing.policy_generation
                    == command.expected_policy_generation
                    and existing.cas_version == command.expected_cas_version
                )
                cas_version = existing.cas_version + 1
            if not valid:
                raise StalePolicyBaselineError("The current policy baseline changed.")
            current = CurrentAccessPolicy(
                workspace_id=command.policy.workspace_id,
                environment_id=command.policy.environment_id,
                source_object_id=command.policy.source_object_id,
                access_policy_version_id=command.policy.access_policy_version_id,
                policy_activation_id=activation.policy_activation_id,
                policy_generation=activation.policy_generation,
                cas_version=cas_version,
            )
            self._policies[command.policy.access_policy_version_id] = command.policy
            self._activations[activation.policy_activation_id] = activation
            self._current[command.policy.source_object_id] = current
            self._audit_events.add(command.audit_event_id)
            self._outbox_events.add(command.outbox_event_id)
            result = AccessPolicyActivationResult(
                activation=activation,
                current=current,
                outbox_event_id=command.outbox_event_id,
            )
            self._replays[command.idempotency_receipt_id] = result
            return result

    async def activation_count(self) -> int:
        return len(self._activations)

    async def success_effect_count(self) -> int:
        return len(self._audit_events) + len(self._outbox_events)


__all__ = ["InMemoryAccessPolicyRepository"]
