from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import cast
from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
    TrustedContextProvenance,
    TrustedPersistenceContext,
)
from spine.application.persistence.unit_of_work import UnitOfWork
from spine.application.retention import (
    ApprovedRetentionApplicabilityAuthority,
    RetentionArtifactRef,
    RetentionApplicabilityError,
    RetentionEligibility,
    RetentionEligibilityQuery,
    RetentionEligibilityReader,
    RetentionEligibilityStatus,
    RetentionSourceDecision,
    RetentionSourceLineage,
)
from spine.domain.sources.retention import (
    RetentionApplicability,
    RetentionArtifactClass,
    RetentionAuthorityKind,
    RetentionTargetKind,
)
from spine.domain.common import ContextOrigin


NOW = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)


def _uuid(value: int) -> UUID:
    return UUID(int=value)


def _binding(
    *,
    applicability_id: int,
    source_object_id: UUID | None,
    source_class: str | None = None,
    policy: int = 50,
    effective_from: datetime = NOW - timedelta(days=1),
    effective_until: datetime | None = NOW + timedelta(days=1),
) -> RetentionApplicability:
    class_binding = source_object_id is None
    return RetentionApplicability(
        applicability_id=_uuid(applicability_id),
        workspace_id=_uuid(1),
        environment_id=_uuid(2),
        target_kind=(
            RetentionTargetKind.SOURCE_CLASS
            if class_binding
            else RetentionTargetKind.EXACT_SOURCE
        ),
        source_object_id=source_object_id,
        source_class=source_class if class_binding else None,
        source_class_reference=_uuid(applicability_id + 100) if class_binding else None,
        source_class_version_id=_uuid(applicability_id + 200) if class_binding else None,
        source_class_digest="d" * 64 if class_binding else None,
        artifact_classes=(
            RetentionArtifactClass.ORIGINAL,
            RetentionArtifactClass.PARSED_CONTENT,
            RetentionArtifactClass.TRANSCRIPT,
            RetentionArtifactClass.PROJECTION,
            RetentionArtifactClass.CACHED_ANSWER,
            RetentionArtifactClass.CITATION,
        ),
        retention_policy_id=_uuid(policy),
        retention_policy_version_id=_uuid(policy + 1),
        retention_policy_digest=f"{policy % 16:x}" * 64,
        applicability_evidence_reference=_uuid(applicability_id + 1),
        applicability_evidence_digest="a" * 64,
        approving_authority_reference=_uuid(applicability_id + 2),
        approving_authority_version_id=_uuid(applicability_id + 3),
        approving_authority_digest="b" * 64,
        approving_authority_kind=RetentionAuthorityKind.DATA_PORTAL,
        effective_from=effective_from,
        effective_until=effective_until,
        exception_references=(),
        legal_hold_references=(_uuid(applicability_id + 4),),
        expected_current_applicability_id=None,
        expected_current_generation=0,
        generation=1,
        actor_id=_uuid(90),
        audit_event_id=_uuid(applicability_id + 5),
        outbox_event_id=_uuid(applicability_id + 6),
        approved_at=NOW - timedelta(days=2),
    )


class _Candidates:
    def __init__(self, values: dict[UUID, tuple[RetentionApplicability, ...]]) -> None:
        self.values = values
        self.calls: list[RetentionSourceLineage] = []

    async def list_current_applicabilities(self, source, context):
        self.calls.append(source)
        return self.values.get(source.source_object_id, ())


class _Lineages:
    def __init__(self, values: tuple[RetentionSourceLineage, ...]) -> None:
        self.values = values
        self.calls: list[RetentionArtifactRef] = []

    async def resolve_artifact_lineage(self, artifact, context):
        self.calls.append(artifact)
        return self.values


class _Authority:
    def __init__(self, current: bool = True) -> None:
        self.current = current

    async def verify(self, applicability, context, transaction) -> None:
        if not self.current:
            raise RuntimeError

    async def is_current(self, applicability, context) -> bool:
        return self.current


