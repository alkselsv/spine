from __future__ import annotations

from dataclasses import replace
import asyncio
from datetime import datetime, timezone
from uuid import UUID

import pytest

from spine.application.persistence.idempotency import IdempotencyKey
from spine.application.persistence.errors import IdempotencyConflictError

from spine.application.access_policies import (
    AccessPolicyActivationCommand,
    AccessPolicyActivationService,
    InitialPolicyIneligibleError,
    PolicyApprovalRequiredError,
    PolicyAuthoritySnapshot,
    StalePolicyAuthorityError,
    StalePolicyBaselineError,
    access_policy_document_hash,
)
from spine.domain.sources.access_policy import (
    AccessGrant,
    AccessPermission,
    AccessPolicyVersion,
    EligibilityDisposition,
    InitialPolicyEligibilityDecision,
    PolicyApproval,
    PolicyApprovalDecision,
    PolicyBenefitClassification,
    PolicyOriginKind,
    PolicyProposal,
    PrincipalKind,
)
from spine.infrastructure.persistence.access_policies import (
    InMemoryAccessPolicyRepository,
)


NOW = datetime(2026, 10, 12, tzinfo=timezone.utc)


def _id(value: int) -> UUID:
    return UUID(int=value)


class Authority:
    def __init__(self) -> None:
        snapshot = PolicyAuthoritySnapshot(
            canonical_human_id=_id(20),
            workspace_id=_id(2),
            environment_id=_id(3),
            membership_generation=7,
            authority_generation=11,
            service_authority_generation=13,
            is_policy_administrator=True,
            snapshot_reference=_id(21),
        )
        self.snapshots = {snapshot.canonical_human_id: snapshot}
        self.classification = PolicyBenefitClassification.REVOCATION
        self.failed_cas: list[str] = []

    @property
    def snapshot(self) -> PolicyAuthoritySnapshot:
        return self.snapshots[_id(20)]

    async def resolve(self, canonical_human_id: UUID) -> PolicyAuthoritySnapshot:
        return self.snapshots[canonical_human_id]

    async def classify_policy_change(
        self,
        requester: PolicyAuthoritySnapshot,
        current_policy_version_id: UUID,
        proposed_policy: AccessPolicyVersion,
    ) -> PolicyBenefitClassification:
        del requester, current_policy_version_id, proposed_policy
        return self.classification

    async def are_independent_humans(
        self,
        requester: PolicyAuthoritySnapshot,
        approver: PolicyAuthoritySnapshot,
    ) -> bool:
        return requester.canonical_human_id != approver.canonical_human_id

    async def record_failed_cas(
        self, command: AccessPolicyActivationCommand, reason_code: str
    ) -> None:
        del command
        self.failed_cas.append(reason_code)

    def add_approver(self) -> PolicyAuthoritySnapshot:
        snapshot = PolicyAuthoritySnapshot(
            canonical_human_id=_id(22),
            workspace_id=_id(2),
            environment_id=_id(3),
            membership_generation=17,
            authority_generation=19,
            service_authority_generation=23,
            is_policy_administrator=True,
            snapshot_reference=_id(24),
        )
        self.snapshots[snapshot.canonical_human_id] = snapshot
        return snapshot


class UnavailableFailedAttemptAudit:
    async def record_failed_cas(
        self, command: AccessPolicyActivationCommand, reason_code: str
    ) -> None:
        del command, reason_code
        raise RuntimeError("audit unavailable")


def _policy() -> AccessPolicyVersion:
    policy = AccessPolicyVersion(
        access_policy_version_id=_id(1),
        workspace_id=_id(2),
        environment_id=_id(3),
        source_object_id=_id(4),
        version=1,
        predecessor_policy_version_id=None,
        document_schema_version="access-policy.v1",
        document_hash="a" * 64,
        origin_kind=PolicyOriginKind.ELIGIBLE_TEMPLATE,
        origin_reference=_id(5),
        origin_version=3,
        origin_hash="b" * 64,
        created_by_canonical_human_id=_id(20),
        created_at=NOW,
    )
    return policy.model_copy(update={"document_hash": access_policy_document_hash(policy)})


