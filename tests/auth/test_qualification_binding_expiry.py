from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from uuid import UUID

from spine.auth import (
    AuthorizationDeniedError,
    InMemoryAuthorizationDirectory,
    InMemoryServiceAuthorizationDirectory,
    QualificationAuthorityBinding,
    QualificationPrincipalKind,
)
from spine.auth.records import AuthorizationRecordStatus
from tests.contracts.auth.test_authorization_directory_contract import (
    authenticated_alias,
    qualification_binding,
    records,
    route_policy,
    scope,
)


NOW = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "deadline",
    (NOW, NOW - timedelta(microseconds=1)),
    ids=("at-deadline", "after-deadline"),
)
async def test_human_authority_denied_at_or_after_deadline_without_cleanup(
    deadline: datetime,
) -> None:
    directory = InMemoryAuthorizationDirectory(
        records=records(
            qualification_bindings=(qualification_binding(expires_at=deadline),)
        ),
        clock=lambda: NOW,
    )

    with pytest.raises(AuthorizationDeniedError):
        await directory.resolve_request_authority(
            authenticated_alias(), scope(), route_policy()
        )


@pytest.mark.asyncio
async def test_human_authority_allowed_immediately_before_deadline() -> None:
    directory = InMemoryAuthorizationDirectory(
        records=records(
            qualification_bindings=(qualification_binding(expires_at=NOW),)
        ),
        clock=lambda: NOW - timedelta(microseconds=1),
    )

    result = await directory.resolve_request_authority(
        authenticated_alias(), scope(), route_policy()
    )

    assert result.authorization_generation == 7


@pytest.mark.asyncio
async def test_revoked_human_binding_denies_before_deadline() -> None:
    directory = InMemoryAuthorizationDirectory(
        records=records(
            qualification_bindings=(
                qualification_binding(
                    expires_at=NOW + timedelta(hours=1),
                    status=AuthorizationRecordStatus.REVOKED,
                ),
            )
        ),
        clock=lambda: NOW,
    )

    with pytest.raises(AuthorizationDeniedError):
        await directory.resolve_request_authority(
            authenticated_alias(), scope(), route_policy()
        )


def test_binding_successor_cannot_extend_deadline() -> None:
    original = qualification_binding(expires_at=NOW)
    extended = qualification_binding(expires_at=NOW + timedelta(hours=1), version=2)

    with pytest.raises(ValueError, match="deadline are immutable"):
        InMemoryAuthorizationDirectory(
            records=records(qualification_bindings=(original, extended))
        )


def service_binding(
    *,
    expires_at: datetime,
    version: int = 1,
    status: AuthorizationRecordStatus = AuthorizationRecordStatus.ACTIVE,
) -> QualificationAuthorityBinding:
    human = qualification_binding(expires_at=expires_at)
    return human.model_copy(
        update={
            "binding_id": UUID("15000000-0000-0000-0000-000000000002"),
            "principal_kind": QualificationPrincipalKind.SERVICE,
            "principal_id": UUID("17000000-0000-0000-0000-000000000001"),
            "purpose": "answer_question",
            "operation": "retrieve_context",
            "version": version,
            "status": status,
        }
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("now", "allowed"),
    (
        (NOW - timedelta(microseconds=1), True),
        (NOW, False),
        (NOW + timedelta(microseconds=1), False),
    ),
    ids=("immediately-before", "at-deadline", "after-deadline"),
)
async def test_service_authority_uses_hard_deadline_without_cleanup(
    now: datetime,
    allowed: bool,
) -> None:
    directory = InMemoryServiceAuthorizationDirectory(
        records(
            qualification_bindings=(service_binding(expires_at=NOW),)
        ),
        clock=lambda: now,
    )
    request = {
        "service_principal_id": UUID("17000000-0000-0000-0000-000000000001"),
        "requested_scope": scope(),
        "purpose": "answer_question",
        "operation": "retrieve_context",
        "expected_generation": 7,
    }

    if allowed:
        assert await directory.resolve_service_authority(**request) == 7
    else:
        with pytest.raises(AuthorizationDeniedError):
            await directory.resolve_service_authority(**request)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("binding_status", "expected_generation"),
    (
        (AuthorizationRecordStatus.REVOKED, 7),
        (AuthorizationRecordStatus.ACTIVE, 6),
    ),
    ids=("revoked", "stale-generation"),
)
async def test_service_authority_denies_revocation_and_stale_generation(
    binding_status: AuthorizationRecordStatus,
    expected_generation: int,
) -> None:
    directory = InMemoryServiceAuthorizationDirectory(
        records(
            qualification_bindings=(
                service_binding(
                    expires_at=NOW + timedelta(hours=1), status=binding_status
                ),
            )
        ),
        clock=lambda: NOW,
    )

    with pytest.raises(AuthorizationDeniedError):
        await directory.resolve_service_authority(
            service_principal_id=UUID("17000000-0000-0000-0000-000000000001"),
            requested_scope=scope(),
            purpose="answer_question",
            operation="retrieve_context",
            expected_generation=expected_generation,
        )
