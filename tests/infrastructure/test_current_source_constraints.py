from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from spine.domain.sources.retention import (
    RetentionApplicability,
    RetentionArtifactClass,
    RetentionAuthorityKind,
    RetentionTargetKind,
)
from spine.infrastructure.current_source_constraints import current_retention_allows

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
WORKSPACE = UUID("10000000-0000-4000-8000-000000000001")
ENVIRONMENT = UUID("20000000-0000-4000-8000-000000000001")
SOURCE = UUID("30000000-0000-4000-8000-000000000001")
REVISION = UUID("40000000-0000-4000-8000-000000000001")


def _id(value: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"current-source-constraints:{value}")


def _binding(target_kind: RetentionTargetKind) -> RetentionApplicability:
    target = (
        {"source_object_id": SOURCE}
        if target_kind is RetentionTargetKind.EXACT_SOURCE
        else {
            "source_class": "document",
            "source_class_reference": _id("document-class"),
            "source_class_version_id": _id("document-class:v1"),
            "source_class_digest": "d" * 64,
        }
    )
    suffix = target_kind.value
    return RetentionApplicability(
        applicability_id=_id(f"applicability:{suffix}"),
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        target_kind=target_kind,
        artifact_classes=(RetentionArtifactClass.PROJECTION,),
        retention_policy_id=_id(f"policy:{suffix}"),
        retention_policy_version_id=_id(f"policy-version:{suffix}"),
        retention_policy_digest="a" * 64,
        applicability_evidence_reference=_id(f"evidence:{suffix}"),
        applicability_evidence_digest="b" * 64,
        approving_authority_reference=_id(f"authority:{suffix}"),
        approving_authority_version_id=_id(f"authority-version:{suffix}"),
        approving_authority_digest="c" * 64,
        approving_authority_kind=RetentionAuthorityKind.LEGAL_COMPLIANCE,
        effective_from=NOW - timedelta(days=1),
        expected_current_applicability_id=None,
        expected_current_generation=0,
        generation=1,
        actor_id=_id(f"actor:{suffix}"),
        audit_event_id=_id(f"audit:{suffix}"),
        outbox_event_id=_id(f"outbox:{suffix}"),
        approved_at=NOW,
        **target,
    )


class _Result:
    def __init__(self, *, scalar=None, rows=()) -> None:
        self.scalar = scalar
        self.rows = rows

    def scalar_one_or_none(self):
        return self.scalar

    def scalars(self):
        return self

    def all(self):
        return list(self.rows)


class _UnitOfWork:
    def __init__(self, bindings: tuple[RetentionApplicability, ...]) -> None:
        self.bindings = bindings

    async def _execute(self, statement, parameters=None):
        sql = str(statement)
        if "pg_advisory_xact_lock_shared" in sql:
            assert parameters is not None
            return _Result()
        if "source_objects JOIN" in sql:
            return _Result(scalar="document")
        return _Result(
            rows=tuple(binding.model_dump(mode="json") for binding in self.bindings)
        )


@pytest.mark.asyncio
async def test_retention_rejects_exact_and_source_class_current_ambiguity() -> None:
    exact = _binding(RetentionTargetKind.EXACT_SOURCE)
    source_class = _binding(RetentionTargetKind.SOURCE_CLASS)

    allowed = await current_retention_allows(
        _UnitOfWork((exact, source_class)),
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        source_object_id=SOURCE,
        source_revision_id=REVISION,
        artifact_class=RetentionArtifactClass.PROJECTION,
        evaluated_at=NOW,
        expected_applicability_id=exact.applicability_id,
        expected_generation=exact.generation,
    )

    assert allowed is False


@pytest.mark.asyncio
async def test_retention_rejects_ambiguity_before_temporal_eligibility() -> None:
    exact = _binding(RetentionTargetKind.EXACT_SOURCE)
    expired_class = _binding(RetentionTargetKind.SOURCE_CLASS).model_copy(
        update={"effective_until": NOW - timedelta(hours=1)}
    )

    allowed = await current_retention_allows(
        _UnitOfWork((exact, expired_class)),
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        source_object_id=SOURCE,
        source_revision_id=REVISION,
        artifact_class=RetentionArtifactClass.PROJECTION,
        evaluated_at=NOW,
        expected_applicability_id=exact.applicability_id,
        expected_generation=exact.generation,
    )

    assert allowed is False


@pytest.mark.asyncio
async def test_retention_accepts_one_current_binding_matching_pinned_authority() -> (
    None
):
    exact = _binding(RetentionTargetKind.EXACT_SOURCE)

    allowed = await current_retention_allows(
        _UnitOfWork((exact,)),
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        source_object_id=SOURCE,
        source_revision_id=REVISION,
        artifact_class=RetentionArtifactClass.PROJECTION,
        evaluated_at=NOW,
        expected_applicability_id=exact.applicability_id,
        expected_generation=exact.generation,
    )

    assert allowed is True