def _eligibility(policy: AccessPolicyVersion) -> InitialPolicyEligibilityDecision:
    return InitialPolicyEligibilityDecision(
        decision_id=_id(30),
        workspace_id=policy.workspace_id,
        environment_id=policy.environment_id,
        source_object_id=policy.source_object_id,
        policy_version_id=policy.access_policy_version_id,
        policy_hash=policy.document_hash,
        origin_kind=policy.origin_kind,
        origin_reference=policy.origin_reference,
        origin_version=policy.origin_version,
        origin_hash=policy.origin_hash,
        membership_generation=7,
        authority_generation=11,
        service_authority_generation=13,
        disposition=EligibilityDisposition.ELIGIBLE,
        reason_code="eligible_template",
        evaluator_contract_version="initial-policy.v1",
        authority_snapshot_reference=_id(21),
        audit_event_id=_id(31),
        decided_at=NOW,
    )


def _command(
    policy: AccessPolicyVersion,
    eligibility: InitialPolicyEligibilityDecision,
) -> AccessPolicyActivationCommand:
    return AccessPolicyActivationCommand(
        policy=policy,
        requester_canonical_human_id=_id(20),
        activator_canonical_human_id=_id(20),
        expected_policy_version_id=None,
        expected_policy_generation=0,
        expected_cas_version=0,
        eligibility=eligibility,
        activation_id=_id(40),
        idempotency_key=IdempotencyKey("initial-policy"),
        audit_event_id=_id(42),
        outbox_event_id=_id(43),
        activated_at=NOW,
    )


@pytest.mark.asyncio
async def test_initial_activation_requires_exact_current_eligibility() -> None:
    authority = Authority()
    repository = InMemoryAccessPolicyRepository()
    service = AccessPolicyActivationService(repository, authority, authority)
    policy = _policy()

    result = await service.activate(_command(policy, _eligibility(policy)))

    assert result.current.access_policy_version_id == policy.access_policy_version_id
    assert result.current.policy_generation == 1
    assert result.outbox_event_id == _id(43)
    assert await repository.activation_count() == 1

    replay = await service.activate(_command(policy, _eligibility(policy)))

    assert replay.replay is True
    assert replay.outbox_event_id == result.outbox_event_id
    assert await repository.activation_count() == 1
    assert await repository.success_effect_count() == 2


@pytest.mark.asyncio
async def test_initial_activation_mismatch_has_no_success_effect() -> None:
    authority = Authority()
    repository = InMemoryAccessPolicyRepository()
    service = AccessPolicyActivationService(repository, authority, authority)
    policy = _policy()
    eligibility = _eligibility(policy).model_copy(update={"policy_hash": "c" * 64})

    with pytest.raises(InitialPolicyIneligibleError):
        await service.activate(_command(policy, eligibility))

    assert await repository.resolve_current(policy.source_object_id) is None
    assert await repository.activation_count() == 0
    assert await repository.success_effect_count() == 0


@pytest.mark.asyncio
async def test_reused_idempotency_key_with_changed_command_is_rejected() -> None:
    authority = Authority()
    repository = InMemoryAccessPolicyRepository()
    service = AccessPolicyActivationService(repository, authority, authority)
    policy = _policy()
    command = _command(policy, _eligibility(policy))
    await service.activate(command)

    with pytest.raises(IdempotencyConflictError):
        await service.activate(replace(command, outbox_event_id=_id(44)))

    assert await repository.activation_count() == 1
    assert await repository.success_effect_count() == 2


def _successor(
    predecessor: AccessPolicyVersion,
    *,
    version_id: int = 50,
) -> AccessPolicyVersion:
    policy = AccessPolicyVersion(
        access_policy_version_id=_id(version_id),
        workspace_id=predecessor.workspace_id,
        environment_id=predecessor.environment_id,
        source_object_id=predecessor.source_object_id,
        version=predecessor.version + 1,
        predecessor_policy_version_id=predecessor.access_policy_version_id,
        document_schema_version="access-policy.v1",
        document_hash="c" * 64,
        origin_kind=PolicyOriginKind.APPROVED_PROPOSAL,
        origin_reference=_id(51),
        origin_version=1,
        origin_hash="d" * 64,
        created_by_canonical_human_id=_id(20),
        created_at=NOW,
    )
    return policy.model_copy(update={"document_hash": access_policy_document_hash(policy)})


