"""Deterministic Access Policy repository used by contract tests and composition."""

from __future__ import annotations

import asyncio
from uuid import UUID

from spine.application.access_policies import (
    AccessPolicyActivationCommand,
    AccessPolicyActivationResult,
    PolicyActivationDraft,
    StalePolicyBaselineError,
)
from spine.domain.sources.access_policy import (
    AccessPolicyVersion,
    CurrentAccessPolicy,
    PolicyActivation,
    PolicyApproval,
    PolicyProposal,
)
from spine.application.persistence.context import PersistenceOperation
from spine.application.persistence.errors import IdempotencyConflictError


class InMemoryAccessPolicyRepository:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._policies: dict[UUID, AccessPolicyVersion] = {}
        self._activations: dict[UUID, PolicyActivation] = {}
        self._current: dict[UUID, CurrentAccessPolicy] = {}
        self._replays: dict[str, AccessPolicyActivationResult] = {}
        self._digests: dict[str, str] = {}
        self._proposals: dict[UUID, PolicyProposal] = {}
        self._approvals: dict[UUID, PolicyApproval] = {}
        self._audit_events: set[UUID] = set()
        self._outbox_events: set[UUID] = set()

    async def resolve_policy(
        self, policy_version_id: UUID
    ) -> AccessPolicyVersion | None:
        return self._policies.get(policy_version_id)

    async def resolve_current(self, source_object_id: UUID) -> CurrentAccessPolicy | None:
        return self._current.get(source_object_id)

    async def resolve_activation_result(
        self, command: AccessPolicyActivationCommand
    ) -> AccessPolicyActivationResult | None:
        key = command.idempotency_key.value
        digest = command.expected_digest(
            PersistenceOperation("access_policy_activation")
        ).value
        existing_digest = self._digests.get(key)
        if existing_digest is not None and existing_digest != digest:
            raise IdempotencyConflictError(
                "Idempotency key conflicts with existing command."
            )
        replay = self._replays.get(key)
        if replay is None:
            return None
        return AccessPolicyActivationResult(
            activation=replay.activation,
            current=replay.current,
            outbox_event_id=replay.outbox_event_id,
            replay=True,
        )

    async def persist_proposal(
        self, policy: AccessPolicyVersion, proposal: PolicyProposal
    ) -> PolicyProposal:
        async with self._lock:
            existing = self._proposals.get(proposal.policy_proposal_id)
            if existing is not None and existing != proposal:
                raise StalePolicyBaselineError("Proposal identity conflicts.")
            self._policies[policy.access_policy_version_id] = policy
            self._proposals[proposal.policy_proposal_id] = proposal
            return proposal

    async def resolve_proposal(self, proposal_id: UUID) -> PolicyProposal | None:
        return self._proposals.get(proposal_id)

    async def persist_approval(self, approval: PolicyApproval) -> PolicyApproval:
        async with self._lock:
            existing = self._approvals.get(approval.policy_approval_id)
            if existing is not None and existing != approval:
                raise StalePolicyBaselineError("Approval identity conflicts.")
            self._approvals[approval.policy_approval_id] = approval
            return approval

    async def activate(
        self,
        command: AccessPolicyActivationCommand,
        activation: PolicyActivationDraft,
    ) -> AccessPolicyActivationResult:
        async with self._lock:
            key = command.idempotency_key.value
            digest = command.expected_digest(
                PersistenceOperation("access_policy_activation")
            ).value
            existing_digest = self._digests.get(key)
            if existing_digest is not None and existing_digest != digest:
                raise IdempotencyConflictError(
                    "Idempotency key conflicts with existing command."
                )
            replay = self._replays.get(key)
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
            proposal = command.proposal
            if proposal is not None:
                existing_proposal = self._proposals.get(proposal.policy_proposal_id)
                if existing_proposal is not None and existing_proposal != proposal:
                    raise StalePolicyBaselineError("Proposal identity conflicts.")
            approval = command.approval
            if approval is not None:
                existing_approval = self._approvals.get(approval.policy_approval_id)
                if existing_approval is not None and existing_approval != approval:
                    raise StalePolicyBaselineError("Approval identity conflicts.")
            completed_activation = activation.finalize(command.activation_id)
            current = CurrentAccessPolicy(
                workspace_id=command.policy.workspace_id,
                environment_id=command.policy.environment_id,
                source_object_id=command.policy.source_object_id,
                access_policy_version_id=command.policy.access_policy_version_id,
                policy_activation_id=completed_activation.policy_activation_id,
                policy_generation=completed_activation.policy_generation,
                cas_version=cas_version,
            )
            self._policies[command.policy.access_policy_version_id] = command.policy
            if proposal is not None:
                self._proposals[proposal.policy_proposal_id] = proposal
            if approval is not None:
                self._approvals[approval.policy_approval_id] = approval
            self._activations[completed_activation.policy_activation_id] = (
                completed_activation
            )
            self._current[command.policy.source_object_id] = current
            self._audit_events.add(command.audit_event_id)
            self._outbox_events.add(command.outbox_event_id)
            result = AccessPolicyActivationResult(
                activation=completed_activation,
                current=current,
                outbox_event_id=command.outbox_event_id,
            )
            self._digests[key] = digest
            self._replays[key] = result
            return result

    async def activation_count(self) -> int:
        return len(self._activations)

    async def success_effect_count(self) -> int:
        return len(self._audit_events) + len(self._outbox_events)


__all__ = ["InMemoryAccessPolicyRepository"]
