"""Post-observation evaluation contracts for the private real R1 dataset.

Detailed values in this module are local protected evaluation artifacts.  The
aggregate result contract defined below is the only disclosure-safe report
surface; it deliberately excludes questions, answers, case identities, source
mappings, evidence identities, excerpts, and locators.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from typing import Any, Final, Literal, Protocol
from uuid import UUID

from pydantic import Field, PrivateAttr, model_validator

from spine.domain.common import DefinitionModel
from spine.evals.production_contract import (
    ProductionContractObservation,
    ProductionContractObservationError,
)
from spine.evaluations.citation_metrics import (
    CITATION_METRICS_EVALUATOR_VERSION,
    answer_outcome_passes,
    score_grounded_claims,
)
from spine.evaluations.retrieval_metrics import (
    RETRIEVAL_METRICS_EVALUATOR_VERSION,
    score_mrr,
    score_recall_at_k,
)
from spine.evals.real_qa_r1 import (
    REAL_R1_DATASET_DIGEST,
    REAL_R1_DATASET_VERSION,
    LoadedRealDataset,
    RealClaim,
    RealLocator,
    RealQACase,
    RealRuntimeCaseResolution,
    RealRuntimeRevisionBinding,
)


REAL_EVALUATION_CASE_SCHEMA: Final[str] = "r1-real-evaluation-case:v1"
REAL_EVALUATION_RESULT_SCHEMA: Final[str] = "r1-real-evaluation-result:v1"
REAL_EVALUATOR_VERSION: Final[str] = "v1"
_DIGEST = r"^[0-9a-f]{64}$"
_FINALIZED_OBSERVATION_SCHEMA = "r1-finalized-real-observation:v1"
_CASE_RESULT_SCHEMA = "r1-real-evaluation-case-result:v1"
_RUNTIME_IDENTITY_SCHEMA = "r1-real-evaluation-runtime-identity:v1"
_APPROVED_CASE_COUNT = 149
_APPROVED_ANSWERED_COUNT = 136
_APPROVED_ABSTAINED_COUNT = 13
_APPROVED_CITATION_COUNT = 177
_APPROVED_CLAIM_EVIDENCE_COUNT = 158


class RealEvaluationContractError(ValueError):
    """A disclosure-safe failure to construct or evaluate a real case."""


class RealEvaluationRuntimeIdentity(DefinitionModel):
    """Exact execution/configuration versions attached to every quality score."""

    schema_version: Literal[_RUNTIME_IDENTITY_SCHEMA] = _RUNTIME_IDENTITY_SCHEMA
    model_version: str = Field(min_length=1, max_length=256)
    prompt_version: str = Field(min_length=1, max_length=256)
    validation_policy_version: str = Field(min_length=1, max_length=128)
    retrieval_strategy: str = Field(min_length=1, max_length=128)
    index_version: str = Field(min_length=1, max_length=256)
    context_profile_id: UUID | None = None
    context_profile_version: int | None = Field(default=None, ge=1)
    projection_config_version_id: UUID | None = None
    parser_contract_version: str | None = Field(default=None, min_length=1, max_length=128)
    locator_contract_version: str | None = Field(default=None, min_length=1, max_length=128)

    @classmethod
    def from_observation(
        cls,
        observation: ProductionContractObservation,
        *,
        model_version: str,
        prompt_version: str,
        validation_policy_version: str,
    ) -> "RealEvaluationRuntimeIdentity":
        bundle = observation.context_bundle
        if bundle is None:
            return cls(
                model_version=model_version,
                prompt_version=prompt_version,
                validation_policy_version=validation_policy_version,
                retrieval_strategy=observation.retrieval.retrieval_strategy,
                index_version=observation.retrieval.index_version,
            )
        projection = bundle.projection_config_version
        return cls(
            model_version=model_version,
            prompt_version=prompt_version,
            validation_policy_version=validation_policy_version,
            retrieval_strategy=observation.retrieval.retrieval_strategy,
            index_version=observation.retrieval.index_version,
            context_profile_id=bundle.profile_ref.profile_id,
            context_profile_version=bundle.profile_ref.profile_version,
            projection_config_version_id=projection.projection_config_version_id,
            parser_contract_version=projection.parser_contract_version,
            locator_contract_version=projection.locator_contract_version,
        )


class RealEvaluationEvidenceProvenance(DefinitionModel):
    """Exact private dataset identity joined to its verified canonical identity."""

    evidence_id: str = Field(min_length=1, max_length=256)
    dataset_source_id: str = Field(min_length=1, max_length=256)
    dataset_revision_id: str = Field(min_length=1, max_length=256)
    canonical_source_object_id: UUID
    canonical_source_revision_id: UUID
    locator: RealLocator


class FinalizedRealObservation(DefinitionModel):
    """A validated immutable production observation with its exact crosswalk."""

    schema_version: Literal[_FINALIZED_OBSERVATION_SCHEMA] = (
        _FINALIZED_OBSERVATION_SCHEMA
    )
    observation: ProductionContractObservation
    resolution: RealRuntimeCaseResolution
    _validation_proof: str | None = PrivateAttr(default=None)

    @classmethod
    def from_observation(
        cls,
        *,
        observation: ProductionContractObservation,
        resolution: RealRuntimeCaseResolution,
    ) -> "FinalizedRealObservation":
        if type(observation) is not ProductionContractObservation:
            raise ProductionContractObservationError("observation is not typed")
        observation.validate()
        request = resolution.request
        if observation.case_id != request.case_id:
            raise ProductionContractObservationError(
                "observation case identity does not match the resolved request"
            )
        if observation.question.subject.text != request.question:
            raise ProductionContractObservationError(
                "observation question does not match the resolved request"
            )
        if (
            observation.question.scope.workspace_id != request.workspace_id
            or observation.question.scope.environment_id != request.environment_id
        ):
            raise ProductionContractObservationError(
                "observation scope does not match the resolved request"
            )
        binding_pairs = {
            (binding.source_object_id, binding.source_revision_id)
            for binding in resolution.bindings
        }
        try:
            observed_pairs = {
                (UUID(reference.source_id), reference.source_revision_id)
                for reference in observation.retrieval.references
            }
        except (TypeError, ValueError):
            raise ProductionContractObservationError(
                "observation provenance identity is invalid"
            ) from None
        if not observed_pairs <= binding_pairs:
            raise ProductionContractObservationError(
                "observation provenance does not match verified bindings"
            )
        value = cls(observation=observation, resolution=resolution)
        object.__setattr__(value, "_validation_proof", value.public_fields_digest())
        return value

    @property
    def is_finalized(self) -> bool:
        return (
            self._validation_proof is not None
            and self._validation_proof == self.public_fields_digest()
        )

    def public_fields_digest(self) -> str:
        payload = self.model_dump(mode="json")
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


class RealGoldCasePort(Protocol):
    """Load protected labels only after a finalized observation exists."""

    dataset_version: str
    dataset_sha256: str
    population: "RealEvaluationPopulation"

    def load_case(self, finalized: FinalizedRealObservation) -> RealQACase: ...


class ValidatedRealGoldProvider:
    """Read cases from one structurally validated, explicitly selected dataset."""

    def __init__(self, loaded: LoadedRealDataset) -> None:
        if loaded.validation_report.validation_status != "passed" or loaded.dataset is None:
            raise RealEvaluationContractError("real gold dataset is not validated")
        self._dataset = loaded.dataset
        self.dataset_version = loaded.dataset.version
        self.dataset_sha256 = loaded.dataset.dataset_integrity_digest
        self.population = RealEvaluationPopulation.from_loaded(loaded)

    def load_case(self, finalized: FinalizedRealObservation) -> RealQACase:
        if type(finalized) is not FinalizedRealObservation or not finalized.is_finalized:
            raise RealEvaluationContractError(
                "real gold requires a validated finalized observation"
            )
        case_id = finalized.observation.case_id
        matches = tuple(case for case in self._dataset.cases if case.case_id == case_id)
        if len(matches) != 1:
            raise RealEvaluationContractError("real gold case is unavailable or ambiguous")
        return matches[0]


class RealEvaluationAdapter:
    """Post-observation boundary that releases gold to deterministic evaluators."""

    def join_gold(
        self,
        finalized: FinalizedRealObservation,
        *,
        gold: RealGoldCasePort,
    ) -> "RealEvaluationCase":
        if (
            type(finalized) is not FinalizedRealObservation
            or not finalized.is_finalized
        ):
            raise RealEvaluationContractError(
                "real evaluation requires a validated finalized observation"
            )
        case = gold.load_case(finalized)
        if type(case) is not RealQACase or case.case_id != finalized.observation.case_id:
            raise RealEvaluationContractError("gold provider returned another case")
        return RealEvaluationCase.from_case(
            case,
            dataset_version=gold.dataset_version,
            dataset_sha256=gold.dataset_sha256,
            bindings=finalized.resolution.bindings,
        )


class RealEvaluationCase(DefinitionModel):
    """Protected gold labels made available only at the evaluator boundary."""

    schema_version: Literal[REAL_EVALUATION_CASE_SCHEMA] = REAL_EVALUATION_CASE_SCHEMA
    dataset_version: str = Field(min_length=1, max_length=128)
    dataset_sha256: str = Field(pattern=_DIGEST)
    case_id: str = Field(min_length=1, max_length=256)
    expected_outcome: Literal["answered", "abstained", "denied"]
    expected_answer: str | None = None
    expected_claims: tuple[RealClaim, ...] = ()
    provenance: tuple[RealEvaluationEvidenceProvenance, ...] = ()

    @property
    def expected_citation_count(self) -> int:
        return len(self.provenance)

    @property
    def expected_claim_evidence_count(self) -> int:
        return sum(len(claim.evidence_ids) for claim in self.expected_claims)

    @classmethod
    def from_case(
        cls,
        case: RealQACase,
        *,
        dataset_version: str,
        dataset_sha256: str,
        bindings: tuple[RealRuntimeRevisionBinding, ...],
    ) -> "RealEvaluationCase":
        """Join gold labels to the already verified runtime identity crosswalk."""

        if type(case) is not RealQACase or case.synthetic is not False:
            raise RealEvaluationContractError("real evaluation case is invalid")
        if (
            dataset_version != REAL_R1_DATASET_VERSION
            or dataset_sha256 != REAL_R1_DATASET_DIGEST
        ):
            raise RealEvaluationContractError("real evaluation dataset identity mismatch")
        binding_by_identity = {
            (binding.dataset_source_id, binding.dataset_revision_id): binding
            for binding in bindings
        }
        if len(binding_by_identity) != len(bindings):
            raise RealEvaluationContractError("real evaluation bindings are ambiguous")
        if not set(case.source_ids) <= {
            binding.dataset_source_id for binding in bindings
        } or not set(case.source_revision_ids) <= {
            binding.dataset_revision_id for binding in bindings
        }:
            raise RealEvaluationContractError(
                "real evaluation case provenance does not match verified bindings"
            )
        provenance: list[RealEvaluationEvidenceProvenance] = []
        for citation in case.citations:
            binding = binding_by_identity.get(
                (citation.source_id, citation.source_revision_id)
            )
            if binding is None:
                raise RealEvaluationContractError(
                    "real evaluation citation has no verified runtime binding"
                )
            provenance.append(
                RealEvaluationEvidenceProvenance(
                    evidence_id=citation.evidence_id,
                    dataset_source_id=citation.source_id,
                    dataset_revision_id=citation.source_revision_id,
                    canonical_source_object_id=binding.source_object_id,
                    canonical_source_revision_id=binding.source_revision_id,
                    locator=citation.locator,
                )
            )
        return cls(
            dataset_version=dataset_version,
            dataset_sha256=dataset_sha256,
            case_id=case.case_id,
            expected_outcome=case.expected_outcome,
            expected_answer=case.expected_answer or case.golden_answer,
            expected_claims=case.expected_claims,
            provenance=tuple(provenance),
        )

    @model_validator(mode="after")
    def validate_gold_graph(self) -> "RealEvaluationCase":
        if (
            self.dataset_version != REAL_R1_DATASET_VERSION
            or self.dataset_sha256 != REAL_R1_DATASET_DIGEST
        ):
            raise ValueError("real evaluation dataset identity mismatch")
        evidence_ids = tuple(item.evidence_id for item in self.provenance)
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("real evaluation evidence identities must be unique")
        claim_ids = tuple(item.claim_id for item in self.expected_claims)
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError("real evaluation claim identities must be unique")
        claim_texts = tuple(item.text for item in self.expected_claims)
        if len(claim_texts) != len(set(claim_texts)):
            raise ValueError("real evaluation claim texts must be unique")
        claim_evidence = {
            evidence_id
            for claim in self.expected_claims
            for evidence_id in claim.evidence_ids
        }
        if not claim_evidence <= set(evidence_ids):
            raise ValueError("claim evidence is outside the expected citation set")
        if self.expected_outcome == "answered":
            if self.expected_answer is None or not self.expected_claims or not self.provenance:
                raise ValueError("answered real evaluation cases require gold labels")
        elif self.expected_answer is not None or self.expected_claims or self.provenance:
            raise ValueError("non-answered real evaluation cases cannot carry gold content")
        return self


class RealEvaluationPopulation(DefinitionModel):
    """Protected exact identity manifest derived from the validated gold set."""

    dataset_version: str = Field(min_length=1, max_length=128)
    dataset_sha256: str = Field(pattern=_DIGEST)
    case_outcomes: dict[str, Literal["answered", "abstained", "denied"]]
    citation_keys: tuple[tuple[str, str], ...]
    claim_evidence_keys: tuple[tuple[str, str, str], ...]

    @classmethod
    def from_loaded(cls, loaded: LoadedRealDataset) -> "RealEvaluationPopulation":
        if loaded.validation_report.validation_status != "passed" or loaded.dataset is None:
            raise RealEvaluationContractError("real gold dataset is not validated")
        return cls(
            dataset_version=loaded.dataset.version,
            dataset_sha256=loaded.dataset.dataset_integrity_digest,
            case_outcomes={case.case_id: case.expected_outcome for case in loaded.dataset.cases},
            citation_keys=tuple(
                (case.case_id, citation.evidence_id)
                for case in loaded.dataset.cases
                for citation in case.citations
            ),
            claim_evidence_keys=tuple(
                (case.case_id, claim.claim_id, evidence_id)
                for case in loaded.dataset.cases
                for claim in case.expected_claims
                for evidence_id in claim.evidence_ids
            ),
        )

    @model_validator(mode="after")
    def validate_approved_population(self) -> "RealEvaluationPopulation":
        outcomes = Counter(self.case_outcomes.values())
        case_ids = set(self.case_outcomes)
        if (
            self.dataset_version != REAL_R1_DATASET_VERSION
            or self.dataset_sha256 != REAL_R1_DATASET_DIGEST
            or len(self.case_outcomes) != _APPROVED_CASE_COUNT
            or outcomes
            != Counter(answered=_APPROVED_ANSWERED_COUNT, abstained=_APPROVED_ABSTAINED_COUNT)
            or len(self.citation_keys) != _APPROVED_CITATION_COUNT
            or len(set(self.citation_keys)) != len(self.citation_keys)
            or len(self.claim_evidence_keys) != _APPROVED_CLAIM_EVIDENCE_COUNT
            or len(set(self.claim_evidence_keys)) != len(self.claim_evidence_keys)
            or any(case_id not in case_ids for case_id, _ in self.citation_keys)
            or any(
                case_id not in case_ids
                for case_id, _, _ in self.claim_evidence_keys
            )
        ):
            raise ValueError("real evaluation population is not the approved identity set")
        return self


class RealMetricResult(DefinitionModel):
    """One deterministic metric without protected content."""

    evaluator_key: str = Field(min_length=1, max_length=128)
    evaluator_version: str = Field(min_length=1, max_length=128)
    status: Literal["measured", "not_applicable", "failed"]
    score: float | None = Field(default=None, ge=0.0, le=1.0)
    eligible_count: int = Field(ge=0)
    matched_count: int = Field(ge=0)
    failure_counts: dict[str, int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_applicability(self) -> "RealMetricResult":
        if self.matched_count > self.eligible_count:
            raise ValueError("matched metric count cannot exceed eligible count")
        if self.status == "not_applicable" and (
            self.score is not None or self.eligible_count != 0
        ):
            raise ValueError("not-applicable metrics require zero eligible values")
        return self


class RealCaseEvaluationResult(DefinitionModel):
    """Protected local result retaining exact dataset/runtime provenance."""

    schema_version: Literal[_CASE_RESULT_SCHEMA] = _CASE_RESULT_SCHEMA
    case_id: str = Field(min_length=1, max_length=256)
    dataset_version: str = Field(min_length=1, max_length=128)
    dataset_sha256: str = Field(pattern=_DIGEST)
    runtime_identity: RealEvaluationRuntimeIdentity
    expected_outcome: Literal["answered", "abstained", "denied"]
    observed_outcome: Literal[
        "answered",
        "abstained",
        "denied",
        "clarification_required",
        "failed",
        "cancelled",
    ]
    expected_citation_count: int = Field(default=0, ge=0)
    expected_claim_evidence_count: int = Field(default=0, ge=0)
    expected_claim_evidence: tuple[tuple[str, str], ...] = ()
    observation_status: Literal["complete", "partial", "unavailable"]
    evaluation_status: Literal["completed", "partial", "unavailable"] = "completed"
    provenance: tuple[RealEvaluationEvidenceProvenance, ...] = ()
    metrics: tuple[RealMetricResult, ...] = ()

    @model_validator(mode="after")
    def validate_quality_availability(self) -> "RealCaseEvaluationResult":
        if self.evaluation_status == "completed" and not self.metrics:
            raise ValueError("completed evaluation requires quality metrics")
        if self.evaluation_status != "completed" and self.metrics:
            raise ValueError("unavailable evaluation cannot carry quality metrics")
        return self

    def metric(self, evaluator_key: str) -> RealMetricResult:
        matches = tuple(
            metric for metric in self.metrics if metric.evaluator_key == evaluator_key
        )
        if len(matches) != 1:
            raise KeyError(evaluator_key)
        return matches[0]


class RealAggregateMetric(DefinitionModel):
    evaluator_key: str = Field(min_length=1, max_length=128)
    evaluator_version: str = Field(min_length=1, max_length=128)
    status: Literal["measured", "not_applicable"]
    score: float | None = Field(default=None, ge=0.0, le=1.0)
    eligible_count: int = Field(ge=0)
    matched_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_counts(self) -> "RealAggregateMetric":
        if self.matched_count > self.eligible_count:
            raise ValueError("aggregate matched count cannot exceed eligible count")
        return self


class RealOutcomeSlice(DefinitionModel):
    expected_outcome: Literal["answered", "abstained", "denied"]
    status: Literal["measured", "not_applicable"]
    score: float | None = Field(default=None, ge=0.0, le=1.0)
    eligible_count: int = Field(ge=0)
    matched_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_counts(self) -> "RealOutcomeSlice":
        if self.matched_count > self.eligible_count:
            raise ValueError("outcome matched count cannot exceed eligible count")
        if self.status == "not_applicable" and (
            self.score is not None or self.eligible_count != 0
        ):
            raise ValueError("not-applicable outcomes require zero eligible cases")
        return self


class RealEvaluationResult(DefinitionModel):
    """Aggregate-only, disclosure-safe output for the later local runner."""

    schema_version: Literal[REAL_EVALUATION_RESULT_SCHEMA] = (
        REAL_EVALUATION_RESULT_SCHEMA
    )
    evaluator_version: Literal[REAL_EVALUATOR_VERSION] = REAL_EVALUATOR_VERSION
    dataset_version: str = Field(min_length=1, max_length=128)
    dataset_sha256: str = Field(pattern=_DIGEST)
    evaluation_status: Literal["completed", "partial", "failed"]
    case_count: int = Field(ge=0)
    unavailable_observation_count: int = Field(ge=0)
    evaluation_failure_count: int = Field(ge=0)
    runtime_identities: tuple[RealEvaluationRuntimeIdentity, ...] = ()
    metrics: tuple[RealAggregateMetric, ...] = ()
    outcome_slices: tuple[RealOutcomeSlice, ...] = Field(min_length=3, max_length=3)

    @model_validator(mode="after")
    def validate_safe_aggregate(self) -> "RealEvaluationResult":
        if (
            self.dataset_version != REAL_R1_DATASET_VERSION
            or self.dataset_sha256 != REAL_R1_DATASET_DIGEST
        ):
            raise ValueError("real evaluation dataset identity mismatch")
        if {item.expected_outcome for item in self.outcome_slices} != {
            "answered",
            "abstained",
            "denied",
        }:
            raise ValueError("real evaluation outcome slices must be complete")
        metric_keys = [item.evaluator_key for item in self.metrics]
        if len(metric_keys) != len(set(metric_keys)):
            raise ValueError("real evaluation aggregate metrics must be unique")
        return self

    def metric(self, evaluator_key: str) -> RealAggregateMetric:
        matches = tuple(
            metric for metric in self.metrics if metric.evaluator_key == evaluator_key
        )
        if len(matches) != 1:
            raise KeyError(evaluator_key)
        return matches[0]

    def outcome_slice(
        self, expected_outcome: Literal["answered", "abstained", "denied"]
    ) -> RealOutcomeSlice:
        return next(
            item
            for item in self.outcome_slices
            if item.expected_outcome == expected_outcome
        )


def aggregate_real_evaluations(
    results: tuple[RealCaseEvaluationResult, ...],
    *,
    evaluation_failure_count: int = 0,
    dataset_version: str | None = None,
    dataset_sha256: str | None = None,
    population: RealEvaluationPopulation | None = None,
) -> RealEvaluationResult:
    """Create a metadata/aggregate-only result without protected case details."""

    if evaluation_failure_count < 0:
        raise ValueError("evaluation_failure_count cannot be negative")
    dataset_identities = {(result.dataset_version, result.dataset_sha256) for result in results}
    if dataset_version is not None or dataset_sha256 is not None:
        if dataset_version is None or dataset_sha256 is None:
            raise ValueError("dataset version and digest must be supplied together")
        dataset_identities.add((dataset_version, dataset_sha256))
    if len(dataset_identities) != 1:
        raise RealEvaluationContractError("real evaluation dataset identity mismatch")
    if not results and evaluation_failure_count == 0:
        raise ValueError("an empty real evaluation requires an evaluation failure")
    dataset_version, dataset_sha256 = next(iter(dataset_identities))

    metric_groups: dict[tuple[str, str], list[RealMetricResult]] = {}
    scored_results = tuple(
        result for result in results if result.evaluation_status == "completed"
    )
    for result in scored_results:
        for metric in result.metrics:
            metric_groups.setdefault(
                (metric.evaluator_key, metric.evaluator_version), []
            ).append(metric)
    aggregates: list[RealAggregateMetric] = []
    for (key, version), metrics in sorted(metric_groups.items()):
        eligible_count = sum(metric.eligible_count for metric in metrics)
        matched_count = sum(metric.matched_count for metric in metrics)
        if eligible_count == 0:
            aggregates.append(
                RealAggregateMetric(
                    evaluator_key=key,
                    evaluator_version=version,
                    status="not_applicable",
                    eligible_count=0,
                    matched_count=0,
                )
            )
            continue
        weighted_score = sum(
            (metric.score or 0.0) * metric.eligible_count
            for metric in metrics
            if metric.status == "measured"
        ) / eligible_count
        aggregates.append(
            RealAggregateMetric(
                evaluator_key=key,
                evaluator_version=version,
                status="measured",
                score=weighted_score,
                eligible_count=eligible_count,
                matched_count=matched_count,
            )
        )

    slices: list[RealOutcomeSlice] = []
    for expected_outcome in ("answered", "abstained", "denied"):
        eligible = tuple(
            result
            for result in scored_results
            if result.expected_outcome == expected_outcome
        )
        matched = sum(
            result.observed_outcome == expected_outcome for result in eligible
        )
        slices.append(
            RealOutcomeSlice(
                expected_outcome=expected_outcome,
                status="measured" if eligible else "not_applicable",
                score=matched / len(eligible) if eligible else None,
                eligible_count=len(eligible),
                matched_count=matched,
            )
        )
    unavailable_count = sum(
        result.evaluation_status != "completed" for result in results
    )
    observed_case_outcomes = {
        result.case_id: result.expected_outcome for result in scored_results
    }
    observed_citation_keys = {
        (result.case_id, item.evidence_id)
        for result in scored_results
        for item in result.provenance
    }
    observed_claim_evidence_keys = {
        (result.case_id, claim_id, evidence_id)
        for result in scored_results
        for claim_id, evidence_id in result.expected_claim_evidence
    }
    population_complete = (
        population is not None
        and population.dataset_version == dataset_version
        and population.dataset_sha256 == dataset_sha256
        and len(observed_case_outcomes) == len(scored_results)
        and observed_case_outcomes == population.case_outcomes
        and observed_citation_keys == set(population.citation_keys)
        and observed_claim_evidence_keys == set(population.claim_evidence_keys)
        and len(scored_results) == _APPROVED_CASE_COUNT
    )
    status: Literal["completed", "partial", "failed"]
    if evaluation_failure_count and not results:
        status = "failed"
    elif evaluation_failure_count or unavailable_count or not population_complete:
        status = "partial"
    else:
        status = "completed"
    return RealEvaluationResult(
        dataset_version=dataset_version,
        dataset_sha256=dataset_sha256,
        evaluation_status=status,
        case_count=len(results),
        unavailable_observation_count=unavailable_count,
        evaluation_failure_count=evaluation_failure_count,
        runtime_identities=tuple(
            value
            for _, value in sorted(
                {
                    result.runtime_identity.model_dump_json(): result.runtime_identity
                    for result in results
                }.items()
            )
        ),
        metrics=tuple(aggregates),
        outcome_slices=tuple(slices),
    )


def evaluate_real_case(
    finalized: FinalizedRealObservation,
    case: RealEvaluationCase,
    *,
    runtime_identity: RealEvaluationRuntimeIdentity,
    recall_k: int = 1,
) -> RealCaseEvaluationResult:
    """Evaluate one frozen production observation against post-joined real gold."""

    if (
        type(finalized) is not FinalizedRealObservation
        or not finalized.is_finalized
    ):
        raise RealEvaluationContractError(
            "real evaluation requires a validated finalized observation"
        )
    if type(case) is not RealEvaluationCase or case.case_id != finalized.observation.case_id:
        raise RealEvaluationContractError("real evaluation case identity mismatch")
    if isinstance(recall_k, bool) or not isinstance(recall_k, int) or recall_k < 1:
        raise ValueError("recall_k must be a positive integer")

    binding_pairs = {
        (binding.source_object_id, binding.source_revision_id)
        for binding in finalized.resolution.bindings
    }
    if any(
        (item.canonical_source_object_id, item.canonical_source_revision_id)
        not in binding_pairs
        for item in case.provenance
    ):
        raise RealEvaluationContractError(
            "real evaluation provenance does not match verified bindings"
        )

    observation = finalized.observation
    expected_runtime_identity = RealEvaluationRuntimeIdentity.from_observation(
        observation,
        model_version=runtime_identity.model_version,
        prompt_version=runtime_identity.prompt_version,
        validation_policy_version=runtime_identity.validation_policy_version,
    )
    if runtime_identity != expected_runtime_identity:
        raise RealEvaluationContractError(
            "real evaluation runtime identity does not match the observation"
        )
    observed_outcome = observation.answer.outcome.value
    if observed_outcome in {"failed", "cancelled"}:
        return RealCaseEvaluationResult(
            case_id=case.case_id,
            dataset_version=case.dataset_version,
            dataset_sha256=case.dataset_sha256,
            runtime_identity=runtime_identity,
            expected_outcome=case.expected_outcome,
            observed_outcome=observed_outcome,
            expected_citation_count=case.expected_citation_count,
            expected_claim_evidence_count=case.expected_claim_evidence_count,
            expected_claim_evidence=tuple(
                (claim.claim_id, evidence_id)
                for claim in case.expected_claims
                for evidence_id in claim.evidence_ids
            ),
            observation_status="unavailable",
            evaluation_status="unavailable",
            provenance=case.provenance,
        )
    if observation.context_response.outcome.value == "partial":
        return RealCaseEvaluationResult(
            case_id=case.case_id,
            dataset_version=case.dataset_version,
            dataset_sha256=case.dataset_sha256,
            runtime_identity=runtime_identity,
            expected_outcome=case.expected_outcome,
            observed_outcome=observed_outcome,
            expected_citation_count=case.expected_citation_count,
            expected_claim_evidence_count=case.expected_claim_evidence_count,
            expected_claim_evidence=tuple(
                (claim.claim_id, evidence_id)
                for claim in case.expected_claims
                for evidence_id in claim.evidence_ids
            ),
            observation_status="partial",
            evaluation_status="partial",
            provenance=case.provenance,
        )
    expected_by_id = {item.evidence_id: item for item in case.provenance}
    expected_ids = set(expected_by_id)
    ranked_ids: list[str] = []
    seen_ranked: set[str] = set()
    for reference in observation.retrieval.references:
        expected = expected_by_id.get(reference.evidence_id)
        try:
            source_object_id = UUID(reference.source_id)
        except (TypeError, ValueError):
            continue
        if (
            expected is None
            or source_object_id != expected.canonical_source_object_id
            or reference.source_revision_id != expected.canonical_source_revision_id
            or reference.workspace_id != observation.question.scope.workspace_id
            or reference.environment_id != observation.question.scope.environment_id
            or reference.evidence_id in seen_ranked
        ):
            continue
        seen_ranked.add(reference.evidence_id)
        ranked_ids.append(reference.evidence_id)

    retrieval_eligible = len(expected_ids) if case.expected_outcome == "answered" else 0
    if retrieval_eligible and observation.retrieval.references and not ranked_ids:
        retrieval_metrics = tuple(
            RealMetricResult(
                evaluator_key=key,
                evaluator_version=RETRIEVAL_METRICS_EVALUATOR_VERSION,
                status="not_applicable",
                eligible_count=0,
                matched_count=0,
                failure_counts={"inaccessible_evidence": 1},
            )
            for key in ("retrieval.recall_at_k", "retrieval.mrr")
        )
    elif retrieval_eligible:
        recall_score, recall_matched, _ = score_recall_at_k(
            ranked_ids, expected_ids, k=recall_k
        )
        mrr_score, first_rank = score_mrr(ranked_ids, expected_ids)
        retrieval_metrics = (
            RealMetricResult(
                evaluator_key="retrieval.recall_at_k",
                evaluator_version=RETRIEVAL_METRICS_EVALUATOR_VERSION,
                status="measured",
                score=recall_score,
                eligible_count=retrieval_eligible,
                matched_count=recall_matched,
            ),
            RealMetricResult(
                evaluator_key="retrieval.mrr",
                evaluator_version=RETRIEVAL_METRICS_EVALUATOR_VERSION,
                status="measured",
                score=mrr_score,
                eligible_count=1,
                matched_count=int(first_rank is not None),
            ),
        )
    else:
        retrieval_metrics = tuple(
            RealMetricResult(
                evaluator_key=key,
                evaluator_version=RETRIEVAL_METRICS_EVALUATOR_VERSION,
                status="not_applicable",
                eligible_count=0,
                matched_count=0,
            )
            for key in ("retrieval.recall_at_k", "retrieval.mrr")
        )

    answer_content = observation.answer.answer
    observed_citations = () if answer_content is None else answer_content.citations
    failures: Counter[str] = Counter()
    valid_evidence_by_citation: dict[UUID, str] = {}
    locator_valid_count = 0
    identity_valid_count = 0
    for citation in observed_citations:
        reference = citation.evidence_reference
        expected = expected_by_id.get(reference.evidence_id)
        identity_valid = expected is not None and (
            reference.source_object_id == expected.canonical_source_object_id
            and reference.source_revision_id == expected.canonical_source_revision_id
            and reference.workspace_id == observation.question.scope.workspace_id
            and reference.environment_id == observation.question.scope.environment_id
        )
        locator_valid = expected is not None and _locator_matches(
            reference.locator.kind,
            reference.locator.value,
            expected.locator,
        )
        identity_valid_count += int(identity_valid)
        locator_valid_count += int(locator_valid)
        if identity_valid and locator_valid:
            valid_evidence_by_citation[citation.citation_id] = reference.evidence_id
        else:
            failures[
                "provenance_mismatch" if not identity_valid else "locator_mismatch"
            ] += 1

    valid_expected_ids = set(valid_evidence_by_citation.values()) & expected_ids
    valid_citation_count = len(valid_evidence_by_citation)
    citation_count = len(observed_citations)
    if case.expected_outcome == "answered":
        precision = valid_citation_count / citation_count if citation_count else 0.0
        recall = len(valid_expected_ids) / len(expected_ids) if expected_ids else None
        locator_validity = locator_valid_count / citation_count if citation_count else 0.0
        identity_validity = identity_valid_count / citation_count if citation_count else 0.0
        citation_status: Literal["measured", "not_applicable", "failed"] = "measured"
    else:
        precision = 1.0 if not observed_citations else 0.0
        recall = None
        locator_validity = 1.0 if not observed_citations else 0.0
        identity_validity = 1.0 if not observed_citations else 0.0
        citation_status = "not_applicable"
        if observed_citations:
            failures["unexpected_content"] += 1

    outcome_matches = answer_outcome_passes(
        observed_outcome=observed_outcome,
        expected_outcome=case.expected_outcome,
        has_claims=bool(answer_content and answer_content.claims),
        has_citations=bool(observed_citations),
    )
    if not outcome_matches:
        failures["outcome_mismatch"] += 1

    expected_claim_count = len(case.expected_claims)
    observed_evidence_by_claim: dict[str, frozenset[str]] = {}
    if answer_content is not None:
        observed_claims_by_text: dict[str, list[Any]] = {}
        for claim in answer_content.claims:
            observed_claims_by_text.setdefault(claim.text, []).append(claim)
        relationships_by_claim: dict[UUID, set[UUID]] = {}
        for relationship in answer_content.relationships:
            relationships_by_claim.setdefault(relationship.claim_id, set()).update(
                relationship.citation_ids
            )
        for expected_claim in case.expected_claims:
            matching_claims = observed_claims_by_text.get(expected_claim.text, [])
            if len(matching_claims) != 1:
                if len(matching_claims) > 1:
                    failures["ambiguous_claim"] += 1
                continue
            observed_claim = matching_claims[0]
            observed_evidence = {
                valid_evidence_by_citation[citation_id]
                for citation_id in relationships_by_claim.get(observed_claim.claim_id, set())
                if citation_id in valid_evidence_by_citation
            }
            observed_evidence_by_claim[expected_claim.claim_id] = frozenset(
                observed_evidence
            )
        expected_texts = {claim.text for claim in case.expected_claims}
        unexpected_claim_count = sum(
            claim.text not in expected_texts for claim in answer_content.claims
        )
        if unexpected_claim_count:
            failures["unexpected_claim"] += unexpected_claim_count
    groundedness_score, supported_claim_count, _, unsupported_claims = score_grounded_claims(
        expected_evidence_by_claim={
            claim.claim_id: frozenset(claim.evidence_ids)
            for claim in case.expected_claims
        },
        observed_evidence_by_claim=observed_evidence_by_claim,
    )
    if unsupported_claims:
        failures["unsupported_claim"] += len(unsupported_claims)

    deterministic_citations_pass = (
        not failures
        and outcome_matches
        and (
            case.expected_outcome != "answered"
            or (
                precision == 1.0
                and recall == 1.0
                and locator_validity == 1.0
                and identity_validity == 1.0
            )
        )
    )
    metrics = (
        *retrieval_metrics,
        RealMetricResult(
            evaluator_key="qa.answer_outcome",
            evaluator_version=CITATION_METRICS_EVALUATOR_VERSION,
            status="measured",
            score=1.0 if outcome_matches else 0.0,
            eligible_count=1,
            matched_count=int(outcome_matches),
        ),
        RealMetricResult(
            evaluator_key="qa.citations",
            evaluator_version=CITATION_METRICS_EVALUATOR_VERSION,
            status="measured" if case.expected_outcome == "answered" else citation_status,
            score=(1.0 if deterministic_citations_pass else 0.0)
            if case.expected_outcome == "answered"
            else None,
            eligible_count=1 if case.expected_outcome == "answered" else 0,
            matched_count=int(deterministic_citations_pass)
            if case.expected_outcome == "answered"
            else 0,
            failure_counts=dict(sorted(failures.items())),
        ),
        RealMetricResult(
            evaluator_key="qa.citation_precision",
            evaluator_version=CITATION_METRICS_EVALUATOR_VERSION,
            status="measured" if case.expected_outcome == "answered" else citation_status,
            score=precision if case.expected_outcome == "answered" else None,
            eligible_count=citation_count if case.expected_outcome == "answered" else 0,
            matched_count=valid_citation_count if case.expected_outcome == "answered" else 0,
            failure_counts=dict(sorted(failures.items())),
        ),
        RealMetricResult(
            evaluator_key="qa.citation_recall",
            evaluator_version=CITATION_METRICS_EVALUATOR_VERSION,
            status="measured" if case.expected_outcome == "answered" else "not_applicable",
            score=recall,
            eligible_count=len(expected_ids),
            matched_count=len(valid_expected_ids),
            failure_counts=dict(sorted(failures.items())),
        ),
        RealMetricResult(
            evaluator_key="qa.locator_validity",
            evaluator_version=CITATION_METRICS_EVALUATOR_VERSION,
            status="measured" if case.expected_outcome == "answered" else citation_status,
            score=locator_validity if case.expected_outcome == "answered" else None,
            eligible_count=citation_count if case.expected_outcome == "answered" else 0,
            matched_count=locator_valid_count if case.expected_outcome == "answered" else 0,
            failure_counts=dict(sorted(failures.items())),
        ),
        RealMetricResult(
            evaluator_key="qa.groundedness",
            evaluator_version=CITATION_METRICS_EVALUATOR_VERSION,
            status="measured" if expected_claim_count else "not_applicable",
            score=groundedness_score,
            eligible_count=expected_claim_count,
            matched_count=supported_claim_count,
            failure_counts=dict(sorted(failures.items())),
        ),
    )
    return RealCaseEvaluationResult(
        case_id=case.case_id,
        dataset_version=case.dataset_version,
        dataset_sha256=case.dataset_sha256,
        runtime_identity=runtime_identity,
        expected_outcome=case.expected_outcome,
        observed_outcome=observed_outcome,
        expected_citation_count=case.expected_citation_count,
        expected_claim_evidence_count=case.expected_claim_evidence_count,
        expected_claim_evidence=tuple(
            (claim.claim_id, evidence_id)
            for claim in case.expected_claims
            for evidence_id in claim.evidence_ids
        ),
        observation_status="complete",
        provenance=case.provenance,
        metrics=metrics,
    )


def _locator_matches(kind: str, value: str, expected: RealLocator) -> bool:
    if expected.kind == "pdf_page":
        return kind == "page" and value == (
            f"page:{expected.page}:{expected.char_start}-{expected.char_end}"
        )
    if expected.kind == "xlsx_cell_range":
        return kind == "sheet_row" and value == (
            f"{expected.sheet}:{expected.row}:{expected.cell_range}"
        )
    path = f"paragraph:{expected.paragraph - 1}"
    return kind == "custom" and value == (
        f"ooxml:{path}:{expected.char_start}-{expected.char_end}"
    )


__all__ = [
    "REAL_EVALUATION_CASE_SCHEMA",
    "REAL_EVALUATION_RESULT_SCHEMA",
    "REAL_EVALUATOR_VERSION",
    "FinalizedRealObservation",
    "RealEvaluationAdapter",
    "RealEvaluationCase",
    "RealEvaluationContractError",
    "RealEvaluationEvidenceProvenance",
    "RealEvaluationPopulation",
    "RealEvaluationRuntimeIdentity",
    "RealGoldCasePort",
    "ValidatedRealGoldProvider",
    "RealCaseEvaluationResult",
    "RealAggregateMetric",
    "RealEvaluationResult",
    "RealMetricResult",
    "RealOutcomeSlice",
    "aggregate_real_evaluations",
    "evaluate_real_case",
]
