from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
    TrustedContextProvenance,
    TrustedPersistenceContext,
)
from spine.application.tombstone_authorization import (
    InMemoryTombstoneAuthorizationRepository,
    IssueTombstoneAuthorizationCommand,
    TombstoneAuthorizationOutcome,
    TombstoneAuthorizationService,
)
from spine.domain.common import ContextOrigin
from spine.domain.sources.canonical import CurrentSourceHead
from spine.domain.sources.deletion_authorization import (
    DeletionEvidenceCompleteness,
    DeletionEvidenceOutcome,
    DeletionIdentityContinuity,
    DeletionOrderingGuarantee,
    QualifiedDeletionEvidence,
    SourceDeletionPolicyApproval,
    TombstoneAuthorization,
    TombstoneAuthorizationDecision,
    TombstoneAuthorizationDisposition,
    TombstoneAuthorizationIssuer,
)
from spine.domain.sources.models import RevisionKind

NOW = datetime(2026, 11, 5, 9, tzinfo=timezone.utc)


def _id(value: int) -> UUID:
    return UUID(int=value)


def _context(*, workspace: int = 2, service: int = 31) -> TrustedPersistenceContext:
    return TrustedPersistenceContext(
        scope=EnvironmentScope(workspace_id=_id(workspace), environment_id=_id(3)),
        origin=ContextOrigin.WORKER,
        purpose=PersistencePurpose("tombstone_authorization"),
        operation=PersistenceOperation("issue"),
        trace_id=_id(90),
        acting_subject_id=None,
        service_principal_id=_id(service),
        provenance=TrustedContextProvenance(issuer_id=_id(91), signature=b"trusted"),
    )


def _evidence(**updates: object) -> QualifiedDeletionEvidence:
    values: dict[str, object] = {
        "evidence_id": _id(1),
        "workspace_id": _id(2),
        "environment_id": _id(3),
        "source_object_id": _id(4),
        "provider_kind": "yandex_disk",
        "source_contract_id": "yandex_disk.file",
        "source_contract_version": "v1",
        "connection_id": _id(5),
        "connection_version_id": _id(6),
        "authorization_generation": 7,
        "configuration_generation": 8,
        "object_namespace": "yandex-disk",
        "object_identity": "ydx:stable-object",
        "resource_scope_reference": _id(9),
        "scope_approval_version_id": _id(10),
        "scope_approval_digest": "a" * 64,
        "qualifier_service_principal_id": _id(14),
        "qualification_authority_reference": _id(15),
        "qualification_authority_generation": 2,
        "observation_id": _id(11),
        "scan_identity": "scan-42",
        "profile_id": "yandex.two-pass-removal",
        "profile_version": "v1",
        "evidence_digest": "b" * 64,
        "evidence_references": (_id(12), _id(13)),
        "outcome": DeletionEvidenceOutcome.CONFIRMED_QUALIFIED_DELETION,
        "completeness": DeletionEvidenceCompleteness.COMPLETE,
        "ordering": DeletionOrderingGuarantee.STABLE_ORDERED_DIGEST,
        "identity_continuity": DeletionIdentityContinuity.CONFIRMED,
        "expected_count": 41,
        "observed_unique_count": 41,
        "ordered_inventory_digest": "c" * 64,
        "observed_at": NOW - timedelta(minutes=2),
        "fresh_until": NOW + timedelta(minutes=3),
        "verified_at": NOW,
    }
    values.update(updates)
    return QualifiedDeletionEvidence(**values)


def _approval(**updates: object) -> SourceDeletionPolicyApproval:
    values: dict[str, object] = {
        "approval_id": _id(20),
        "workspace_id": _id(2),
        "environment_id": _id(3),
        "policy_id": _id(21),
        "policy_version_id": _id(22),
        "policy_digest": "d" * 64,
        "source_contract_id": "yandex_disk.file",
        "source_contract_version": "v1",
        "connection_id": _id(5),
        "resource_scope_reference": _id(9),
        "approver_human_id": _id(23),
        "approver_authority_reference": _id(23),
        "approver_authority_generation": 4,
        "authority_generation": 3,
        "approved_at": NOW - timedelta(days=1),
        "valid_until": NOW + timedelta(days=1),
        "audit_event_id": _id(25),
    }
    values.update(updates)
    return SourceDeletionPolicyApproval(**values)


def _issuer(**updates: object) -> TombstoneAuthorizationIssuer:
    values: dict[str, object] = {
        "issuer_authority_id": _id(30),
        "workspace_id": _id(2),
        "environment_id": _id(3),
        "service_principal_id": _id(31),
        "authorized_by_human_id": _id(33),
        "authorized_by_authority_generation": 4,
        "authority_version": "v1",
        "authority_digest": "e" * 64,
        "authority_generation": 2,
        "valid_from": NOW - timedelta(days=1),
        "valid_until": NOW + timedelta(days=1),
        "audit_event_id": _id(32),
    }
    values.update(updates)
    return TombstoneAuthorizationIssuer(**values)


