from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from spine.application.processing_disposition import (
    OrdinaryProcessingEligibility,
    OrdinaryProcessingEligibilityReason,
)
from spine.application.source_usage_guard import (
    SourceUsageArtifact,
    SourceUsageDependency,
    SourceUsageKind,
    SourceUsageReason,
    SourceUsageRequest,
)
from spine.domain.sources import RetentionArtifactClass
from spine.infrastructure.source_usage_guard import (
    TransactionCurrentSourceUsageAuthority,
)

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
WORKSPACE = UUID("10000000-0000-4000-8000-000000000001")
ENVIRONMENT = UUID("20000000-0000-4000-8000-000000000001")
OTHER_WORKSPACE = UUID("10000000-0000-4000-8000-000000000002")


def _id(value: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"transaction-source-usage:{value}")


def _request() -> SourceUsageRequest:
    return SourceUsageRequest(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        usage=SourceUsageKind.PROJECTION_ACTIVATION,
        artifact=SourceUsageArtifact(
            artifact_class=RetentionArtifactClass.PROJECTION,
            artifact_id=_id("snapshot"),
            artifact_version=3,
            artifact_digest="a" * 64,
        ),
        dependencies=tuple(
            SourceUsageDependency(
                source_object_id=_id(f"source:{value}"),
                source_revision_id=_id(f"revision:{value}"),
                dependency_id=_id(f"dependency:{value}"),
                artifact_member_id=_id(f"member:{value}"),
            )
            for value in ("a", "b")
        ),
        evaluated_at=NOW,
    )


class _Processing:
    async def resolve(self, **values):
        suffix = values["source_object_id"].hex
        return OrdinaryProcessingEligibility(
            eligible=True,
            reason=OrdinaryProcessingEligibilityReason.ELIGIBLE,
            lifecycle_eligibility_reference=uuid5(NAMESPACE_URL, f"lifecycle:{suffix}"),
            lifecycle_eligibility_generation=4,
            retention_applicability_reference=uuid5(
                NAMESPACE_URL, f"retention:{suffix}"
            ),
            retention_applicability_generation=5,
        )


@pytest.fixture(autouse=True)
def _allow_currentness_lock(monkeypatch):
    async def allowed(*args, **kwargs):
        return True

    monkeypatch.setattr(
        "spine.infrastructure.source_usage_guard.lock_source_usage_currentness",
        allowed,
    )


@pytest.mark.asyncio
async def test_transaction_guard_binds_exact_current_authority_for_every_source(
    monkeypatch,
) -> None:
    lifecycle_calls = []
    retention_calls = []
    hold_calls = []

    monkeypatch.setattr(
        "spine.infrastructure.source_usage_guard.CanonicalCurrentProcessingDispositionAuthority",
        lambda _canonical: _Processing(),
    )

    async def lifecycle(_uow, **values):
        lifecycle_calls.append(values)
        return True

    async def retention(_uow, **values):
        retention_calls.append(values)
        return True

    async def holds(_uow, **values):
        hold_calls.append(values)
        return True

    monkeypatch.setattr(
        "spine.infrastructure.source_usage_guard.current_lifecycle_allows", lifecycle
    )
    monkeypatch.setattr(
        "spine.infrastructure.source_usage_guard.current_retention_allows", retention
    )
    monkeypatch.setattr(
        "spine.infrastructure.source_usage_guard.projection_hold_allows", holds
    )
    request = _request()

    decision = await TransactionCurrentSourceUsageAuthority(
        SimpleNamespace(canonical=object(), _execute=object())
    ).evaluate(request)

    assert decision.allowed is True
    assert decision.checked_dependencies == request.dependencies
    assert len(lifecycle_calls) == len(retention_calls) == len(hold_calls) == 2
    assert all(call["expected_generation"] == 4 for call in lifecycle_calls)
    assert all(call["expected_generation"] == 5 for call in retention_calls)
    assert [call["artifact_id"] for call in hold_calls] == [
        dependency.artifact_member_id for dependency in request.dependencies
    ]
    assert all(
        call["prohibited_effects"] == ("model_use", "derived_artifact_disclosure")
        for call in hold_calls
    )


@pytest.mark.asyncio
async def test_transaction_guard_suppresses_whole_mixed_artifact_on_one_denial(
    monkeypatch,
) -> None:
    request = _request()
    denied_source = request.dependencies[1].source_object_id
    monkeypatch.setattr(
        "spine.infrastructure.source_usage_guard.CanonicalCurrentProcessingDispositionAuthority",
        lambda _canonical: _Processing(),
    )

    async def lifecycle(_uow, **values):
        return values["source_object_id"] != denied_source

    async def allowed(*args, **kwargs):
        return True

    monkeypatch.setattr(
        "spine.infrastructure.source_usage_guard.current_lifecycle_allows", lifecycle
    )
    monkeypatch.setattr(
        "spine.infrastructure.source_usage_guard.current_retention_allows", allowed
    )
    monkeypatch.setattr(
        "spine.infrastructure.source_usage_guard.projection_hold_allows", allowed
    )

    decision = await TransactionCurrentSourceUsageAuthority(
        SimpleNamespace(canonical=object(), _execute=object())
    ).evaluate(request)

    assert decision.allowed is False
    assert decision.reason is SourceUsageReason.LIFECYCLE_INELIGIBLE
    assert decision.checked_dependencies == request.dependencies[:1]


@pytest.mark.asyncio
async def test_transaction_guard_denies_a_different_workspace_authority(
    monkeypatch,
) -> None:
    class WorkspaceProcessing(_Processing):
        async def resolve(self, **values):
            if values["workspace_id"] != WORKSPACE:
                return OrdinaryProcessingEligibility(
                    eligible=False,
                    reason=OrdinaryProcessingEligibilityReason.CURRENT_REVISION_MISMATCH,
                )
            return await super().resolve(**values)

    monkeypatch.setattr(
        "spine.infrastructure.source_usage_guard.CanonicalCurrentProcessingDispositionAuthority",
        lambda _canonical: WorkspaceProcessing(),
    )
    request = _request().model_copy(update={"workspace_id": OTHER_WORKSPACE})

    decision = await TransactionCurrentSourceUsageAuthority(
        SimpleNamespace(canonical=object(), _execute=object())
    ).evaluate(request)

    assert decision.allowed is False
    assert decision.reason is SourceUsageReason.PROCESSING_INELIGIBLE
    assert decision.checked_dependencies == ()


@pytest.mark.parametrize(
    ("usage", "expected_effects"),
    (
        (SourceUsageKind.RETRIEVAL, ("retrieval", "model_use")),
        (
            SourceUsageKind.FINAL_DISCLOSURE,
            ("final_disclosure", "derived_artifact_disclosure"),
        ),
    ),
)
def test_transaction_guard_maps_usage_to_exact_hold_effects(
    usage: SourceUsageKind,
    expected_effects: tuple[str, ...],
) -> None:
    assert (
        TransactionCurrentSourceUsageAuthority._hold_effects(usage) == expected_effects
    )