class _PolicyApprovals:
    async def policy_is_current(self, applicability, context, transaction) -> bool:
        return applicability.retention_policy_digest == "2" * 64


class _BindingApprovals:
    async def binding_is_approved(self, applicability, context, transaction) -> bool:
        return applicability.approving_authority_digest == "b" * 64


class _UnavailablePolicyApprovals:
    async def policy_is_current(self, applicability, context, transaction) -> bool:
        raise RuntimeError("private approval adapter detail")


def _context(workspace_id: UUID = _uuid(1), environment_id: UUID = _uuid(2)):
    return TrustedPersistenceContext(
        scope=EnvironmentScope(
            workspace_id=workspace_id,
            environment_id=environment_id,
        ),
        origin=ContextOrigin.INTERACTIVE,
        purpose=PersistencePurpose("retention_governance"),
        operation=PersistenceOperation("retention_eligibility"),
        trace_id=_uuid(900),
        acting_subject_id=_uuid(90),
        service_principal_id=None,
        provenance=TrustedContextProvenance(_uuid(901), b"verified-context"),
    )


def _source(value: int, source_class: str = "document") -> RetentionSourceLineage:
    return RetentionSourceLineage(
        source_object_id=_uuid(value),
        source_revision_id=_uuid(value + 1000),
        source_class=source_class,
    )


def _query() -> RetentionEligibilityQuery:
    return RetentionEligibilityQuery(
        artifact=RetentionArtifactRef(
            workspace_id=_uuid(1),
            environment_id=_uuid(2),
            artifact_class=RetentionArtifactClass.PARSED_CONTENT,
            artifact_id=_uuid(900),
            artifact_version=1,
            artifact_digest="f" * 64,
        )
    )


def _reader(candidates: _Candidates, *sources: RetentionSourceLineage, authority=None):
    return RetentionEligibilityReader(
        candidates=candidates,
        lineages=_Lineages(sources),
        authority=authority or _Authority(),
        clock=lambda: NOW,
    )


@pytest.mark.asyncio
async def test_exact_current_binding_makes_source_artifact_eligible() -> None:
    source = _source(10)
    candidates = _Candidates(
        {source.source_object_id: (_binding(applicability_id=100, source_object_id=source.source_object_id),)}
    )
    verifier = ApprovedRetentionApplicabilityAuthority(
        policies=_PolicyApprovals(), bindings=_BindingApprovals()
    )
    reader = _reader(candidates, source, authority=verifier)

    result = await reader.resolve(_query(), _context())

    assert result.status is RetentionEligibilityStatus.ELIGIBLE
    assert result.eligible is True
    assert result.source_decisions[0].applicability_id == _uuid(100)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("bindings", "expected"),
    [
        ((), RetentionEligibilityStatus.MISSING),
        (
            (
                _binding(applicability_id=110, source_object_id=None, source_class="document"),
                _binding(applicability_id=120, source_object_id=None, source_class="document"),
            ),
            RetentionEligibilityStatus.AMBIGUOUS,
        ),
        (
            (
                _binding(applicability_id=130, source_object_id=_uuid(10), policy=50),
                _binding(applicability_id=140, source_object_id=None, source_class="document", policy=60),
            ),
            RetentionEligibilityStatus.CONFLICTING,
        ),
        (
            (
                _binding(
                    applicability_id=150,
                    source_object_id=_uuid(10),
                    effective_until=NOW - timedelta(seconds=1),
                ),
            ),
            RetentionEligibilityStatus.EXPIRED,
        ),
    ],
)
async def test_missing_ambiguous_conflicting_or_expired_binding_denies(
    bindings: tuple[RetentionApplicability, ...],
    expected: RetentionEligibilityStatus,
) -> None:
    source = _source(10)
    reader = _reader(_Candidates({source.source_object_id: bindings}), source)

    result = await reader.resolve(_query(), _context())

    assert result.status is expected
    assert result.eligible is False