def _head(**updates: object) -> CurrentSourceHead:
    values: dict[str, object] = {
        "source_object_id": _id(4),
        "workspace_id": _id(2),
        "environment_id": _id(3),
        "current_revision_id": _id(41),
        "current_revision_kind": RevisionKind.CONTENT,
        "acceptance_id": _id(42),
        "accepted_generation": 4,
        "cas_version": 5,
        "changed_at": NOW - timedelta(days=2),
    }
    values.update(updates)
    return CurrentSourceHead(**values)


def _authorization(**updates: object) -> TombstoneAuthorization:
    values: dict[str, object] = {
        "authorization_id": _id(40),
        "workspace_id": _id(2),
        "environment_id": _id(3),
        "source_object_id": _id(4),
        "provider_kind": "yandex_disk",
        "source_contract_id": "yandex_disk.file",
        "source_contract_version": "v1",
        "connection_id": _id(5),
        "connection_version_id": _id(6),
        "authorization_generation": 7,
        "configuration_generation": 8,
        "object_namespace": "yandex-disk",
        "object_identity": "ydx:stable-object",
        "resource_scope_reference": _id(9),
        "scope_approval_version_id": _id(10),
        "scope_approval_digest": "a" * 64,
        "qualified_evidence_id": _id(1),
        "qualified_evidence_profile_id": "yandex.two-pass-removal",
        "qualified_evidence_profile_version": "v1",
        "qualified_evidence_digest": "b" * 64,
        "observation_id": _id(11),
        "deletion_policy_approval_id": _id(20),
        "deletion_policy_version_id": _id(22),
        "deletion_policy_digest": "d" * 64,
        "deletion_policy_authority_generation": 3,
        "approver_human_id": _id(23),
        "approver_authority_generation": 4,
        "issuer_authority_id": _id(30),
        "issuer_service_principal_id": _id(31),
        "issuer_authority_generation": 2,
        "expected_current_revision_id": _id(41),
        "expected_current_acceptance_id": _id(42),
        "expected_current_cas_version": 5,
        "tombstone_revision_id": _id(43),
        "transition_id": _id(44),
        "retention_eligibility_reference": _id(45),
        "retention_eligibility_generation": 6,
        "legal_hold_check_reference": _id(46),
        "legal_hold_generation": 7,
        "idempotency_key": "delete-source-4-head-5",
        "command_digest": "f" * 64,
        "audit_event_id": _id(47),
        "issued_at": NOW,
        "expires_at": NOW + timedelta(minutes=5),
    }
    values.update(updates)
    return TombstoneAuthorization(**values)


class Authority:
    def __init__(self) -> None:
        self.evidence = _evidence()
        self.approval = _approval()
        self.issuer = _issuer()
        self.head = _head()
        self.retention_eligible = True
        self.legal_hold_clear = True

    async def resolve_evidence(self, authorization, context):
        return self.evidence

    async def resolve_current_policy_approval(self, authorization, context):
        return self.approval

    async def resolve_current_issuer(self, authorization, context):
        return self.issuer

    async def resolve_current_head(self, authorization, context):
        return self.head

    async def retention_and_legal_eligible(self, authorization, context):
        return self.retention_eligible and self.legal_hold_clear


def _service(authority: Authority | None = None):
    authority = authority or Authority()
    repository = InMemoryTombstoneAuthorizationRepository()
    return (
        TombstoneAuthorizationService(repository, authority, clock=lambda: NOW),
        repository,
    )


@pytest.mark.asyncio
async def test_trusted_issuer_issues_once_and_exact_replay_is_stable() -> None:
    service, repository = _service()
    command = IssueTombstoneAuthorizationCommand(_authorization())

    first = await service.issue(command, _context())
    replay = await service.issue(command, _context())

    assert first.outcome is TombstoneAuthorizationOutcome.ISSUED
    assert replay.outcome is TombstoneAuthorizationOutcome.REPLAYED
    assert replay.authorization == first.authorization
    assert await repository.is_current(_id(40), _context()) is True


@pytest.mark.asyncio
async def test_changed_payload_under_command_key_conflicts_without_second_effect() -> (
    None
):
    service, repository = _service()
    await service.issue(
        IssueTombstoneAuthorizationCommand(_authorization()), _context()
    )

    changed = _authorization(authorization_id=_id(48), command_digest="9" * 64)
    result = await service.issue(
        IssueTombstoneAuthorizationCommand(changed), _context()
    )

    assert result.outcome is TombstoneAuthorizationOutcome.IDEMPOTENCY_CONFLICT
    assert len(repository.authorizations) == 1


async def _revoke(
    repository: InMemoryTombstoneAuthorizationRepository,
) -> None:
    current = repository.current_decisions[_id(40)]
    revoke = TombstoneAuthorizationDecision(
        decision_id=_id(50),
        workspace_id=_id(2),
        environment_id=_id(3),
        source_object_id=_id(4),
        authorization_id=_id(40),
        disposition=TombstoneAuthorizationDisposition.REVOKED,
        expected_current_decision_id=current.decision_id,
        expected_current_generation=current.generation,
        generation=current.generation + 1,
        authority_reference=_id(30),
        authority_generation=2,
        audit_event_id=_id(52),
        decided_at=NOW + timedelta(seconds=1),
    )
    await repository.record_decision(revoke, _context())


