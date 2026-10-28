from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest

from spine.infrastructure.qa_worker_composition import (
    _ACTIVE_AUTHORIZATION_CONTEXT,
    _ACTIVE_COMPLETENESS_INTENT,
    _ACTIVE_EVIDENCE_CONTENT,
    _ACTIVE_RETRIEVAL_SCOPE,
    _ActiveCompletenessIntent,
    _CanonicalEvidenceVerifier,
    _ParsedProjectionRetrieval,
    _ValidationPlans,
    _projection_reference_matches_evidence,
)
from spine.application.content_authorization import (
    AuthorizationContextMode,
    TrustedAuthorizationContext,
)
from spine.application.evidence_lineage_verification import (
    EvidenceVerificationReasonCode,
    RejectedEvidenceItem,
)
from spine.domain.capabilities.answer_evaluation import (
    AnswerDisclosureStatus,
    AnswerPartialityStatus,
    CoverageEvaluationStatus,
    CoverageMemberStatus,
    MissingCoverageImpact,
    PartialFallbackAuthority,
)
from spine.domain.context_profiles import CompletenessRequirement, PartialAnswerFallback
from spine.domain.projections import ProjectionFreshness, ProjectionHealth
from spine.domain.sources import RevisionKind
from tests.application.test_production_qa_capability import (
    ExistingCitationValidation,
    ExistingSemanticValidation,
)
from tests.domain.capabilities.test_answer_question_candidate import (
    candidate_input,
    candidate_output,
)
from spine.memory.contracts import ProjectionOutcome


class _Rows:
    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self._rows = rows

    def mappings(self) -> "_Rows":
        return self

    def all(self) -> list[SimpleNamespace]:
        return self._rows


class _Transaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_: object) -> None:
        return None


class _Session:
    def __init__(self, row: SimpleNamespace) -> None:
        self._row = row

    async def __aenter__(self) -> "_Session":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    def begin(self) -> _Transaction:
        return _Transaction()

    async def execute(self, statement: object, parameters: object) -> _Rows:
        del parameters
        sql = str(statement)
        if "set_config" in sql:
            return _Rows([])
        rows = (
            [self._row]
            if "r.segment_id = CONCAT('segment-'" in sql
            and "r.projected_object_id = CAST(s.segment_id AS text)" not in sql
            and "JOIN spine.source_revisions revision" in sql
            and "JOIN spine.canonical_boundary_members boundary_member" in sql
            else []
        )
        return _Rows(rows)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "expected_method"),
    (
        (AuthorizationContextMode.HUMAN_DISCLOSURE, "human"),
        (AuthorizationContextMode.INTERACTIVE_PROCESSING, "interactive"),
        (AuthorizationContextMode.NONINTERACTIVE_PROCESSING, "noninteractive"),
    ),
)
async def test_evidence_verifier_preserves_authorization_mode(
    mode: AuthorizationContextMode,
    expected_method: str,
) -> None:
    source_object_id = UUID(int=3)

    class Authorization:
        def __init__(self) -> None:
            self.calls: list[
                tuple[str, AuthorizationContextMode, tuple[UUID, ...]]
            ] = []

        async def decide_human_disclosure(
            self,
            context: TrustedAuthorizationContext,
            source_ids: tuple[UUID, ...],
        ) -> object:
            self.calls.append(("human", context.mode, tuple(source_ids)))
            return SimpleNamespace(allowed=False)

        async def decide_interactive_processing(
            self,
            context: TrustedAuthorizationContext,
            source_ids: tuple[UUID, ...],
        ) -> object:
            self.calls.append(("interactive", context.mode, tuple(source_ids)))
            return SimpleNamespace(allowed=False)

        async def decide_noninteractive_processing(
            self,
            context: TrustedAuthorizationContext,
            source_ids: tuple[UUID, ...],
        ) -> object:
            self.calls.append(("noninteractive", context.mode, tuple(source_ids)))
            return SimpleNamespace(allowed=False)

    authorization = Authorization()
    verifier = _CanonicalEvidenceVerifier(
        authorization=authorization,  # type: ignore[arg-type]
        uow_factory=None,
        boundary=object(),  # type: ignore[arg-type]
        service_principal_id=UUID(int=7),
        operation="verify_projection_evidence",
    )
    context = TrustedAuthorizationContext(
        mode=mode,
        workspace_id=UUID(int=1),
        environment_id=UUID(int=2),
        canonical_human_id=(
            None
            if mode is AuthorizationContextMode.NONINTERACTIVE_PROCESSING
            else UUID(int=6)
        ),
        service_principal_id=(
            None
            if mode is AuthorizationContextMode.HUMAN_DISCLOSURE
            else UUID(int=7)
        ),
        purpose="answer_question",
        operation="retrieve_context",
        membership_generation=1,
        authority_generation=1,
        service_authority_generation=(
            None
            if mode is AuthorizationContextMode.HUMAN_DISCLOSURE
            else 1
        ),
        authority_snapshot_reference=UUID(int=8),
        trace_id=UUID(int=9),
        issued_at=datetime(2026, 10, 28, tzinfo=timezone.utc),
        noninteractive_authority_reference=(
            UUID(int=10)
            if mode is AuthorizationContextMode.NONINTERACTIVE_PROCESSING
            else None
        ),
    )

    result = await verifier.verify(
        item=SimpleNamespace(
            chunk_id="synthetic-chunk",
            evidence=(SimpleNamespace(source_object_id=source_object_id),),
        ),
        publication_pin=object(),
        authorization_context=context,
    )

    assert isinstance(result, RejectedEvidenceItem)
    assert result.reason_code is EvidenceVerificationReasonCode.AUTHORIZATION_DENIED
    assert authorization.calls == [
        (expected_method, mode, (source_object_id,))
    ]