def _proposal(
    policy: AccessPolicyVersion,
    classification: PolicyBenefitClassification,
) -> PolicyProposal:
    assert policy.predecessor_policy_version_id is not None
    return PolicyProposal(
        policy_proposal_id=_id(60),
        workspace_id=policy.workspace_id,
        environment_id=policy.environment_id,
        source_object_id=policy.source_object_id,
        proposal_version=1,
        proposed_policy_version_id=policy.access_policy_version_id,
        proposed_policy_hash=policy.document_hash,
        requester_canonical_human_id=_id(20),
        expected_policy_version_id=policy.predecessor_policy_version_id,
        expected_policy_generation=1,
        benefit_classification=classification,
        benefit_schema_version="policy-benefit.v1",
        membership_generation=7,
        authority_generation=11,
        service_authority_generation=13,
        submitted_at=NOW,
    )


def _approval(proposal: PolicyProposal, approver: PolicyAuthoritySnapshot) -> PolicyApproval:
    return PolicyApproval(
        policy_approval_id=_id(70),
        workspace_id=proposal.workspace_id,
        environment_id=proposal.environment_id,
        policy_proposal_id=proposal.policy_proposal_id,
        proposal_version=proposal.proposal_version,
        proposal_hash=proposal.proposed_policy_hash,
        approver_canonical_human_id=approver.canonical_human_id,
        identity_proof_reference=_id(71),
        authority_snapshot_reference=approver.snapshot_reference,
        membership_generation=approver.membership_generation,
        authority_generation=approver.authority_generation,
        decision=PolicyApprovalDecision.APPROVED,
        reason_code="approved",
        decided_at=NOW,
    )


def _successor_command(
    policy: AccessPolicyVersion,
    proposal: PolicyProposal,
    *,
    approver: PolicyAuthoritySnapshot | None = None,
    approval: PolicyApproval | None = None,
) -> AccessPolicyActivationCommand:
    return AccessPolicyActivationCommand(
        policy=policy,
        requester_canonical_human_id=_id(20),
        activator_canonical_human_id=(
            approver.canonical_human_id if approver is not None else _id(20)
        ),
        expected_policy_version_id=policy.predecessor_policy_version_id,
        expected_policy_generation=1,
        expected_cas_version=1,
        proposal=proposal,
        approval=approval,
        activation_id=_id(80),
        idempotency_key=IdempotencyKey(f"successor-{policy.access_policy_version_id}"),
        audit_event_id=_id(82),
        outbox_event_id=_id(83),
        activated_at=NOW,
    )


async def _activate_initial(
    service: AccessPolicyActivationService,
) -> AccessPolicyVersion:
    policy = _policy()
    await service.activate(_command(policy, _eligibility(policy)))
    return policy


@pytest.mark.asyncio
async def test_revocation_activates_without_independent_approval_and_retains_history() -> None:
    authority = Authority()
    repository = InMemoryAccessPolicyRepository()
    service = AccessPolicyActivationService(repository, authority, authority)
    predecessor = await _activate_initial(service)
    policy = _successor(predecessor)
    proposal = _proposal(policy, PolicyBenefitClassification.REVOCATION)

    result = await service.activate(_successor_command(policy, proposal))

    assert result.current.policy_generation == 2
    assert await repository.activation_count() == 2


@pytest.mark.asyncio
async def test_caller_cannot_downgrade_self_benefiting_classification() -> None:
    authority = Authority()
    authority.classification = PolicyBenefitClassification.SELF_BENEFITING
    repository = InMemoryAccessPolicyRepository()
    service = AccessPolicyActivationService(repository, authority, authority)
    predecessor = await _activate_initial(service)
    policy = _successor(predecessor)
    mislabeled = _proposal(policy, PolicyBenefitClassification.OTHER_ONLY)

    with pytest.raises(StalePolicyAuthorityError):
        await service.activate(_successor_command(policy, mislabeled))

    assert await repository.activation_count() == 1


