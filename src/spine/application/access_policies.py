"""Application service for immutable Access Policy activation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from typing import Protocol
from uuid import UUID

from pydantic import Field, model_validator

from spine.domain.common import DefinitionModel
from spine.domain.audit import AuditEvent
from spine.application.persistence.outbox import OutboxIntent
from spine.domain.sources.access_policy import (
    AccessPolicyVersion,
    AccessPermission,
    CurrentAccessPolicy,
    EligibilityDisposition,
    InitialPolicyEligibilityDecision,
    PolicyActivation,
    PolicyApproval,
    PolicyApprovalDecision,
    PolicyBenefitClassification,
    PolicyProposal,
    PrincipalKind,
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
    idempotency_receipt_id: UUID
    audit_event_id: UUID
    outbox_event_id: UUID
    activated_at: datetime
    audit_event: AuditEvent | None = None
    outbox_intent: OutboxIntent | None = None
    proposal: PolicyProposal | None = None
    approval: PolicyApproval | None = None
    eligibility: InitialPolicyEligibilityDecision | None = None


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
        self, idempotency_receipt_id: UUID
    ) -> AccessPolicyActivationResult | None: ...

    async def activate(
        self,
        command: AccessPolicyActivationCommand,
        activation: PolicyActivation,
    ) -> AccessPolicyActivationResult: ...

    async def persist_proposal(
        self, policy: AccessPolicyVersion, proposal: PolicyProposal
    ) -> PolicyProposal: ...

    async def resolve_proposal(self, proposal_id: UUID) -> PolicyProposal | None: ...

    async def persist_approval(self, approval: PolicyApproval) -> PolicyApproval: ...

    async def resolve_effective_policy(
        self, source_object_id: UUID
    ) -> tuple[CurrentAccessPolicy, AccessPolicyVersion] | None: ...


class ContentAuthorizationContext(DefinitionModel):
    """Trusted Issue #5 authority snapshot used for one content decision."""

    workspace_id: UUID
    environment_id: UUID
    canonical_human_id: UUID | None = None
    effective_team_ids: frozenset[UUID] = frozenset()
    membership_generation: int = Field(ge=1)
    service_principal_id: UUID | None = None
    service_authority_generation: int = Field(ge=1)
    purpose: str | None = None
    operation: str | None = None
    server_owned_operation: bool = False
    authority_snapshot_reference: UUID

    @model_validator(mode="after")
    def validate_service_context(self) -> "ContentAuthorizationContext":
        service_values = (
            self.service_principal_id,
            self.purpose,
            self.operation,
        )
        if any(value is not None for value in service_values) and not all(
            value is not None for value in service_values
        ):
            raise ValueError("service identity, purpose, and operation are inseparable")
        return self


class ContentAuthorizationDecision(DefinitionModel):
    """Disclosure-safe allow/deny decision over an exact source set."""

    allowed: bool
    source_object_ids: tuple[UUID, ...]
    policy_version_ids: dict[UUID, UUID]
    policy_generations: dict[UUID, int]
    membership_generation: int = Field(ge=1)
    service_authority_generation: int = Field(ge=1)
    authority_snapshot_reference: UUID
    purpose: str | None = None
    operation: str | None = None
    reason_code: str
    evaluated_at: datetime


