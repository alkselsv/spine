from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.domain.sources.access_policy import (
    AccessGrant,
    AccessPermission,
    AccessPolicyVersion,
    PolicyOriginKind,
    PrincipalKind,
    SourceSecurityDomainBinding,
)


NOW = datetime(2026, 10, 12, tzinfo=timezone.utc)


def _id(value: int) -> UUID:
    return UUID(int=value)


def _policy(*grants: AccessGrant) -> AccessPolicyVersion:
    return AccessPolicyVersion(
        access_policy_version_id=_id(1),
        workspace_id=_id(2),
        environment_id=_id(3),
        source_object_id=_id(4),
        version=1,
        predecessor_policy_version_id=None,
        document_schema_version="access-policy.v1",
        document_hash="a" * 64,
        origin_kind=PolicyOriginKind.APPROVED_PROPOSAL,
        origin_reference=_id(5),
        origin_version=1,
        origin_hash="b" * 64,
        created_by_canonical_human_id=_id(6),
        created_at=NOW,
        grants=grants,
    )


def test_empty_policy_is_valid_deny_all_successor() -> None:
    assert _policy().grants == ()


def test_policy_binding_requires_v2_but_keeps_v1_shape_compatible() -> None:
    binding_id = _id(40)
    assert _policy().security_domain_binding_id is None

    policy = AccessPolicyVersion.model_validate(
        {
            **_policy().model_dump(),
            "document_schema_version": "access-policy.v2",
            "security_domain_binding_id": binding_id,
        }
    )

    assert policy.security_domain_binding_id == binding_id

    with pytest.raises(ValidationError, match="access-policy.v2"):
        AccessPolicyVersion.model_validate(
            {**_policy().model_dump(), "security_domain_binding_id": binding_id}
        )


def test_security_domain_binding_is_immutable_and_versioned() -> None:
    binding = SourceSecurityDomainBinding(
        binding_id=_id(40),
        workspace_id=_id(2),
        environment_id=_id(3),
        source_object_id=_id(4),
        security_domain_reference="legal/eu",
        security_domain_version="security-domain.v1",
        binding_version=1,
        predecessor_binding_id=None,
        created_at=NOW,
        policy_activation_id=_id(41),
    )

    assert binding.security_domain_reference == "legal/eu"
    assert binding.model_config["frozen"] is True

    with pytest.raises(ValidationError):
        SourceSecurityDomainBinding.model_validate(
            {
                **binding.model_dump(),
                "predecessor_binding_id": binding.binding_id,
                "binding_version": 2,
            }
        )


@pytest.mark.parametrize(
    "grant",
    [
        AccessGrant(
            access_grant_id=_id(10),
            principal_kind=PrincipalKind.HUMAN,
            principal_id=_id(11),
            permission=AccessPermission.READ_CONTENT,
        ),
        AccessGrant(
            access_grant_id=_id(12),
            principal_kind=PrincipalKind.TEAM,
            principal_id=_id(13),
            permission=AccessPermission.READ_CONTENT,
        ),
        AccessGrant(
            access_grant_id=_id(14),
            principal_kind=PrincipalKind.SERVICE,
            principal_id=_id(15),
            permission=AccessPermission.PROCESS_CONTENT,
            purpose="answer_question",
            operation="retrieve_context",
            valid_from=NOW,
            valid_until=NOW + timedelta(hours=1),
        ),
    ],
)
def test_policy_accepts_only_allow_grants_for_the_principal_kind(
    grant: AccessGrant,
) -> None:
    assert _policy(grant).grants == (grant,)


@pytest.mark.parametrize(
    "values",
    [
        {
            "principal_kind": PrincipalKind.HUMAN,
            "permission": AccessPermission.PROCESS_CONTENT,
            "purpose": "answer_question",
            "operation": "retrieve_context",
        },
        {
            "principal_kind": PrincipalKind.SERVICE,
            "permission": AccessPermission.READ_CONTENT,
        },
        {
            "principal_kind": PrincipalKind.SERVICE,
            "permission": AccessPermission.PROCESS_CONTENT,
            "purpose": "answer_question",
            "operation": "retrieve_context",
            "valid_from": NOW,
        },
        {
            "principal_kind": PrincipalKind.SERVICE,
            "permission": AccessPermission.PROCESS_CONTENT,
            "purpose": "answer_question",
            "operation": "retrieve_context",
            "valid_from": NOW,
            "valid_until": NOW,
        },
    ],
)
def test_grant_rejects_permission_or_interval_that_could_widen_authority(
    values: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        AccessGrant(
            access_grant_id=_id(20),
            principal_id=_id(21),
            **values,
        )


def test_policy_without_explicit_read_grant_denies_administrator_identity() -> None:
    assert _policy().allows_human_read(_id(6), frozenset()) is False


@pytest.mark.parametrize(
    ("evaluated_at", "allowed"),
    [
        (NOW - timedelta(microseconds=1), False),
        (NOW, True),
        (NOW + timedelta(hours=1) - timedelta(microseconds=1), True),
        (NOW + timedelta(hours=1), False),
    ],
)
def test_service_grant_enforces_half_open_validity_interval(
    evaluated_at: datetime, allowed: bool
) -> None:
    policy = _policy(
        AccessGrant(
            access_grant_id=_id(30),
            principal_kind=PrincipalKind.SERVICE,
            principal_id=_id(31),
            permission=AccessPermission.PROCESS_CONTENT,
            purpose="answer_question",
            operation="retrieve_context",
            valid_from=NOW,
            valid_until=NOW + timedelta(hours=1),
        )
    )

    assert policy.allows_service_processing(
        _id(31), "answer_question", "retrieve_context", evaluated_at
    ) is allowed
