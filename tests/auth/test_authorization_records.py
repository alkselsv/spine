from __future__ import annotations

from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.auth import AuthenticationAlias, AuthorizationRole
from spine.auth.records import (
    AuthenticationAliasBinding,
    AuthorizationRecordProvenance,
    AuthorizationRecordStatus,
    CanonicalHumanIdentity,
    EnvironmentRoleBinding,
)


PROVENANCE = AuthorizationRecordProvenance(
    change_id=UUID("90000000-0000-0000-0000-000000000001"),
    recorded_by="synthetic-test-bootstrap",
)


def test_authorization_records_are_frozen_and_reject_extra_claims() -> None:
    identity = CanonicalHumanIdentity(
        identity_id=UUID("10000000-0000-0000-0000-000000000001"),
        version=1,
        status=AuthorizationRecordStatus.ACTIVE,
        provenance=PROVENANCE,
    )

    with pytest.raises(ValidationError):
        identity.status = AuthorizationRecordStatus.DISABLED  # type: ignore[misc]

    with pytest.raises(ValidationError):
        AuthenticationAliasBinding(
            binding_id=UUID("11000000-0000-0000-0000-000000000001"),
            alias=AuthenticationAlias(issuer="https://id.test", subject="subject"),
            canonical_human_identity_id=identity.identity_id,
            version=1,
            status=AuthorizationRecordStatus.ACTIVE,
            provenance=PROVENANCE,
            claims={"groups": ["administrator"]},
        )


def test_role_binding_accepts_only_exact_r1_administrator_role() -> None:
    values = {
        "binding_id": UUID("14000000-0000-0000-0000-000000000001"),
        "workspace_id": UUID("20000000-0000-0000-0000-000000000001"),
        "environment_id": UUID("30000000-0000-0000-0000-000000000001"),
        "canonical_human_identity_id": UUID(
            "10000000-0000-0000-0000-000000000001"
        ),
        "version": 1,
        "status": AuthorizationRecordStatus.ACTIVE,
        "provenance": PROVENANCE,
    }

    assert EnvironmentRoleBinding(
        **values, role=AuthorizationRole.ADMINISTRATOR
    ).role is AuthorizationRole.ADMINISTRATOR

    with pytest.raises(ValidationError):
        EnvironmentRoleBinding(**values, role="owner")
