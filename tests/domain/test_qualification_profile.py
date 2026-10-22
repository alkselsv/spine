from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.auth import (
    AuthorizationRecordStatus,
    QualificationAuthorityBinding,
    QualificationPrincipalKind,
)
from spine.auth.records import AuthorizationRecordProvenance
from spine.domain.qualification import (
    QualificationOpaqueReference,
    QualificationOperation,
    QualificationProfile,
)


NOW = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)
WORKSPACE_ID = UUID("20000000-0000-0000-0000-000000000001")
ENVIRONMENT_ID = UUID("30000000-0000-0000-0000-000000000001")
QUALIFICATION_ID = UUID("40000000-0000-0000-0000-000000000001")
BINDING_ID = UUID("50000000-0000-0000-0000-000000000001")
PROFILE_ID = UUID("60000000-0000-0000-0000-000000000001")
CHANGE_ID = UUID("70000000-0000-0000-0000-000000000001")
PRINCIPAL_ID = UUID("80000000-0000-0000-0000-000000000001")


def binding(*, status: AuthorizationRecordStatus = AuthorizationRecordStatus.DISABLED):
    return QualificationAuthorityBinding(
        binding_id=BINDING_ID,
        qualification_id=QUALIFICATION_ID,
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        principal_kind=QualificationPrincipalKind.HUMAN,
        principal_id=PRINCIPAL_ID,
        expires_at=NOW + timedelta(hours=1),
        version=1,
        status=status,
        provenance=AuthorizationRecordProvenance(
            change_id=CHANGE_ID,
            recorded_by="qualification-profile-test",
        ),
    )


def profile(**overrides: object):
    values: dict[str, object] = {
        "profile_id": PROFILE_ID,
        "profile_version": 1,
        "qualification_id": QUALIFICATION_ID,
        "workspace_id": WORKSPACE_ID,
        "environment_id": ENVIRONMENT_ID,
        "target_reference": QualificationOpaqueReference(value="target:source"),
        "binding": binding(),
        "allowed_operations": frozenset(
            {
                QualificationOperation.PROVISION,
                QualificationOperation.REVOKE,
                QualificationOperation.CLEANUP,
            }
        ),
        "secret_references": (QualificationOpaqueReference(value="secret:source"),),
        "database_role_references": (
            QualificationOpaqueReference(value="role:runtime"),
        ),
        "access_policy_references": (
            QualificationOpaqueReference(value="policy:approved"),
        ),
        "approved": True,
    }
    values.update(overrides)
    return QualificationProfile.create(**values)


def test_approved_profile_has_stable_digest_and_starts_inactive() -> None:
    current = profile()

    assert current.profile_digest == QualificationProfile.calculate_digest(current)
    assert current.binding.status is AuthorizationRecordStatus.DISABLED


def test_profile_rejects_active_binding_plan() -> None:
    with pytest.raises(ValidationError, match="must start inactive"):
        profile(binding=binding(status=AuthorizationRecordStatus.ACTIVE))


def test_profile_rejects_inline_secret_and_request_selector_fields() -> None:
    with pytest.raises(ValidationError):
        QualificationOpaqueReference(value="secret-value=not-a-reference")
    with pytest.raises(ValidationError):
        QualificationOpaqueReference(value="raw-secret-value")

    with pytest.raises(ValidationError):
        QualificationProfile.create(**{
            **profile().model_dump(mode="python"),
            "workspace_selector": "request-controlled",
        })