@pytest.mark.asyncio
async def test_policy_hash_rejects_unapproved_grant_change() -> None:
    authority = Authority()
    repository = InMemoryAccessPolicyRepository()
    service = AccessPolicyActivationService(repository, authority, authority)
    predecessor = await _activate_initial(service)
    policy = _successor(predecessor)
    proposal = _proposal(policy, PolicyBenefitClassification.REVOCATION)
    tampered = policy.model_copy(
        update={
            "grants": (
                AccessGrant(
                    access_grant_id=_id(88),
                    principal_kind=PrincipalKind.HUMAN,
                    principal_id=_id(89),
                    permission=AccessPermission.READ_CONTENT,
                ),
            )
        }
    )

    with pytest.raises(StalePolicyAuthorityError):
        await service.activate(_successor_command(tampered, proposal))

    assert await repository.activation_count() == 1


@pytest.mark.asyncio
async def test_self_benefiting_change_rejects_same_canonical_human_alias() -> None:
    authority = Authority()
    authority.classification = PolicyBenefitClassification.SELF_BENEFITING
    repository = InMemoryAccessPolicyRepository()
    service = AccessPolicyActivationService(repository, authority, authority)
    predecessor = await _activate_initial(service)
    policy = _successor(predecessor)
    proposal = _proposal(policy, PolicyBenefitClassification.SELF_BENEFITING)
    same_human_approval = _approval(proposal, authority.snapshot)

    with pytest.raises(PolicyApprovalRequiredError):
        await service.activate(
            _successor_command(
                policy,
                proposal,
                approver=authority.snapshot,
                approval=same_human_approval,
            )
        )

    assert await repository.activation_count() == 1


@pytest.mark.asyncio
async def test_generation_change_invalidates_independent_approval() -> None:
    authority = Authority()
    authority.classification = PolicyBenefitClassification.SELF_BENEFITING
    approver = authority.add_approver()
    repository = InMemoryAccessPolicyRepository()
    service = AccessPolicyActivationService(repository, authority, authority)
    predecessor = await _activate_initial(service)
    policy = _successor(predecessor)
    proposal = _proposal(policy, PolicyBenefitClassification.SELF_BENEFITING)
    approval = _approval(proposal, approver)
    authority.snapshots[approver.canonical_human_id] = approver.model_copy(
        update={"authority_generation": 20}
    )

    with pytest.raises(PolicyApprovalRequiredError):
        await service.activate(
            _successor_command(
                policy, proposal, approver=approver, approval=approval
            )
        )

    assert await repository.activation_count() == 1


@pytest.mark.asyncio
async def test_stale_baseline_has_no_activation_audit_or_outbox_effect() -> None:
    authority = Authority()
    repository = InMemoryAccessPolicyRepository()
    service = AccessPolicyActivationService(repository, authority, authority)
    predecessor = await _activate_initial(service)
    policy = _successor(predecessor)
    proposal = _proposal(policy, PolicyBenefitClassification.REVOCATION)
    command = _successor_command(policy, proposal)
    stale = replace(command, expected_policy_generation=0)

    with pytest.raises(StalePolicyBaselineError):
        await service.activate(stale)

    assert await repository.activation_count() == 1
    assert await repository.success_effect_count() == 2
    assert authority.failed_cas == ["stale_policy_baseline"]


@pytest.mark.asyncio
async def test_successor_must_directly_follow_current_policy() -> None:
    authority = Authority()
    repository = InMemoryAccessPolicyRepository()
    service = AccessPolicyActivationService(repository, authority, authority)
    predecessor = await _activate_initial(service)
    policy = _successor(predecessor).model_copy(update={"version": 3})
    policy = policy.model_copy(
        update={"document_hash": access_policy_document_hash(policy)}
    )
    proposal = _proposal(policy, PolicyBenefitClassification.REVOCATION)

    with pytest.raises(StalePolicyBaselineError):
        await service.activate(_successor_command(policy, proposal))

    assert await repository.activation_count() == 1


