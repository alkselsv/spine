from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from spine.application.access_policies import (
    ContentAuthorizationContext,
    ContentAuthorizationService,
)
from spine.domain.sources.access_policy import (
    AccessGrant,
    AccessPermission,
    AccessPolicyVersion,
    CurrentAccessPolicy,
    PolicyOriginKind,
    PrincipalKind,
)
from spine.infrastructure.persistence.access_policies import (
    InMemoryAccessPolicyRepository,
)


NOW = datetime(2026, 10, 12, tzinfo=timezone.utc)


def _id(value: int) -> UUID:
    return UUID(int=value)


def _grant(
    grant_id: int,
    kind: PrincipalKind,
    principal_id: int,
    permission: AccessPermission,
    *,
    valid_from: datetime | None = None,
    valid_until: datetime | None = None,
) -> AccessGrant:
    return AccessGrant(
        access_grant_id=_id(grant_id),
        principal_kind=kind,
        principal_id=_id(principal_id),
        permission=permission,
        purpose=("answer_question" if kind is PrincipalKind.SERVICE else None),
        operation=("retrieve_context" if kind is PrincipalKind.SERVICE else None),
        valid_from=valid_from,
        valid_until=valid_until,
    )


def _policy(*grants: AccessGrant) -> AccessPolicyVersion:
    return AccessPolicyVersion(
        access_policy_version_id=_id(10),
        workspace_id=_id(2),
        environment_id=_id(3),
        source_object_id=_id(4),
        version=1,
        predecessor_policy_version_id=None,
        document_schema_version="access-policy.v1",
        document_hash="a" * 64,
        origin_kind=PolicyOriginKind.ELIGIBLE_TEMPLATE,
        origin_reference=_id(5),
        origin_version=1,
        origin_hash="b" * 64,
        created_by_canonical_human_id=_id(6),
        created_at=NOW,
        grants=grants,
    )


def _current(policy: AccessPolicyVersion) -> CurrentAccessPolicy:
    return CurrentAccessPolicy(
        workspace_id=policy.workspace_id,
        environment_id=policy.environment_id,
        source_object_id=policy.source_object_id,
        access_policy_version_id=policy.access_policy_version_id,
        policy_activation_id=_id(11),
        policy_generation=7,
        cas_version=7,
    )


def _context(*, human_id: int | None = 20, service_id: int | None = None) -> ContentAuthorizationContext:
    return ContentAuthorizationContext(
        workspace_id=_id(2),
        environment_id=_id(3),
        canonical_human_id=(_id(human_id) if human_id is not None else None),
        effective_team_ids=frozenset({_id(21)}),
        membership_generation=5,
        service_principal_id=(_id(service_id) if service_id is not None else None),
        service_authority_generation=9,
        purpose="answer_question" if service_id is not None else None,
        operation="retrieve_context" if service_id is not None else None,
        server_owned_operation=human_id is None,
        authority_snapshot_reference=_id(22),
    )


async def _service(*grants: AccessGrant) -> ContentAuthorizationService:
    repository = InMemoryAccessPolicyRepository()
    policy = _policy(*grants)
    await repository.seed_effective_policy(policy, _current(policy))
    return ContentAuthorizationService(repository)


@pytest.mark.asyncio
async def test_human_or_current_team_read_grant_allows_disclosure() -> None:
    service = await _service(
        _grant(30, PrincipalKind.TEAM, 21, AccessPermission.READ_CONTENT)
    )

    decision = await service.decide_human_disclosure(_context(), (_id(4),), NOW)

    assert decision.allowed is True
    assert decision.policy_generations == {_id(4): 7}


@pytest.mark.asyncio
async def test_administrator_context_without_read_grant_denies_without_source_detail() -> None:
    service = await _service()

    decision = await service.decide_human_disclosure(_context(), (_id(4),), NOW)

    assert decision.allowed is False
    assert decision.reason_code == "content_authority_denied"
    assert decision.policy_version_ids == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("clock", "allowed"),
    [
        (NOW - timedelta(microseconds=1), False),
        (NOW, True),
        (NOW + timedelta(hours=1) - timedelta(microseconds=1), True),
        (NOW + timedelta(hours=1), False),
    ],
)
async def test_service_validity_interval_is_half_open(
    clock: datetime, allowed: bool
) -> None:
    service = await _service(
        _grant(
            31,
            PrincipalKind.SERVICE,
            40,
            AccessPermission.PROCESS_CONTENT,
            valid_from=NOW,
            valid_until=NOW + timedelta(hours=1),
        )
    )

    decision = await service.decide_noninteractive_processing(
        _context(human_id=None, service_id=40), (_id(4),), clock
    )

    assert decision.allowed is allowed


@pytest.mark.asyncio
async def test_interactive_processing_requires_human_and_service_intersection() -> None:
    service = await _service(
        _grant(32, PrincipalKind.HUMAN, 20, AccessPermission.READ_CONTENT)
    )

    decision = await service.decide_interactive_processing(
        _context(service_id=40), (_id(4),), NOW
    )

    assert decision.allowed is False


@pytest.mark.asyncio
async def test_noninteractive_processing_requires_server_owned_operation() -> None:
    service = await _service(
        _grant(33, PrincipalKind.SERVICE, 40, AccessPermission.PROCESS_CONTENT)
    )
    context = _context(human_id=None, service_id=40).model_copy(
        update={"server_owned_operation": False}
    )

    decision = await service.decide_noninteractive_processing(
        context, (_id(4),), NOW
    )

    assert decision.allowed is False


@pytest.mark.asyncio
async def test_multi_source_decision_fails_closed_when_any_policy_is_missing() -> None:
    service = await _service(
        _grant(34, PrincipalKind.HUMAN, 20, AccessPermission.READ_CONTENT)
    )

    decision = await service.decide_human_disclosure(
        _context(), (_id(4), _id(404)), NOW
    )

    assert decision.allowed is False
    assert decision.policy_version_ids == {}
