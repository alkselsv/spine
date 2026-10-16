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
    SourceSecurityDomainBinding,
)
from spine.application.persistence.context import PersistenceOperation
from spine.application.persistence.errors import IdempotencyConflictError


class InMemoryAccessPolicyRepository:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._policies: dict[tuple[UUID, UUID, UUID], AccessPolicyVersion] = {}
        self._activations: dict[UUID, PolicyActivation] = {}
        self._current: dict[tuple[UUID, UUID, UUID], CurrentAccessPolicy] = {}
        self._replays: dict[str, AccessPolicyActivationResult] = {}
        self._digests: dict[str, str] = {}
        self._proposals: dict[UUID, PolicyProposal] = {}
        self._approvals: dict[UUID, PolicyApproval] = {}
        self._security_domain_bindings: dict[
            tuple[UUID, UUID, UUID], SourceSecurityDomainBinding
        ] = {}
        self._audit_events: set[UUID] = set()
        self._outbox_events: set[UUID] = set()

    async def resolve_policy(
        self,
        policy_version_id: UUID,
        *,
        workspace_id: UUID | None = None,
        environment_id: UUID | None = None,
    ) -> AccessPolicyVersion | None:
        if workspace_id is not None and environment_id is not None:
            return self._policies.get((workspace_id, environment_id, policy_version_id))
        matches = [
            value for value in self._policies.values()
            if value.access_policy_version_id == policy_version_id
        ]
        return matches[0] if len(matches) == 1 else None

    async def resolve_current(
        self,
        source_object_id: UUID,
        *,
        workspace_id: UUID | None = None,
        environment_id: UUID | None = None,
    ) -> CurrentAccessPolicy | None:
        if workspace_id is not None and environment_id is not None:
            return self._current.get((workspace_id, environment_id, source_object_id))
        matches = [
            value for value in self._current.values()
            if value.source_object_id == source_object_id
        ]
        return matches[0] if len(matches) == 1 else None

    async def resolve_security_domain_binding(
        self,
        binding_id: UUID,
        *,
        workspace_id: UUID | None = None,
        environment_id: UUID | None = None,
    ) -> SourceSecurityDomainBinding | None:
        if workspace_id is not None and environment_id is not None:
            return self._security_domain_bindings.get(
                (workspace_id, environment_id, binding_id)
            )
        matches = [
            value
            for value in self._security_domain_bindings.values()
            if value.binding_id == binding_id
        ]
        return matches[0] if len(matches) == 1 else None

    async def persist_security_domain_binding(
        self, binding: SourceSecurityDomainBinding
    ) -> SourceSecurityDomainBinding:
        async with self._lock:
            key = (binding.workspace_id, binding.environment_id, binding.binding_id)
            existing = self._security_domain_bindings.get(key)
            if existing is not None and existing != binding:
                raise StalePolicyBaselineError("Security Domain binding identity conflicts.")
            for candidate in self._security_domain_bindings.values():
                if (
                    candidate.workspace_id == binding.workspace_id
                    and candidate.environment_id == binding.environment_id
                    and candidate.source_object_id == binding.source_object_id
                    and candidate.binding_version == binding.binding_version
                    and candidate.binding_id != binding.binding_id
                ):
                    raise StalePolicyBaselineError("Security Domain binding version conflicts.")
            self._security_domain_bindings[key] = binding
            return binding

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
            self._policies[
                policy.workspace_id,
                policy.environment_id,
                policy.access_policy_version_id,
            ] = policy
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
            scope_key = (
                command.policy.workspace_id,
                command.policy.environment_id,
                command.policy.source_object_id,
            )
            existing = self._current.get(scope_key)
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
            self._policies[
                command.policy.workspace_id,
                command.policy.environment_id,
                command.policy.access_policy_version_id,
            ] = command.policy
            if proposal is not None:
                self._proposals[proposal.policy_proposal_id] = proposal
            if approval is not None:
                self._approvals[approval.policy_approval_id] = approval
            self._activations[completed_activation.policy_activation_id] = (
                completed_activation
            )
            self._current[scope_key] = current
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