@pytest.mark.asyncio
async def test_revocation_makes_authorization_non_current() -> None:
    service, repository = _service()
    await service.issue(
        IssueTombstoneAuthorizationCommand(_authorization()), _context()
    )
    await _revoke(repository)

    assert await repository.is_current(_id(40), _context()) is False


@pytest.mark.asyncio
async def test_replay_returns_same_artifact_after_revocation() -> None:
    service, repository = _service()
    await service.issue(
        IssueTombstoneAuthorizationCommand(_authorization()), _context()
    )
    await _revoke(repository)

    replay = await service.issue(
        IssueTombstoneAuthorizationCommand(_authorization()), _context()
    )
    assert replay.outcome is TombstoneAuthorizationOutcome.REPLAYED


@pytest.mark.asyncio
async def test_second_command_for_same_head_is_denied() -> None:
    service, _ = _service()
    await service.issue(
        IssueTombstoneAuthorizationCommand(_authorization()), _context()
    )
    reused = _authorization(
        authorization_id=_id(60),
        transition_id=_id(61),
        tombstone_revision_id=_id(62),
        idempotency_key="second-command",
        command_digest="8" * 64,
        audit_event_id=_id(63),
    )
    denied = await service.issue(IssueTombstoneAuthorizationCommand(reused), _context())
    assert denied.outcome is TombstoneAuthorizationOutcome.TOMBSTONE_NOT_AUTHORIZED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", ["evidence", "approval", "issuer", "head", "retention", "legal"]
)
async def test_stale_missing_or_unsupported_current_guarantee_fails_closed(
    case: str,
) -> None:
    authority = Authority()
    if case == "evidence":
        authority.evidence = _evidence(
            outcome=DeletionEvidenceOutcome.NOT_VERIFIED,
            completeness=DeletionEvidenceCompleteness.UNPROVEN,
            ordering=DeletionOrderingGuarantee.UNPROVEN,
            identity_continuity=DeletionIdentityContinuity.UNPROVEN,
            expected_count=None,
            observed_unique_count=None,
            ordered_inventory_digest=None,
        )
    elif case == "approval":
        authority.approval = _approval(authority_generation=99)
    elif case == "issuer":
        authority.issuer = _issuer(authority_generation=99)
    elif case == "head":
        authority.head = _head(cas_version=6)
    elif case == "retention":
        authority.retention_eligible = False
    else:
        authority.legal_hold_clear = False
    service, _ = _service(authority)

    result = await service.issue(
        IssueTombstoneAuthorizationCommand(_authorization()), _context()
    )

    assert result.outcome is TombstoneAuthorizationOutcome.TOMBSTONE_NOT_AUTHORIZED
    assert result.authorization is None


@pytest.mark.asyncio
async def test_workspace_and_issuer_identity_are_fail_closed() -> None:
    service, _ = _service()

    wrong_workspace = await service.issue(
        IssueTombstoneAuthorizationCommand(_authorization()), _context(workspace=99)
    )
    wrong_issuer = await service.issue(
        IssueTombstoneAuthorizationCommand(_authorization()), _context(service=99)
    )

    assert (
        wrong_workspace.outcome
        is TombstoneAuthorizationOutcome.TOMBSTONE_NOT_AUTHORIZED
    )
    assert (
        wrong_issuer.outcome is TombstoneAuthorizationOutcome.TOMBSTONE_NOT_AUTHORIZED
    )


@pytest.mark.asyncio
async def test_current_policy_or_issuer_revocation_fails_closed() -> None:
    authority = Authority()
    service, _ = _service(authority)
    authority.approval = _approval(revoked=True)
    denied_policy = await service.issue(
        IssueTombstoneAuthorizationCommand(_authorization()), _context()
    )
    authority.approval = _approval()
    authority.issuer = _issuer(revoked=True)
    denied_issuer = await service.issue(
        IssueTombstoneAuthorizationCommand(_authorization()), _context()
    )

    assert (
        denied_policy.outcome is TombstoneAuthorizationOutcome.TOMBSTONE_NOT_AUTHORIZED
    )
    assert (
        denied_issuer.outcome is TombstoneAuthorizationOutcome.TOMBSTONE_NOT_AUTHORIZED
    )


@pytest.mark.asyncio
async def test_unknown_commit_reconciles_by_original_command_identity() -> None:
    class UnknownCommitRepository(InMemoryTombstoneAuthorizationRepository):
        async def issue(self, authorization, context):
            await super().issue(authorization, context)
            raise RuntimeError("commit outcome unavailable")

    authority = Authority()
    repository = UnknownCommitRepository()
    service = TombstoneAuthorizationService(repository, authority, clock=lambda: NOW)

    result = await service.issue(
        IssueTombstoneAuthorizationCommand(_authorization()), _context()
    )

    assert result.outcome is TombstoneAuthorizationOutcome.REPLAYED
    assert result.authorization == _authorization()
