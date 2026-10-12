"""Application service for immutable Access Policy activation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from typing import Protocol
from uuid import UUID

from pydantic import Field

from spine.domain.common import DefinitionModel
from spine.domain.audit import AuditEvent
from spine.application.persistence.outbox import OutboxIntent
from spine.application.persistence.command_digest import CommandDigest, digest_command
from spine.application.persistence.context import PersistenceOperation
from spine.application.persistence.idempotency import IdempotencyKey
from spine.domain.sources.access_policy import (
    AccessPolicyVersion,
    CurrentAccessPolicy,
    EligibilityDisposition,
    InitialPolicyEligibilityDecision,
    PolicyActivation,
    PolicyApproval,
    PolicyApprovalDecision,
    PolicyBenefitClassification,
    PolicyProposal,
)


class AccessPolicyError(Exception):
    """Disclosure-safe policy command failure."""


class InitialPolicyIneligibleError(AccessPolicyError):
    pass


class PolicyApprovalRequiredError(AccessPolicyError):
    pass


class StalePolicyAuthorityError(AccessPolicyError):
    pass


class StalePolicyBaselineError(AccessPolicyError):
    pass


class PolicyAuthoritySnapshot(DefinitionModel):
    canonical_human_id: UUID
    workspace_id: UUID
    environment_id: UUID
    membership_generation: int = Field(ge=1)
    authority_generation: int = Field(ge=1)
    service_authority_generation: int = Field(ge=1)
    is_policy_administrator: bool
    snapshot_reference: UUID


class PolicyAuthorityPort(Protocol):
    async def resolve(self, canonical_human_id: UUID) -> PolicyAuthoritySnapshot: ...

    async def classify_policy_change(
        self,
        requester: PolicyAuthoritySnapshot,
        current_policy_version_id: UUID,
        proposed_policy: AccessPolicyVersion,
    ) -> PolicyBenefitClassification: ...

    async def are_independent_humans(
        self,
        requester: PolicyAuthoritySnapshot,
        approver: PolicyAuthoritySnapshot,
    ) -> bool: ...


class PolicyFailedAttemptAudit(Protocol):
    async def record_failed_cas(
        self, command: "AccessPolicyActivationCommand", reason_code: str
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class AccessPolicyActivationCommand:
    policy: AccessPolicyVersion
    requester_canonical_human_id: UUID
    activator_canonical_human_id: UUID
    expected_policy_version_id: UUID | None
    expected_policy_generation: int
    expected_cas_version: int
    activation_id: UUID
    idempotency_key: IdempotencyKey
    audit_event_id: UUID
    outbox_event_id: UUID
    activated_at: datetime
    audit_event: AuditEvent | None = None
    outbox_intent: OutboxIntent | None = None
    proposal: PolicyProposal | None = None
    approval: PolicyApproval | None = None
    eligibility: InitialPolicyEligibilityDecision | None = None

    def expected_digest(self, operation: PersistenceOperation) -> CommandDigest:
        return digest_command(
            operation=operation,
            operation_schema_version=1,
            payload={
                "policy": self.policy.model_dump(mode="json"),
                "requester_canonical_human_id": self.requester_canonical_human_id,
                "activator_canonical_human_id": self.activator_canonical_human_id,
                "expected_policy_version_id": self.expected_policy_version_id,
                "expected_policy_generation": self.expected_policy_generation,
                "expected_cas_version": self.expected_cas_version,
                "activation_id": self.activation_id,
                "audit_event_id": self.audit_event_id,
                "outbox_event_id": self.outbox_event_id,
                "audit_event": (
                    self.audit_event.model_dump(
                        mode="json", exclude={"payload_snapshot"}
                    )
                    if self.audit_event
                    else None
                ),
                "outbox_intent": (
                    self.outbox_intent.model_dump(
                        mode="json", exclude={"payload_snapshot"}
                    )
                    if self.outbox_intent
                    else None
                ),
                "activated_at": self.activated_at,
                "proposal": (
                    self.proposal.model_dump(mode="json") if self.proposal else None
                ),
                "approval": (
                    self.approval.model_dump(mode="json") if self.approval else None
                ),
                "eligibility": (
                    self.eligibility.model_dump(mode="json")
                    if self.eligibility
                    else None
                ),
                "idempotency_key": self.idempotency_key.value,
            },
        )


@dataclass(frozen=True, slots=True)
class PolicyActivationDraft:
    values: dict[str, object]

    def finalize(self, receipt_id: UUID) -> PolicyActivation:
        return PolicyActivation.model_validate(
            {**self.values, "idempotency_receipt_id": receipt_id}
        )


@dataclass(frozen=True, slots=True)
class AccessPolicyActivationResult:
    activation: PolicyActivation
    current: CurrentAccessPolicy
    outbox_event_id: UUID
    replay: bool = False


class AccessPolicyRepository(Protocol):
    async def resolve_policy(
        self, policy_version_id: UUID
    ) -> AccessPolicyVersion | None: ...

    async def resolve_current(self, source_object_id: UUID) -> CurrentAccessPolicy | None: ...

    async def resolve_activation_result(
        self, command: AccessPolicyActivationCommand
    ) -> AccessPolicyActivationResult | None: ...

    async def activate(
        self,
        command: AccessPolicyActivationCommand,
        activation: PolicyActivationDraft,
    ) -> AccessPolicyActivationResult: ...

    async def persist_proposal(
        self, policy: AccessPolicyVersion, proposal: PolicyProposal
    ) -> PolicyProposal: ...

    async def resolve_proposal(self, proposal_id: UUID) -> PolicyProposal | None: ...

    async def persist_approval(self, approval: PolicyApproval) -> PolicyApproval: ...

def _same_scope(policy: AccessPolicyVersion, snapshot: PolicyAuthoritySnapshot) -> bool:
    return (
        policy.workspace_id == snapshot.workspace_id
        and policy.environment_id == snapshot.environment_id
        and snapshot.is_policy_administrator
    )


def _current_generations(
    snapshot: PolicyAuthoritySnapshot,
    *,
    membership: int,
    authority: int,
    service: int,
) -> bool:
    return (
        snapshot.membership_generation == membership
        and snapshot.authority_generation == authority
        and snapshot.service_authority_generation == service
    )


def access_policy_document_hash(policy: AccessPolicyVersion) -> str:
    """Hash the complete authority-bearing policy document."""

    grants = []
    for grant in policy.grants:
        values = grant.model_dump(mode="json", exclude={"access_grant_id"})
        grants.append(values)
    grants.sort(key=lambda value: json.dumps(value, sort_keys=True, separators=(",", ":")))
    payload = {
        "schema_version": policy.document_schema_version,
        "workspace_id": str(policy.workspace_id),
        "environment_id": str(policy.environment_id),
        "source_object_id": str(policy.source_object_id),
        "version": policy.version,
        "predecessor_policy_version_id": (
            str(policy.predecessor_policy_version_id)
            if policy.predecessor_policy_version_id is not None
            else None
        ),
        "origin_kind": policy.origin_kind.value,
        "origin_reference": str(policy.origin_reference),
        "origin_version": policy.origin_version,
        "origin_hash": policy.origin_hash,
        "grants": grants,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()


class AccessPolicyActivationService:
    """Revalidate authority and delegate one atomic CAS transition."""

    def __init__(
        self,
        repository: AccessPolicyRepository,
        authority: PolicyAuthorityPort,
        failed_attempt_audit: PolicyFailedAttemptAudit,
    ) -> None:
        self._repository = repository
        self._authority = authority
        self._failed_attempt_audit = failed_attempt_audit

    async def activate(
        self, command: AccessPolicyActivationCommand
    ) -> AccessPolicyActivationResult:
        try:
            return await self._activate(command)
        except StalePolicyBaselineError as error:
            try:
                await self._failed_attempt_audit.record_failed_cas(
                    command, "stale_policy_baseline"
                )
            except Exception:
                pass
            raise error

    async def _activate(
        self, command: AccessPolicyActivationCommand
    ) -> AccessPolicyActivationResult:
        policy = command.policy
        requester = await self._authority.resolve(
            command.requester_canonical_human_id
        )
        activator = await self._authority.resolve(
            command.activator_canonical_human_id
        )
        if not _same_scope(policy, requester) or not _same_scope(policy, activator):
            raise StalePolicyAuthorityError("Current policy authority is required.")

        replay = await self._repository.resolve_activation_result(
            command
        )
        if replay is not None:
            if (
                replay.activation.workspace_id != policy.workspace_id
                or replay.activation.environment_id != policy.environment_id
                or replay.activation.source_object_id != policy.source_object_id
                or replay.activation.new_policy_version_id
                != policy.access_policy_version_id
            ):
                raise StalePolicyBaselineError("Idempotency identity conflicts.")
            return replay

        if policy.document_hash != access_policy_document_hash(policy):
            raise StalePolicyAuthorityError("Policy document hash is invalid.")

        current = await self._repository.resolve_current(policy.source_object_id)
        if current is None:
            self._validate_initial(command, requester)
            next_generation = 1
        else:
            predecessor = await self._repository.resolve_policy(
                current.access_policy_version_id
            )
            if predecessor is None:
                raise StalePolicyBaselineError("The current policy baseline changed.")
            classification = await self._authority.classify_policy_change(
                requester,
                current.access_policy_version_id,
                policy,
            )
            independent = await self._authority.are_independent_humans(
                requester, activator
            )
            self._validate_successor(
                command,
                requester,
                activator,
                current,
                predecessor,
                classification,
                independent,
            )
            next_generation = current.policy_generation + 1

        evidence = command.eligibility
        proposal = command.proposal
        approval = command.approval
        activation = PolicyActivationDraft(
            values={
                "policy_activation_id": command.activation_id,
                "workspace_id": policy.workspace_id,
                "environment_id": policy.environment_id,
                "source_object_id": policy.source_object_id,
                "predecessor_policy_version_id": command.expected_policy_version_id,
                "new_policy_version_id": policy.access_policy_version_id,
                "policy_generation": next_generation,
                "policy_proposal_id": (
                    proposal.policy_proposal_id if proposal else None
                ),
                "policy_proposal_version": (
                    proposal.proposal_version if proposal else None
                ),
                "policy_proposal_hash": (
                    proposal.proposed_policy_hash if proposal else None
                ),
                "policy_approval_id": (
                    approval.policy_approval_id if approval else None
                ),
                "policy_approval_decision": (
                    approval.decision if approval else None
                ),
                "initial_eligibility_decision_id": (
                    evidence.decision_id if evidence else None
                ),
                "requester_canonical_human_id": (
                    command.requester_canonical_human_id
                ),
                "activator_canonical_human_id": (
                    command.activator_canonical_human_id
                ),
                "membership_generation": requester.membership_generation,
                "authority_generation": requester.authority_generation,
                "service_authority_generation": (
                    requester.service_authority_generation
                ),
                "audit_event_id": command.audit_event_id,
                "outbox_event_id": command.outbox_event_id,
                "activated_at": command.activated_at,
            }
        )
        return await self._repository.activate(command, activation)

    @staticmethod
    def _validate_initial(
        command: AccessPolicyActivationCommand,
        requester: PolicyAuthoritySnapshot,
    ) -> None:
        policy = command.policy
        decision = command.eligibility
        exact = decision is not None and (
            decision.disposition is EligibilityDisposition.ELIGIBLE
            and decision.workspace_id == policy.workspace_id
            and decision.environment_id == policy.environment_id
            and decision.source_object_id == policy.source_object_id
            and decision.policy_version_id == policy.access_policy_version_id
            and decision.policy_hash == policy.document_hash
            and decision.origin_kind is policy.origin_kind
            and decision.origin_reference == policy.origin_reference
            and decision.origin_version == policy.origin_version
            and decision.origin_hash == policy.origin_hash
            and decision.authority_snapshot_reference == requester.snapshot_reference
            and _current_generations(
                requester,
                membership=decision.membership_generation,
                authority=decision.authority_generation,
                service=decision.service_authority_generation,
            )
        )
        if (
            not exact
            or command.expected_policy_version_id is not None
            or command.expected_policy_generation != 0
            or command.expected_cas_version != 0
        ):
            raise InitialPolicyIneligibleError(
                "Initial policy eligibility is absent, stale, or mismatched."
            )

    @staticmethod
    def _validate_successor(
        command: AccessPolicyActivationCommand,
        requester: PolicyAuthoritySnapshot,
        activator: PolicyAuthoritySnapshot,
        current: CurrentAccessPolicy,
        predecessor: AccessPolicyVersion,
        classification: PolicyBenefitClassification,
        independent: bool,
    ) -> None:
        policy = command.policy
        proposal = command.proposal
        if (
            current.workspace_id != policy.workspace_id
            or current.environment_id != policy.environment_id
            or current.access_policy_version_id != command.expected_policy_version_id
            or current.policy_generation != command.expected_policy_generation
            or current.cas_version != command.expected_cas_version
            or policy.predecessor_policy_version_id
            != current.access_policy_version_id
            or policy.version != predecessor.version + 1
        ):
            raise StalePolicyBaselineError("The current policy baseline changed.")
        if proposal is None or (
            proposal.workspace_id != policy.workspace_id
            or proposal.environment_id != policy.environment_id
            or proposal.source_object_id != policy.source_object_id
            or proposal.proposed_policy_version_id != policy.access_policy_version_id
            or proposal.proposed_policy_hash != policy.document_hash
            or proposal.expected_policy_version_id != current.access_policy_version_id
            or proposal.expected_policy_generation != current.policy_generation
            or proposal.requester_canonical_human_id
            != command.requester_canonical_human_id
            or proposal.benefit_classification is not classification
            or not _current_generations(
                requester,
                membership=proposal.membership_generation,
                authority=proposal.authority_generation,
                service=proposal.service_authority_generation,
            )
        ):
            raise StalePolicyAuthorityError("Proposal evidence is stale or mismatched.")
        if classification is not PolicyBenefitClassification.SELF_BENEFITING:
            return
        approval = command.approval
        if approval is None or (
            approval.decision is not PolicyApprovalDecision.APPROVED
            or approval.policy_proposal_id != proposal.policy_proposal_id
            or approval.proposal_version != proposal.proposal_version
            or approval.proposal_hash != proposal.proposed_policy_hash
            or approval.approver_canonical_human_id
            != activator.canonical_human_id
            or approval.approver_canonical_human_id
            == requester.canonical_human_id
            or not independent
            or approval.authority_snapshot_reference != activator.snapshot_reference
            or approval.membership_generation != activator.membership_generation
            or approval.authority_generation != activator.authority_generation
        ):
            raise PolicyApprovalRequiredError(
                "An independent current canonical-human approval is required."
            )

    async def submit_proposal(
        self, policy: AccessPolicyVersion, proposal: PolicyProposal
    ) -> PolicyProposal:
        requester = await self._authority.resolve(
            proposal.requester_canonical_human_id
        )
        current = await self._repository.resolve_current(policy.source_object_id)
        if current is None or not _same_scope(policy, requester):
            raise StalePolicyBaselineError("The current policy baseline changed.")
        classification = await self._authority.classify_policy_change(
            requester, current.access_policy_version_id, policy
        )
        if (
            proposal.proposed_policy_version_id != policy.access_policy_version_id
            or proposal.proposed_policy_hash != policy.document_hash
            or proposal.expected_policy_version_id
            != current.access_policy_version_id
            or proposal.expected_policy_generation != current.policy_generation
            or proposal.benefit_classification is not classification
            or not _current_generations(
                requester,
                membership=proposal.membership_generation,
                authority=proposal.authority_generation,
                service=proposal.service_authority_generation,
            )
        ):
            raise StalePolicyAuthorityError("Proposal evidence is stale or mismatched.")
        return await self._repository.persist_proposal(policy, proposal)

    async def record_approval(self, approval: PolicyApproval) -> PolicyApproval:
        approver = await self._authority.resolve(
            approval.approver_canonical_human_id
        )
        proposal = await self._repository.resolve_proposal(
            approval.policy_proposal_id
        )
        if proposal is None or (
            approval.workspace_id != proposal.workspace_id
            or approval.environment_id != proposal.environment_id
            or approval.proposal_version != proposal.proposal_version
            or approval.proposal_hash != proposal.proposed_policy_hash
            or approver.workspace_id != proposal.workspace_id
            or approver.environment_id != proposal.environment_id
            or not approver.is_policy_administrator
            or approval.authority_snapshot_reference
            != approver.snapshot_reference
            or approval.membership_generation != approver.membership_generation
            or approval.authority_generation != approver.authority_generation
        ):
            raise StalePolicyAuthorityError("Approval evidence is stale or mismatched.")
        return await self._repository.persist_approval(approval)


__all__ = [
    "AccessPolicyActivationCommand",
    "AccessPolicyActivationResult",
    "AccessPolicyActivationService",
    "AccessPolicyError",
    "AccessPolicyRepository",
    "InitialPolicyIneligibleError",
    "PolicyApprovalRequiredError",
    "PolicyActivationDraft",
    "PolicyAuthorityPort",
    "PolicyAuthoritySnapshot",
    "PolicyFailedAttemptAudit",
    "StalePolicyAuthorityError",
    "StalePolicyBaselineError",
    "access_policy_document_hash",
]
