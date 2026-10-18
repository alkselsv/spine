"""Deterministic citation and claim-support metrics for the offline Q&A set.

The evaluator treats the checked-in Q&A dataset and the supplied ContextBundle
as the only authoritative evidence.  It never scores an excerpt or a semantic
judge as a substitute for source identity, lifecycle, authorization, locator,
or bundle-membership checks.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from typing import Any, Final, Literal
from uuid import UUID, uuid5

from pydantic import Field, TypeAdapter, model_validator

from spine.domain.common import DefinitionModel
from spine.domain.evaluations import EvaluationResult, EvaluationVerdict
from spine.evals.qa_r1 import Locator, QACase, QADataset
from spine.memory.contracts import ContextReference, RetrievalResult


CITATION_EVALUATOR_KEY: Final[str] = "qa.citations"
CITATION_PRECISION_EVALUATOR_KEY: Final[str] = "qa.citation_precision"
CITATION_RECALL_EVALUATOR_KEY: Final[str] = "qa.citation_recall"
LOCATOR_VALIDITY_EVALUATOR_KEY: Final[str] = "qa.locator_validity"
GROUNDEDNESS_EVALUATOR_KEY: Final[str] = "qa.groundedness"
CITATION_METRICS_EVALUATOR_VERSION: Final[str] = "v1"
_SUBJECT_NAMESPACE: Final[UUID] = UUID("2a9d8b5c-6c28-46d7-bd2a-0a664348d171")
_LOCATOR = TypeAdapter(Locator)

AnswerOutcome = Literal[
    "answered",
    "abstained",
    "denied",
    "clarification_required",
    "failed",
    "cancelled",
]


def answer_outcome_passes(
    *,
    observed_outcome: AnswerOutcome,
    expected_outcome: Literal["answered", "abstained", "denied"],
    has_claims: bool,
    has_citations: bool,
) -> bool:
    """The shared v1 answer-outcome formula used by every dataset adapter."""

    return observed_outcome == expected_outcome and (
        observed_outcome == "answered" or (not has_claims and not has_citations)
    )


def score_grounded_claims(
    *,
    expected_evidence_by_claim: Mapping[str, frozenset[str]],
    observed_evidence_by_claim: Mapping[str, frozenset[str]],
) -> tuple[float | None, int, int, tuple[str, ...]]:
    """Apply v1 explicit claim-to-evidence support without semantic guessing."""

    supported = {
        claim_id
        for claim_id, expected in expected_evidence_by_claim.items()
        if expected <= observed_evidence_by_claim.get(claim_id, frozenset())
    }
    unsupported = (
        set(expected_evidence_by_claim) - supported
    ) | (set(observed_evidence_by_claim) - set(expected_evidence_by_claim))
    eligible = len(expected_evidence_by_claim)
    return (
        len(supported) / eligible if eligible else None,
        len(supported),
        eligible,
        tuple(sorted(unsupported)),
    )


class Citation(DefinitionModel):
    """A candidate answer's immutable reference to one evidence item.

    The fields that can be supplied by a model are intentionally permissive at
    this boundary.  The evaluator must classify malformed or fabricated input,
    rather than making Pydantic reject it before a deterministic result exists.
    """

    citation_id: str | None = Field(default=None, max_length=128)
    evidence_id: str | None = Field(default=None, max_length=128)
    source_object_id: UUID | None = None
    source_revision_id: UUID | None = None
    workspace_id: UUID | None = None
    environment_id: UUID | None = None
    revision_digest: str | None = None
    source_revision_digest: str | None = None
    locator: Any = None
    context_bundle_id: str | None = None

    @model_validator(mode="after")
    def validate_digest_aliases(self) -> "Citation":
        if (
            self.revision_digest is not None
            and self.source_revision_digest is not None
            and self.revision_digest != self.source_revision_digest
        ):
            raise ValueError("revision digest aliases must match")
        return self


class AnswerClaim(DefinitionModel):
    """A labelled factual claim and its candidate citation identities."""

    claim_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1)
    citation_ids: tuple[str, ...] = ()


class AnswerSubmission(DefinitionModel):
    """The small provider-neutral answer contract consumed by the evaluators."""

    outcome: AnswerOutcome
    claims: tuple[AnswerClaim, ...] = ()
    citations: tuple[Citation, ...] = ()
    semantic_score: float | None = Field(default=None, ge=0.0, le=1.0)


class ContextBundle(DefinitionModel):
    """The exact persisted evidence membership used for one offline answer."""

    bundle_id: str = Field(min_length=1, max_length=128)
    workspace_id: UUID
    environment_id: UUID
    references: tuple[ContextReference, ...] = ()


def evaluate_citations(
    answer: AnswerSubmission,
    case: QACase,
    *,
    dataset: QADataset,
    context_bundle: ContextBundle | RetrievalResult,
    subject_id: UUID | None = None,
) -> EvaluationResult:
    """Compute citation metrics and apply deterministic safety gates."""

    context_bundle = _coerce_bundle(context_bundle, case)
    assessment = _assess(answer, case, dataset=dataset, context_bundle=context_bundle)
    details = {
        "outcome": answer.outcome,
        "expected_outcome": case.expected_outcome,
        "status": "measured" if assessment.deterministic_passed else "failed",
        "citation_count": len(answer.citations),
        "valid_citation_count": assessment.valid_count,
        "expected_citation_count": len(case.expected_evidence_ids),
        "citation_precision": assessment.precision,
        "citation_recall": assessment.recall,
        "locator_validity": assessment.locator_validity,
        "citation_identity_validity": assessment.identity_validity,
        "reference_validity": assessment.identity_validity,
        "failure_counts": dict(sorted(assessment.failures.items())),
        "semantic_score": answer.semantic_score,
        "deterministic_gate": "passed" if assessment.deterministic_passed else "failed",
    }
    return _result(
        evaluator_key=CITATION_EVALUATOR_KEY,
        case=case,
        subject_id=subject_id,
        score=1.0 if assessment.deterministic_passed else 0.0,
        verdict=EvaluationVerdict.PASS if assessment.deterministic_passed else EvaluationVerdict.FAIL,
        details=details,
        identity=f"citations:{answer.model_dump_json()}:{context_bundle.model_dump_json()}",
    )


def evaluate_citation_precision(
    answer: AnswerSubmission,
    case: QACase,
    *,
    dataset: QADataset,
    context_bundle: ContextBundle | RetrievalResult,
    subject_id: UUID | None = None,
) -> EvaluationResult:
    """Return citation precision while preserving the full citation audit."""

    return _metric_result(
        answer,
        case,
        dataset=dataset,
        context_bundle=context_bundle,
        subject_id=subject_id,
        evaluator_key=CITATION_PRECISION_EVALUATOR_KEY,
        metric_name="citation_precision",
    )


def evaluate_citation_recall(
    answer: AnswerSubmission,
    case: QACase,
    *,
    dataset: QADataset,
    context_bundle: ContextBundle | RetrievalResult,
    subject_id: UUID | None = None,
) -> EvaluationResult:
    """Return citation recall while preserving the full citation audit."""

    return _metric_result(
        answer,
        case,
        dataset=dataset,
        context_bundle=context_bundle,
        subject_id=subject_id,
        evaluator_key=CITATION_RECALL_EVALUATOR_KEY,
        metric_name="citation_recall",
    )


def evaluate_locator_validity(
    answer: AnswerSubmission,
    case: QACase,
    *,
    dataset: QADataset,
    context_bundle: ContextBundle | RetrievalResult,
    subject_id: UUID | None = None,
) -> EvaluationResult:
    """Return exact locator validity for candidate citations."""

    return _metric_result(
        answer,
        case,
        dataset=dataset,
        context_bundle=context_bundle,
        subject_id=subject_id,
        evaluator_key=LOCATOR_VALIDITY_EVALUATOR_KEY,
        metric_name="locator_validity",
    )


def evaluate_groundedness(
    answer: AnswerSubmission,
    case: QACase,
    *,
    dataset: QADataset,
    context_bundle: ContextBundle | RetrievalResult,
    subject_id: UUID | None = None,
) -> EvaluationResult:
    """Measure labelled claim-to-evidence support without semantic guessing."""

    context_bundle = _coerce_bundle(context_bundle, case)
    assessment = _assess(answer, case, dataset=dataset, context_bundle=context_bundle)
    valid_by_id = assessment.valid_evidence_by_citation
    missing_by_claim: dict[str, list[str]] = {}
    observed_evidence_by_claim: dict[str, frozenset[str]] = {}

    for claim in case.expected_claims:
        candidate = next((item for item in answer.claims if item.claim_id == claim.claim_id), None)
        cited_evidence = {
            valid_by_id[citation_id]
            for citation_id in candidate.citation_ids
            if candidate is not None and citation_id in valid_by_id
        } if candidate is not None else set()
        observed_evidence_by_claim[claim.claim_id] = frozenset(cited_evidence)
        missing = sorted(set(claim.evidence_ids) - cited_evidence)
        if missing:
            missing_by_claim[claim.claim_id] = missing
    for claim in answer.claims:
        observed_evidence_by_claim.setdefault(claim.claim_id, frozenset())
    score, supported_count, expected_count, unsupported_claims = score_grounded_claims(
        expected_evidence_by_claim={
            claim.claim_id: frozenset(claim.evidence_ids)
            for claim in case.expected_claims
        },
        observed_evidence_by_claim=observed_evidence_by_claim,
    )
    passed = (
        case.expected_outcome != "answered"
        and answer.outcome == case.expected_outcome
        and not answer.claims
        and not answer.citations
    ) or (
        case.expected_outcome == "answered"
        and answer.outcome == "answered"
        and not assessment.failures
        and not unsupported_claims
        and supported_count == expected_count
    )
    details = {
        "outcome": answer.outcome,
        "expected_outcome": case.expected_outcome,
        "status": "measured" if passed else "failed",
        "supported_claim_count": supported_count,
        "expected_claim_count": expected_count,
        "groundedness_support": score,
        "unsupported_claim_ids": list(unsupported_claims),
        "missing_evidence_by_claim": {
            claim_id: sorted(set(evidence_ids))
            for claim_id, evidence_ids in sorted(missing_by_claim.items())
        },
        "deterministic_gate": "passed" if passed else "failed",
        "semantic_score": answer.semantic_score,
    }
    return _result(
        evaluator_key=GROUNDEDNESS_EVALUATOR_KEY,
        case=case,
        subject_id=subject_id,
        score=score,
        verdict=EvaluationVerdict.PASS if passed else EvaluationVerdict.FAIL,
        details=details,
        identity=f"groundedness:{answer.model_dump_json()}:{context_bundle.model_dump_json()}",
    )


def evaluate_answer_outcome(
    answer: AnswerSubmission,
    case: QACase,
    *,
    dataset: QADataset,
    context_bundle: ContextBundle,
    subject_id: UUID | None = None,
) -> EvaluationResult:
    """Evaluate answered, abstained, and denied as distinct outcomes."""

    del dataset, context_bundle
    passed = answer_outcome_passes(
        observed_outcome=answer.outcome,
        expected_outcome=case.expected_outcome,
        has_claims=bool(answer.claims),
        has_citations=bool(answer.citations),
    )
    return _result(
        evaluator_key="qa.answer_outcome",
        case=case,
        subject_id=subject_id,
        score=1.0 if passed else 0.0,
        verdict=EvaluationVerdict.PASS if passed else EvaluationVerdict.FAIL,
        details={
            "outcome": answer.outcome,
            "expected_outcome": case.expected_outcome,
            "deterministic_gate": "passed" if passed else "failed",
        },
        identity=f"outcome:{case.case_id}:{answer.outcome}:{case.expected_outcome}",
    )


class _Assessment:
    def __init__(
        self,
        *,
        precision: float | None,
        recall: float | None,
        locator_validity: float | None,
        identity_validity: float | None,
        valid_count: int,
        failures: Counter[str],
        valid_evidence_by_citation: dict[str, str],
        deterministic_passed: bool,
    ) -> None:
        self.precision = precision
        self.recall = recall
        self.locator_validity = locator_validity
        self.identity_validity = identity_validity
        self.valid_count = valid_count
        self.failures = failures
        self.valid_evidence_by_citation = valid_evidence_by_citation
        self.deterministic_passed = deterministic_passed


def _metric_result(
    answer: AnswerSubmission,
    case: QACase,
    *,
    dataset: QADataset,
    context_bundle: ContextBundle | RetrievalResult,
    subject_id: UUID | None,
    evaluator_key: str,
    metric_name: str,
) -> EvaluationResult:
    context_bundle = _coerce_bundle(context_bundle, case)
    assessment = _assess(answer, case, dataset=dataset, context_bundle=context_bundle)
    score = getattr(
        assessment,
        {"citation_precision": "precision", "citation_recall": "recall"}.get(metric_name, metric_name),
    )
    passed = score == 1.0 and assessment.deterministic_passed
    verdict = (
        EvaluationVerdict.PASS
        if passed
        else EvaluationVerdict.REVIEW
        if score is None and case.expected_outcome != "answered"
        else EvaluationVerdict.FAIL
    )
    return _result(
        evaluator_key=evaluator_key,
        case=case,
        subject_id=subject_id,
        score=score,
        verdict=verdict,
        details={
            metric_name: score,
            "failure_counts": dict(sorted(assessment.failures.items())),
            "status": "measured" if score is not None else "not_applicable",
            "deterministic_gate": "passed" if assessment.deterministic_passed else "failed",
        },
        identity=f"{evaluator_key}:{answer.model_dump_json()}:{context_bundle.model_dump_json()}",
    )


def _assess(
    answer: AnswerSubmission,
    case: QACase,
    *,
    dataset: QADataset,
    context_bundle: ContextBundle,
) -> _Assessment:
    evidence_by_id = {
        evidence.evidence_id: (revision, evidence)
        for revision in dataset.source_revisions
        for evidence in revision.evidence
    }
    object_by_id = {source.source_object_id: source for source in dataset.source_objects}
    current_by_object: dict[UUID, Any] = {}
    for revision in dataset.source_revisions:
        previous = current_by_object.get(revision.source_object_id)
        if previous is None or (revision.revision_number, str(revision.revision_id)) > (
            previous.revision_number,
            str(previous.revision_id),
        ):
            current_by_object[revision.source_object_id] = revision
    bundle_by_evidence = {reference.evidence_id: reference for reference in context_bundle.references}

    failures: Counter[str] = Counter()
    valid_evidence_by_citation: dict[str, str] = {}
    valid_count = 0
    valid_locator_count = 0
    valid_identity_count = 0
    seen_citations: set[str] = set()

    for citation in answer.citations:
        status, locator_valid, identity_valid = _classify_citation(
            citation,
            case=case,
            evidence_by_id=evidence_by_id,
            object_by_id=object_by_id,
            current_by_object=current_by_object,
            bundle=context_bundle,
            bundle_by_evidence=bundle_by_evidence,
        )
        if citation.citation_id in seen_citations:
            status = "malformed"
            locator_valid = False
            identity_valid = False
        seen_citations.add(citation.citation_id)
        if status == "valid":
            valid_count += 1
            valid_evidence_by_citation[citation.citation_id] = citation.evidence_id
        else:
            failures[status] += 1
        valid_locator_count += int(locator_valid)
        valid_identity_count += int(identity_valid)

    expected_ids = set(case.expected_evidence_ids)
    valid_expected = set(valid_evidence_by_citation.values()) & expected_ids
    citation_count = len(answer.citations)
    precision = len(valid_expected) / citation_count if citation_count else (1.0 if case.expected_outcome != "answered" else 0.0)
    recall = len(valid_expected) / len(expected_ids) if expected_ids else None
    locator_validity = valid_locator_count / citation_count if citation_count else (1.0 if case.expected_outcome != "answered" else 0.0)
    identity_validity = valid_identity_count / citation_count if citation_count else (1.0 if case.expected_outcome != "answered" else 0.0)

    if answer.outcome != case.expected_outcome:
        failures["outcome_mismatch"] += 1
    if case.expected_outcome != "answered" and (answer.claims or answer.citations):
        failures["unexpected_content"] += 1
    deterministic_passed = (
        not failures
        and answer.outcome == case.expected_outcome
        and (
            case.expected_outcome != "answered"
            or (precision == 1.0 and recall == 1.0 and locator_validity == 1.0 and identity_validity == 1.0)
        )
    )
    return _Assessment(
        precision=precision,
        recall=recall,
        locator_validity=locator_validity,
        identity_validity=identity_validity,
        valid_count=valid_count,
        failures=failures,
        valid_evidence_by_citation=valid_evidence_by_citation,
        deterministic_passed=deterministic_passed,
    )


def _classify_citation(
    citation: Citation,
    *,
    case: QACase,
    evidence_by_id: Mapping[str, tuple[Any, Any]],
    object_by_id: Mapping[UUID, Any],
    current_by_object: Mapping[UUID, Any],
    bundle: ContextBundle,
    bundle_by_evidence: Mapping[str, ContextReference],
) -> tuple[str, bool, bool]:
    """Return ``(status, locator_valid, identity_valid)`` without disclosures."""

    if not citation.citation_id or not citation.evidence_id:
        return "malformed", False, False
    if (
        citation.source_object_id is None
        or citation.source_revision_id is None
        or citation.workspace_id is None
        or citation.environment_id is None
        or citation.context_bundle_id is None
        or (citation.revision_digest or citation.source_revision_digest) is None
    ):
        return "malformed", False, False
    record = evidence_by_id.get(citation.evidence_id)
    if record is None:
        return "fabricated", False, False
    revision, evidence = record
    source_object = object_by_id.get(revision.source_object_id)
    if source_object is None:
        return "unresolvable", False, False

    locator_valid = _locator_matches(citation.locator, evidence.locator)
    if not locator_valid:
        return "malformed", False, False

    if citation.context_bundle_id is not None and citation.context_bundle_id != bundle.bundle_id:
        return "unresolvable", True, False
    if citation.source_object_id is not None and citation.source_object_id != revision.source_object_id:
        return "fabricated", True, False
    if citation.source_revision_id is not None and citation.source_revision_id != revision.revision_id:
        return "fabricated", True, False
    supplied_digest = citation.revision_digest or citation.source_revision_digest
    if supplied_digest is not None and supplied_digest != revision.revision_digest:
        return "fabricated", True, False

    if citation.workspace_id is not None and citation.workspace_id != case.workspace_id:
        return "unauthorized", True, False
    if citation.environment_id is not None and citation.environment_id != case.environment_id:
        return "unauthorized", True, False
    if (revision.workspace_id, revision.environment_id) != (case.workspace_id, case.environment_id):
        return "unauthorized", True, False

    current = current_by_object[revision.source_object_id]
    if current.kind == "tombstone":
        return "tombstoned", True, False
    if current.revision_id != revision.revision_id:
        return "stale", True, False
    if not any(
        grant.source_object_id == revision.source_object_id
        and grant.principal_id == case.principal_id
        and grant.read_content
        for grant in case.acl
    ):
        return "unauthorized", True, False
    if citation.evidence_id not in case.expected_evidence_ids:
        return "unrelated", True, False
    if revision.revision_id not in case.available_revision_ids:
        return "unrelated", True, False

    bundle_reference = bundle_by_evidence.get(citation.evidence_id)
    if bundle_reference is None or not _bundle_reference_matches(bundle, bundle_reference, revision, evidence):
        return "unresolvable", True, False
    return "valid", True, True


def _bundle_reference_matches(
    bundle: ContextBundle,
    reference: ContextReference,
    revision: Any,
    evidence: Any,
) -> bool:
    return (
        bundle.workspace_id == revision.workspace_id
        and bundle.environment_id == revision.environment_id
        and reference.source_id == str(revision.source_object_id)
        and reference.source_revision_id == revision.revision_id
        and reference.workspace_id == revision.workspace_id
        and reference.environment_id == revision.environment_id
        and reference.locator == evidence.locator.model_dump(mode="json")
    )


def _locator_matches(value: Any, expected: Locator) -> bool:
    if not isinstance(value, dict):
        return False
    try:
        parsed = _LOCATOR.validate_python(value)
    except (TypeError, ValueError):
        return False
    return parsed == expected


def _coerce_bundle(bundle: ContextBundle | RetrievalResult, case: QACase) -> ContextBundle:
    if isinstance(bundle, ContextBundle):
        return bundle
    return ContextBundle(
        bundle_id=bundle.index_version or "retrieval-result",
        workspace_id=case.workspace_id,
        environment_id=case.environment_id,
        references=bundle.references,
    )


def _result(
    *,
    evaluator_key: str,
    case: QACase,
    subject_id: UUID | None,
    score: float | None,
    verdict: EvaluationVerdict,
    details: dict[str, Any],
    identity: str,
) -> EvaluationResult:
    return EvaluationResult(
        id=uuid5(_SUBJECT_NAMESPACE, f"{evaluator_key}:{CITATION_METRICS_EVALUATOR_VERSION}:{identity}"),
        evaluator_key=evaluator_key,
        evaluator_version=CITATION_METRICS_EVALUATOR_VERSION,
        subject_type="qa_case",
        subject_id=subject_id or uuid5(_SUBJECT_NAMESPACE, case.case_id),
        workspace_id=case.workspace_id,
        environment_id=case.environment_id,
        verdict=verdict,
        score=score,
        details=details,
    )


__all__ = [
    "AnswerClaim",
    "AnswerOutcome",
    "AnswerSubmission",
    "CITATION_EVALUATOR_KEY",
    "CITATION_METRICS_EVALUATOR_VERSION",
    "CITATION_PRECISION_EVALUATOR_KEY",
    "CITATION_RECALL_EVALUATOR_KEY",
    "Citation",
    "ContextBundle",
    "GROUNDEDNESS_EVALUATOR_KEY",
    "LOCATOR_VALIDITY_EVALUATOR_KEY",
    "evaluate_answer_outcome",
    "evaluate_citation_precision",
    "evaluate_citation_recall",
    "evaluate_citations",
    "evaluate_groundedness",
    "evaluate_locator_validity",
]
