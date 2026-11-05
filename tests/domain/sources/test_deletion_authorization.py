from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.domain.sources.deletion_authorization import (
    DeletionEvidenceCompleteness,
    DeletionEvidenceOutcome,
    DeletionIdentityContinuity,
    DeletionOrderingGuarantee,
    QualifiedDeletionEvidence,
    SourceDeletionPolicyApproval,
    TombstoneAuthorization,
    TombstoneAuthorizationIssuer,
)

NOW = datetime(2026, 11, 5, 9, tzinfo=timezone.utc)


def _id(value: int) -> UUID:
    return UUID(int=value)


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


def _authorization(**updates: object) -> TombstoneAuthorization:
    evidence = _evidence()
    approval = _approval()
    issuer = _issuer()
    values: dict[str, object] = {
        "authorization_id": _id(40),
        "workspace_id": evidence.workspace_id,
        "environment_id": evidence.environment_id,
        "source_object_id": evidence.source_object_id,
        "provider_kind": evidence.provider_kind,
        "source_contract_id": evidence.source_contract_id,
        "source_contract_version": evidence.source_contract_version,
        "connection_id": evidence.connection_id,
        "connection_version_id": evidence.connection_version_id,
        "authorization_generation": evidence.authorization_generation,
        "configuration_generation": evidence.configuration_generation,
        "object_namespace": evidence.object_namespace,
        "object_identity": evidence.object_identity,
        "resource_scope_reference": evidence.resource_scope_reference,
        "scope_approval_version_id": evidence.scope_approval_version_id,
        "scope_approval_digest": evidence.scope_approval_digest,
        "qualified_evidence_id": evidence.evidence_id,
        "qualified_evidence_profile_id": evidence.profile_id,
        "qualified_evidence_profile_version": evidence.profile_version,
        "qualified_evidence_digest": evidence.evidence_digest,
        "observation_id": evidence.observation_id,
        "deletion_policy_approval_id": approval.approval_id,
        "deletion_policy_version_id": approval.policy_version_id,
        "deletion_policy_digest": approval.policy_digest,
        "deletion_policy_authority_generation": approval.authority_generation,
        "approver_human_id": approval.approver_human_id,
        "approver_authority_generation": approval.approver_authority_generation,
        "issuer_authority_id": issuer.issuer_authority_id,
        "issuer_service_principal_id": issuer.service_principal_id,
        "issuer_authority_generation": issuer.authority_generation,
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


def test_confirmed_evidence_requires_complete_ordered_exact_identity_proof() -> None:
    evidence = _evidence()

    assert evidence.authorizes_issuance_at(NOW) is True

    for update in (
        {"completeness": DeletionEvidenceCompleteness.INCOMPLETE},
        {"ordering": DeletionOrderingGuarantee.UNPROVEN},
        {"identity_continuity": DeletionIdentityContinuity.UNPROVEN},
        {"observed_unique_count": 40},
        {"evidence_references": ()},
    ):
        with pytest.raises(ValidationError):
            _evidence(**update)


@pytest.mark.parametrize(
    "outcome",
    [
        DeletionEvidenceOutcome.PROVIDER_OUTAGE,
        DeletionEvidenceOutcome.PERMISSION_LOSS,
        DeletionEvidenceOutcome.FILTERED_ABSENCE,
        DeletionEvidenceOutcome.MOVE_OR_TRASH_ONLY,
        DeletionEvidenceOutcome.NOT_VERIFIED,
    ],
)
def test_non_authorizing_evidence_never_authorizes_tombstone(
    outcome: DeletionEvidenceOutcome,
) -> None:
    evidence = _evidence(
        outcome=outcome,
        completeness=DeletionEvidenceCompleteness.UNPROVEN,
        ordering=DeletionOrderingGuarantee.UNPROVEN,
        identity_continuity=DeletionIdentityContinuity.UNPROVEN,
        expected_count=None,
        observed_unique_count=None,
        ordered_inventory_digest=None,
    )

    assert evidence.authorizes_issuance_at(NOW) is False


def test_authorization_binds_exact_evidence_policy_issuer_head_and_transition() -> None:
    authorization = _authorization()

    authorization.validate_bindings(
        evidence=_evidence(), approval=_approval(), issuer=_issuer(), evaluated_at=NOW
    )

    with pytest.raises(ValueError, match="binding"):
        authorization.model_copy(
            update={"expected_current_revision_id": _id(99)}
        ).validate_expected_head(
            revision_id=_id(41), acceptance_id=_id(42), cas_version=5
        )


def test_policy_approver_and_trusted_issuer_must_be_separate_identities() -> None:
    authorization = _authorization(issuer_service_principal_id=_id(23))

    with pytest.raises(ValueError, match="separation"):
        authorization.validate_bindings(
            evidence=_evidence(),
            approval=_approval(),
            issuer=_issuer(service_principal_id=_id(23)),
            evaluated_at=NOW,
        )


def test_evidence_qualifier_and_trusted_issuer_must_be_separate_services() -> None:
    evidence = _evidence(qualifier_service_principal_id=_id(31))

    with pytest.raises(ValueError, match="qualifier separation"):
        _authorization().validate_bindings(
            evidence=evidence,
            approval=_approval(),
            issuer=_issuer(),
            evaluated_at=NOW,
        )


def test_authorization_is_single_transition_and_content_free() -> None:
    authorization = _authorization()

    assert authorization.transition_binding == (
        _id(4),
        _id(41),
        _id(42),
        5,
        _id(43),
        _id(44),
    )
    serialized = authorization.model_dump_json()
    assert "provider_payload" not in serialized
    assert "source_content" not in serialized


def test_yandex_positive_evidence_requires_strict_two_pass_ordered_digest() -> None:
    with pytest.raises(ValidationError, match="Yandex"):
        _evidence(ordering=DeletionOrderingGuarantee.TRUSTED_SNAPSHOT)
    with pytest.raises(ValidationError, match="Yandex"):
        _evidence(profile_id="provider.generic-absence")


def test_bitrix24_positive_deletion_guarantees_remain_not_verified() -> None:
    with pytest.raises(ValidationError, match="Bitrix24"):
        _evidence(provider_kind="bitrix24")