@pytest.mark.asyncio
async def test_parsed_retrieval_joins_canonical_segment_with_physical_object_id() -> None:
    workspace_id = UUID(int=1)
    environment_id = UUID(int=2)
    source_object_id = UUID(int=3)
    source_revision_id = UUID(int=4)
    segment_id = UUID(int=5)
    physical_data_id = UUID(int=6)
    source = SimpleNamespace(
        source_object_id=source_object_id,
        source_revision_id=source_revision_id,
        revision_digest=None,
        revision_number=None,
        processing_receipt_id=UUID(int=7),
        locator_contract_version="physical-line-codepoint:v1",
    )
    row = SimpleNamespace(
        source_revision_id=source_revision_id,
        source_object_id=source_object_id,
        segment_id=segment_id,
        segment_digest="b" * 64,
        text="Synthetic qualification evidence.",
        locator_reference="character_range:line:1:0-33",
        locator_digest="c" * 64,
        projected_kind="chunk",
        projected_object_id=str(physical_data_id),
        evidence_id=f"segment-{segment_id.hex}",
        revision_digest="a" * 64,
        revision_number=3,
    )
    command = SimpleNamespace(
        workspace_id=workspace_id,
        environment_id=environment_id,
        retrieval_sources=(source,),
        projection_run_id=UUID(int=8),
        projection_candidate=SimpleNamespace(snapshot_id=UUID(int=9)),
        limit=10,
        command_identity="retrieve-1",
        command_digest="d" * 64,
    )
    retrieval = _ParsedProjectionRetrieval(lambda: _Session(row))

    result = await retrieval.retrieve(command)

    assert result.outcome is ProjectionOutcome.SUCCEEDED
    assert result.chunks[0].chunk_id == str(physical_data_id)
    assert result.evidence[0].evidence_id == f"segment-{segment_id.hex}"
    assert result.evidence[0].revision_digest == "a" * 64
    assert result.evidence[0].revision_number == 3