@pytest.mark.asyncio
async def test_mixed_artifact_reports_ineligible_source_for_suppression_or_rebuild() -> None:
    first = _source(10)
    second = _source(20)
    reader = _reader(
        _Candidates(
            {
                first.source_object_id: (
                    _binding(applicability_id=160, source_object_id=first.source_object_id),
                ),
                second.source_object_id: (
                    _binding(
                        applicability_id=170,
                        source_object_id=second.source_object_id,
                        effective_until=NOW - timedelta(seconds=1),
                    ),
                ),
            }
        ),
        first,
        second,
    )

    result = await reader.resolve(_query(), _context())

    assert result.status is RetentionEligibilityStatus.EXPIRED
    assert tuple(item.status for item in result.source_decisions) == (
        RetentionEligibilityStatus.ELIGIBLE,
        RetentionEligibilityStatus.EXPIRED,
    )


@pytest.mark.asyncio
async def test_cross_tenant_context_fails_closed_without_resolving_sources() -> None:
    source = _source(10)
    candidates = _Candidates(
        {source.source_object_id: (_binding(applicability_id=180, source_object_id=source.source_object_id),)}
    )
    lineages = _Lineages((source,))
    reader = RetentionEligibilityReader(
        candidates=candidates,
        lineages=lineages,
        authority=_Authority(),
        clock=lambda: NOW,
    )

    result = await reader.resolve(_query(), _context(workspace_id=_uuid(999)))

    assert result.status is RetentionEligibilityStatus.AUTHORITY_UNAVAILABLE
    assert candidates.calls == []
    assert lineages.calls == []


@pytest.mark.asyncio
async def test_incomplete_or_untrusted_artifact_lineage_denies_before_binding_selection() -> None:
    source = _source(10)
    candidates = _Candidates(
        {source.source_object_id: (_binding(applicability_id=190, source_object_id=source.source_object_id),)}
    )
    reader = _reader(candidates)

    result = await reader.resolve(_query(), _context())

    assert result.status is RetentionEligibilityStatus.LINEAGE_UNAVAILABLE
    assert candidates.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("policy", "authority_digest"),
    [(51, "b" * 64), (50, "c" * 64)],
)
async def test_missing_current_policy_or_binding_authority_denies(
    policy: int, authority_digest: str
) -> None:
    source = _source(10)
    binding = _binding(
        applicability_id=200, source_object_id=source.source_object_id, policy=policy
    ).model_copy(
        update={"approving_authority_digest": authority_digest}
    )
    candidates = _Candidates({source.source_object_id: (binding,)})
    verifier = ApprovedRetentionApplicabilityAuthority(
        policies=_PolicyApprovals(), bindings=_BindingApprovals()
    )
    reader = _reader(candidates, source, authority=verifier)

    result = await reader.resolve(_query(), _context())

    assert result.status is RetentionEligibilityStatus.AUTHORITY_UNAVAILABLE


def test_retention_eligibility_rejects_aggregate_status_contradicting_sources() -> None:
    with pytest.raises(ValidationError):
        RetentionEligibility(
            workspace_id=_uuid(1),
            environment_id=_uuid(2),
            artifact_class=RetentionArtifactClass.ORIGINAL,
            status=RetentionEligibilityStatus.EXPIRED,
            source_decisions=(
                RetentionSourceDecision(
                    source_object_id=_uuid(10),
                    source_revision_id=_uuid(1010),
                    status=RetentionEligibilityStatus.MISSING,
                ),
            ),
        )


@pytest.mark.asyncio
async def test_writer_authority_outage_is_disclosure_safe() -> None:
    source = _source(10)
    binding = _binding(applicability_id=210, source_object_id=source.source_object_id)
    verifier = ApprovedRetentionApplicabilityAuthority(
        policies=_UnavailablePolicyApprovals(), bindings=_BindingApprovals()
    )

    with pytest.raises(RetentionApplicabilityError) as caught:
        await verifier.verify(binding, _context(), cast(UnitOfWork, object()))

    assert str(caught.value) == "Retention applicability authority is unavailable."