@pytest.mark.asyncio
async def test_failed_attempt_audit_outage_cannot_turn_stale_cas_into_success() -> None:
    authority = Authority()
    repository = InMemoryAccessPolicyRepository()
    service = AccessPolicyActivationService(
        repository, authority, UnavailableFailedAttemptAudit()
    )
    predecessor = await _activate_initial(
        AccessPolicyActivationService(repository, authority, authority)
    )
    policy = _successor(predecessor)
    proposal = _proposal(policy, PolicyBenefitClassification.REVOCATION)

    with pytest.raises(StalePolicyBaselineError):
        await service.activate(
            replace(
                _successor_command(policy, proposal),
                expected_policy_generation=0,
            )
        )

    assert await repository.activation_count() == 1


@pytest.mark.asyncio
async def test_concurrent_successors_have_one_cas_winner() -> None:
    authority = Authority()
    repository = InMemoryAccessPolicyRepository()
    service = AccessPolicyActivationService(repository, authority, authority)
    predecessor = await _activate_initial(service)
    first = _successor(predecessor, version_id=50)
    second = _successor(predecessor, version_id=51)
    commands = (
        _successor_command(
            first, _proposal(first, PolicyBenefitClassification.REVOCATION)
        ),
        replace(
            _successor_command(
                second, _proposal(second, PolicyBenefitClassification.REVOCATION)
            ),
            activation_id=_id(84),
            idempotency_key=IdempotencyKey("concurrent-second"),
            audit_event_id=_id(86),
            outbox_event_id=_id(87),
        ),
    )

    outcomes = await asyncio.gather(
        *(service.activate(command) for command in commands),
        return_exceptions=True,
    )

    assert sum(not isinstance(outcome, Exception) for outcome in outcomes) == 1
    assert sum(isinstance(outcome, StalePolicyBaselineError) for outcome in outcomes) == 1
    assert await repository.activation_count() == 2
    assert await repository.success_effect_count() == 4


@pytest.mark.asyncio
async def test_edited_proposal_invalidates_predecessor_approval() -> None:
    authority = Authority()
    authority.classification = PolicyBenefitClassification.SELF_BENEFITING
    approver = authority.add_approver()
    repository = InMemoryAccessPolicyRepository()
    service = AccessPolicyActivationService(repository, authority, authority)
    predecessor = await _activate_initial(service)
    policy = _successor(predecessor)
    original = _proposal(policy, PolicyBenefitClassification.SELF_BENEFITING)
    old_approval = _approval(original, approver)
    edited = original.model_copy(
        update={"proposal_version": 2, "proposed_policy_hash": "e" * 64}
    )
    edited_policy = policy.model_copy(update={"document_hash": "e" * 64})
    edited_policy = edited_policy.model_copy(
        update={"document_hash": access_policy_document_hash(edited_policy)}
    )
    edited = edited.model_copy(
        update={"proposed_policy_hash": edited_policy.document_hash}
    )

    with pytest.raises(PolicyApprovalRequiredError):
        await service.activate(
            _successor_command(
                edited_policy,
                edited,
                approver=approver,
                approval=old_approval,
            )
        )

    assert await repository.activation_count() == 1


@pytest.mark.asyncio
async def test_pending_proposal_and_rejected_approval_are_retained() -> None:
    authority = Authority()
    authority.classification = PolicyBenefitClassification.SELF_BENEFITING
    approver = authority.add_approver()
    repository = InMemoryAccessPolicyRepository()
    service = AccessPolicyActivationService(repository, authority, authority)
    predecessor = await _activate_initial(service)
    policy = _successor(predecessor)
    proposal = _proposal(policy, PolicyBenefitClassification.SELF_BENEFITING)

    persisted = await service.submit_proposal(policy, proposal)
    rejected = _approval(proposal, approver).model_copy(
        update={
            "decision": PolicyApprovalDecision.REJECTED,
            "reason_code": "rejected",
        }
    )
    recorded = await service.record_approval(rejected)

    assert persisted == proposal
    assert recorded.decision is PolicyApprovalDecision.REJECTED
    assert await repository.activation_count() == 1