class ContentAuthorizationService:
    """Evaluate current allow-only policies without granting from admin authority."""

    def __init__(self, repository: AccessPolicyRepository) -> None:
        self._repository = repository

    async def decide_human_disclosure(
        self,
        context: ContentAuthorizationContext,
        source_object_ids: tuple[UUID, ...],
        clock: datetime,
    ) -> ContentAuthorizationDecision:
        return await self._decide(context, source_object_ids, clock, "human")

    async def decide_interactive_processing(
        self,
        context: ContentAuthorizationContext,
        source_object_ids: tuple[UUID, ...],
        clock: datetime,
    ) -> ContentAuthorizationDecision:
        return await self._decide(context, source_object_ids, clock, "interactive")

    async def decide_noninteractive_processing(
        self,
        context: ContentAuthorizationContext,
        source_object_ids: tuple[UUID, ...],
        clock: datetime,
    ) -> ContentAuthorizationDecision:
        return await self._decide(context, source_object_ids, clock, "noninteractive")

    async def _decide(
        self,
        context: ContentAuthorizationContext,
        source_object_ids: tuple[UUID, ...],
        clock: datetime,
        mode: str,
    ) -> ContentAuthorizationDecision:
        if clock.tzinfo is None or clock.utcoffset() is None:
            raise ValueError("decision clock must be timezone-aware")
        sources = tuple(dict.fromkeys(source_object_ids))
        allowed = bool(sources)
        policy_ids: dict[UUID, UUID] = {}
        generations: dict[UUID, int] = {}
        for source_id in sources:
            resolved = await self._repository.resolve_effective_policy(source_id)
            if resolved is None:
                allowed = False
                break
            current, policy = resolved
            if (
                current.workspace_id != context.workspace_id
                or current.environment_id != context.environment_id
                or policy.workspace_id != context.workspace_id
                or policy.environment_id != context.environment_id
                or current.access_policy_version_id
                != policy.access_policy_version_id
            ):
                allowed = False
                break
            human_allowed = self._human_allowed(policy, context)
            service_allowed = self._service_allowed(policy, context, clock)
            if mode == "human":
                source_allowed = context.canonical_human_id is not None and human_allowed
            elif mode == "interactive":
                source_allowed = (
                    context.canonical_human_id is not None
                    and context.service_principal_id is not None
                    and human_allowed
                    and service_allowed
                )
            else:
                source_allowed = (
                    context.canonical_human_id is None
                    and context.service_principal_id is not None
                    and context.server_owned_operation
                    and service_allowed
                )
            if not source_allowed:
                allowed = False
                break
            policy_ids[source_id] = policy.access_policy_version_id
            generations[source_id] = current.policy_generation
        if not allowed:
            policy_ids = {}
            generations = {}
        return ContentAuthorizationDecision(
            allowed=allowed,
            source_object_ids=sources,
            policy_version_ids=policy_ids,
            policy_generations=generations,
            membership_generation=context.membership_generation,
            service_authority_generation=context.service_authority_generation,
            authority_snapshot_reference=context.authority_snapshot_reference,
            purpose=context.purpose,
            operation=context.operation,
            reason_code=("content_authority_allowed" if allowed else "content_authority_denied"),
            evaluated_at=clock,
        )

    @staticmethod
    def _human_allowed(
        policy: AccessPolicyVersion, context: ContentAuthorizationContext
    ) -> bool:
        principals = set(context.effective_team_ids)
        if context.canonical_human_id is not None:
            principals.add(context.canonical_human_id)
        return any(
            grant.permission is AccessPermission.READ_CONTENT
            and grant.principal_id in principals
            and (
                grant.principal_kind is PrincipalKind.TEAM
                or (
                    grant.principal_kind is PrincipalKind.HUMAN
                    and grant.principal_id == context.canonical_human_id
                )
            )
            for grant in policy.grants
        )

    @staticmethod
    def _service_allowed(
        policy: AccessPolicyVersion,
        context: ContentAuthorizationContext,
        clock: datetime,
    ) -> bool:
        return any(
            grant.principal_kind is PrincipalKind.SERVICE
            and grant.permission is AccessPermission.PROCESS_CONTENT
            and grant.principal_id == context.service_principal_id
            and grant.purpose == context.purpose
            and grant.operation == context.operation
            and (grant.valid_from is None or grant.valid_from <= clock)
            and (grant.valid_until is None or clock < grant.valid_until)
            for grant in policy.grants
        )


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
        except StalePolicyBaselineError:
            try:
                await self._failed_attempt_audit.record_failed_cas(
                    command, "stale_policy_baseline"
                )
            finally:
                raise

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
            command.idempotency_receipt_id
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
                classification,
                independent,
            )
            next_generation = current.policy_generation + 1

        evidence = command.eligibility
        proposal = command.proposal
        approval = command.approval
        activation = PolicyActivation(
            policy_activation_id=command.activation_id,
            workspace_id=policy.workspace_id,
            environment_id=policy.environment_id,
            source_object_id=policy.source_object_id,
            predecessor_policy_version_id=command.expected_policy_version_id,
            new_policy_version_id=policy.access_policy_version_id,
            policy_generation=next_generation,
            policy_proposal_id=(proposal.policy_proposal_id if proposal else None),
            policy_approval_id=(approval.policy_approval_id if approval else None),
            initial_eligibility_decision_id=(evidence.decision_id if evidence else None),
            requester_canonical_human_id=command.requester_canonical_human_id,
            activator_canonical_human_id=command.activator_canonical_human_id,
            membership_generation=requester.membership_generation,
            authority_generation=requester.authority_generation,
            service_authority_generation=requester.service_authority_generation,
            idempotency_receipt_id=command.idempotency_receipt_id,
            audit_event_id=command.audit_event_id,
            outbox_event_id=command.outbox_event_id,
            activated_at=command.activated_at,
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
    "ContentAuthorizationContext",
    "ContentAuthorizationDecision",
    "ContentAuthorizationService",
    "InitialPolicyIneligibleError",
    "PolicyApprovalRequiredError",
    "PolicyAuthorityPort",
    "PolicyAuthoritySnapshot",
    "PolicyFailedAttemptAudit",
    "StalePolicyAuthorityError",
    "StalePolicyBaselineError",
    "access_policy_document_hash",
]
