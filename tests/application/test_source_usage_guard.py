from __future__ import annotations

from datetime import datetime, timezone
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from spine.application.source_usage_guard import (
    CurrentSourceUsageDecision,
    CurrentSourceUsageGuard,
    SourceUsageArtifact,
    SourceUsageDependency,
    SourceUsageKind,
    SourceUsageReason,
    SourceUsageRequest,
)
from spine.domain.sources.retention import RetentionArtifactClass

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
WORKSPACE = UUID("10000000-0000-4000-8000-000000000001")
ENVIRONMENT = UUID("20000000-0000-4000-8000-000000000001")


def _id(value: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"source-usage-guard:{value}")


def _request(*, sources: tuple[str, ...] = ("a", "b")) -> SourceUsageRequest:
    return SourceUsageRequest(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        usage=SourceUsageKind.RETRIEVAL,
        artifact=SourceUsageArtifact(
            artifact_class=RetentionArtifactClass.PROJECTION,
            artifact_id=_id("snapshot"),
            artifact_version=3,
            artifact_digest="a" * 64,
        ),
        dependencies=tuple(
            SourceUsageDependency(
                source_object_id=_id(f"source:{name}"),
                source_revision_id=_id(f"revision:{name}"),
                dependency_id=_id(f"dependency:{name}"),
            )
            for name in sources
        ),
        evaluated_at=NOW,
    )


def test_request_rejects_duplicate_dependencies() -> None:
    request = _request(sources=("a",))
    dependency = request.dependencies[0]

    with pytest.raises(ValueError, match="unique"):
        request.model_copy(
            update={"dependencies": (dependency, dependency)}
        ).model_validate(
            request.model_copy(
                update={"dependencies": (dependency, dependency)}
            ).model_dump()
        )


def test_decision_cannot_claim_eligible_without_every_exact_dependency() -> None:
    request = _request()

    with pytest.raises(ValueError, match="complete"):
        CurrentSourceUsageDecision(
            allowed=True,
            reason=SourceUsageReason.ALLOWED,
            required_dependency_count=len(request.dependencies),
            checked_dependencies=request.dependencies[:1],
        )


@pytest.mark.asyncio
async def test_guard_missing_authority_fails_closed_without_content() -> None:
    guard = CurrentSourceUsageGuard(authority=None)

    decision = await guard.evaluate(_request())

    assert decision == CurrentSourceUsageDecision(
        allowed=False,
        reason=SourceUsageReason.AUTHORITY_UNAVAILABLE,
        required_dependency_count=len(_request().dependencies),
        checked_dependencies=(),
    )
    assert "source" not in decision.model_dump_json()


@pytest.mark.asyncio
async def test_guard_rejects_partial_mixed_artifact_authority() -> None:
    request = _request()

    class PartialAuthority:
        async def evaluate(self, received):
            assert received == request
            return CurrentSourceUsageDecision(
                allowed=False,
                reason=SourceUsageReason.RETENTION_INELIGIBLE,
                required_dependency_count=len(request.dependencies),
                checked_dependencies=request.dependencies[:1],
            )

    decision = await CurrentSourceUsageGuard(authority=PartialAuthority()).evaluate(
        request
    )

    assert decision.allowed is False
    assert decision.reason is SourceUsageReason.RETENTION_INELIGIBLE


@pytest.mark.asyncio
async def test_guard_accepts_only_complete_exact_authority_result() -> None:
    request = _request()

    class CompleteAuthority:
        async def evaluate(self, received):
            return CurrentSourceUsageDecision(
                allowed=True,
                reason=SourceUsageReason.ALLOWED,
                required_dependency_count=len(received.dependencies),
                checked_dependencies=received.dependencies,
            )

    decision = await CurrentSourceUsageGuard(authority=CompleteAuthority()).evaluate(
        request
    )

    assert decision.allowed is True
    assert decision.checked_dependencies == request.dependencies


@pytest.mark.asyncio
async def test_guard_rejects_substituted_dependency_identity() -> None:
    request = _request()
    substituted = request.dependencies[0].model_copy(
        update={"dependency_id": _id("substituted-dependency")}
    )

    class SubstitutingAuthority:
        async def evaluate(self, received):
            return CurrentSourceUsageDecision(
                allowed=True,
                reason=SourceUsageReason.ALLOWED,
                required_dependency_count=len(received.dependencies),
                checked_dependencies=(substituted, received.dependencies[1]),
            )

    decision = await CurrentSourceUsageGuard(
        authority=SubstitutingAuthority()
    ).evaluate(request)

    assert decision.allowed is False
    assert decision.reason is SourceUsageReason.AUTHORITY_UNAVAILABLE