def test_citation_lineage_matches_canonical_segment_not_physical_object_id() -> None:
    common = {
        "source_object_id": UUID(int=1),
        "source_revision_id": UUID(int=2),
        "processing_receipt_id": UUID(int=3),
        "projected_kind": "chunk",
        "locator_digest": "a" * 64,
    }
    reference = SimpleNamespace(
        **common,
        segment_id="segment-canonical",
        projected_object_id="chunk-physical",
    )
    evidence = SimpleNamespace(**common, evidence_id="segment-canonical")

    assert _projection_reference_matches_evidence(reference, evidence)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "include_new_source",
        "health",
        "completeness_requirement",
        "fallback",
        "allow_partial_answer",
        "expected_disclosure",
        "expected_coverage",
        "expected_partiality",
        "expected_fallback_authority",
    ),
    (
        (
            True,
            ProjectionHealth.HEALTHY,
            CompletenessRequirement.EXHAUSTIVE,
            PartialAnswerFallback.ALLOWED,
            True,
            AnswerDisclosureStatus.ACCEPTED,
            CoverageEvaluationStatus.INSUFFICIENT,
            AnswerPartialityStatus.PARTIAL,
            PartialFallbackAuthority.CURRENT_REQUEST,
        ),
        (
            False,
            ProjectionHealth.DEGRADED,
            CompletenessRequirement.EXHAUSTIVE,
            PartialAnswerFallback.ALLOWED,
            True,
            AnswerDisclosureStatus.ACCEPTED,
            CoverageEvaluationStatus.COMPLETE,
            AnswerPartialityStatus.EXHAUSTIVE,
            PartialFallbackAuthority.CURRENT_REQUEST,
        ),
        (
            True,
            ProjectionHealth.HEALTHY,
            CompletenessRequirement.EXHAUSTIVE,
            PartialAnswerFallback.DISALLOWED,
            False,
            AnswerDisclosureStatus.REJECTED,
            CoverageEvaluationStatus.INSUFFICIENT,
            AnswerPartialityStatus.NONE,
            None,
        ),
        (
            True,
            ProjectionHealth.HEALTHY,
            CompletenessRequirement.BEST_EFFORT,
            PartialAnswerFallback.DISALLOWED,
            False,
            AnswerDisclosureStatus.ACCEPTED,
            CoverageEvaluationStatus.COMPLETE,
            AnswerPartialityStatus.EXHAUSTIVE,
            None,
        ),
        (
            True,
            ProjectionHealth.HEALTHY,
            CompletenessRequirement.EXHAUSTIVE,
            PartialAnswerFallback.ALLOWED,
            None,
            AnswerDisclosureStatus.ACCEPTED,
            CoverageEvaluationStatus.INSUFFICIENT,
            AnswerPartialityStatus.PARTIAL,
            PartialFallbackAuthority.ACCEPTED_PREFERENCE,
        ),
    ),
)
async def test_production_evaluator_derives_current_scope_independently_of_projection_health(
    include_new_source: bool,
    health: ProjectionHealth,
    completeness_requirement: CompletenessRequirement,
    fallback: PartialAnswerFallback,
    allow_partial_answer: bool | None,
    expected_disclosure: AnswerDisclosureStatus,
    expected_coverage: CoverageEvaluationStatus,
    expected_partiality: AnswerPartialityStatus,
    expected_fallback_authority: PartialFallbackAuthority | None,
) -> None:
    candidate = candidate_output()
    bundle = candidate_input().context_bundle
    represented_reference = bundle.evidence_references[0]
    missing_source_id = UUID(int=9101)
    missing_revision_id = UUID(int=9102)

    class BoundaryMembers:
        async def resolve_current_members(self):
            represented = (
                SimpleNamespace(
                    source_object_id=represented_reference.source_object_id,
                    source_revision_id=represented_reference.source_revision_id,
                    revision_kind=RevisionKind.CONTENT,
                    current_revision_id=represented_reference.source_revision_id,
                    current_revision_kind=RevisionKind.CONTENT,
                    accepted_generation=1,
                    cas_version=1,
                ),
            )
            missing = (
                SimpleNamespace(
                    source_object_id=missing_source_id,
                    source_revision_id=missing_revision_id,
                    revision_kind=RevisionKind.CONTENT,
                    current_revision_id=missing_revision_id,
                    current_revision_kind=RevisionKind.CONTENT,
                    accepted_generation=2,
                    cas_version=1,
                ),
            )
            return represented + missing if include_new_source else represented

    class Authorization:
        async def decide_interactive_processing(self, context, source_ids):
            del context
            source_id = source_ids[0]
            revision_id = (
                represented_reference.source_revision_id
                if source_id == represented_reference.source_object_id
                else missing_revision_id
            )
            return SimpleNamespace(
                allowed=True,
                reason_code=SimpleNamespace(value="allowed"),
                source_object_ids=(source_id,),
                policy_evidence=(SimpleNamespace(source_revision_id=revision_id),),
                decision_reference=UUID(int=9200 + source_id.int),
            )

    class ImpactValidator:
        async def assess(self, **kwargs):
            del kwargs
            return SimpleNamespace(
                impact=(
                    MissingCoverageImpact.IMMATERIAL
                    if allow_partial_answer is not False
                    else MissingCoverageImpact.UNKNOWN
                ),
                validation_reference=UUID(int=9401),
                policy_version="qa.missing-coverage-impact:r1",
            )

    represented_constraint = SimpleNamespace(
        source_object_id=represented_reference.source_object_id,
        source_revision_id=represented_reference.source_revision_id,
        projection_snapshot_member_id=UUID(int=9301),
    )
    scope = SimpleNamespace(
        source_constraints=(represented_constraint,),
        publication_freshness=ProjectionFreshness.CURRENT,
        publication_health=health,
        completeness_requirement=completeness_requirement,
        partial_answer_fallback=fallback,
        profile_ref=SimpleNamespace(
            profile_id=UUID(int=9302),
            profile_version=1,
            profile_digest="f" * 64,
        ),
    )
    plans = _ValidationPlans(
        boundary_members=BoundaryMembers(),
        authorization=Authorization(),
        impact_validator=ImpactValidator(),
    )
    _ACTIVE_AUTHORIZATION_CONTEXT.set(object())
    _ACTIVE_RETRIEVAL_SCOPE.set(scope)
    _ACTIVE_COMPLETENESS_INTENT.set(
        _ActiveCompletenessIntent(
            allow_partial_answer=allow_partial_answer,
            command_id=UUID(int=9303),
        )
    )
    _ACTIVE_EVIDENCE_CONTENT.set(
        {represented_reference.evidence_id: "Synthetic authorized evidence."}
    )
    validation_plan = plans.create_plan(
        question=candidate.question,
        authorized_context=SimpleNamespace(bundle=bundle),
        candidate=candidate,
    )
    citation_results = tuple(
        [
            await ExistingCitationValidation().validate(
                citation=citation,
                expected_context_bundle=candidate.context_bundle,
                authorization_context=validation_plan.citation_authorization_context,
            )
            for citation in candidate.answer.citations
        ]
    )
    semantic_results = tuple(
        [
            await ExistingSemanticValidation().validate(request)
            for request in validation_plan.semantic_requests
        ]
    )

    result = await plans.evaluate(
        question=candidate.question,
        authorized_context=SimpleNamespace(bundle=bundle),
        candidate=candidate,
        citation_results=citation_results,
        semantic_results=semantic_results,
    )

    assert result.decision.disclosure_status is expected_disclosure
    assert result.decision.coverage_status is expected_coverage
    assert result.decision.partiality_status is expected_partiality
    assert (result.partial_fallback is not None) is (
        expected_fallback_authority is not None
    )
    if result.partial_fallback is not None:
        assert result.partial_fallback.authority is expected_fallback_authority
        if expected_fallback_authority is PartialFallbackAuthority.ACCEPTED_PREFERENCE:
            assert "f" * 64 in result.partial_fallback.policy_version
    expected_target_size = (
        2
        if include_new_source
        and completeness_requirement is not CompletenessRequirement.BEST_EFFORT
        else 1
    )
    assert len(result.target_scope.members) == expected_target_size
    if expected_target_size == 2:
        assert result.observed_coverage.members[1].status is CoverageMemberStatus.PROJECTION_GAP
