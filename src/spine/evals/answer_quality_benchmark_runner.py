"""Gold-blind production runner for the approved answer-quality dataset."""

from __future__ import annotations

from collections import Counter
import hashlib
import json
from typing import Literal, Protocol
from uuid import UUID

from pydantic import Field

from spine.application.corpus_readiness import (
    CanonicalCorpusReadinessService,
    CorpusReadinessRequest,
    CorpusReadinessReport,
    CorpusReadinessStatus,
)
from spine.application.persistence.context import TrustedPersistenceContext
from spine.application.production_qa_composition import ProductionQAACompositionEvidence
from spine.domain.common import DefinitionModel
from spine.evals.answer_quality_dataset import (
    LoadedAnswerQualityDataset,
    answer_quality_observation_input,
)
from spine.evaluations.answer_quality import (
    AnswerQualityEvaluationAdapter,
    AnswerQualityEvaluationReport,
    AnswerQualityGoldPort,
    AnswerQualityJudgePort,
    aggregate_answer_quality_evaluations,
    evaluate_answer_quality_case,
)
from spine.evals.corpus_preparation import corpus_readiness_request_from_bindings
from spine.evals.real_benchmark_runner import (
    RealBenchmarkCaseCounts,
    RealBenchmarkContractError,
    RealBenchmarkObserverPort,
    RuntimeVersionPins,
)
from spine.evals.real_evaluation import FinalizedRealObservation
from spine.evals.real_qa_r1 import (
    RealCaseObservationInput,
    RealRuntimeCaseResolution,
    RealRuntimeMappingManifest,
    RealRuntimeMappingResolver,
    RealRuntimeResolutionError,
    runtime_mapping_manifest_digest,
)


ANSWER_QUALITY_BENCHMARK_REPORT_SCHEMA = "answer-quality-benchmark-report:v1"


class _MappingPort(Protocol):
    async def resolve(
        self,
        *,
        manifest: RealRuntimeMappingManifest,
        case: RealCaseObservationInput,
        workspace_selector: UUID,
        environment_selector: UUID,
    ) -> RealRuntimeCaseResolution: ...


class _ReadinessPort(Protocol):
    async def inspect(
        self,
        *,
        context: TrustedPersistenceContext,
        request: CorpusReadinessRequest,
    ) -> CorpusReadinessReport: ...


class AnswerQualityBenchmarkReport(DefinitionModel):
    """Metadata-only report for one answer-quality production run."""

    schema_version: Literal["answer-quality-benchmark-report:v1"] = (
        ANSWER_QUALITY_BENCHMARK_REPORT_SCHEMA
    )
    run_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    dataset_version: str = Field(min_length=1, max_length=128)
    dataset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    mapping_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    workspace_id: UUID
    environment_id: UUID
    runtime_pins: RuntimeVersionPins
    structural_validation_status: Literal["passed"] = "passed"
    production_observation_status: Literal[
        "completed", "partial", "blocked", "unavailable"
    ]
    evaluation_status: Literal["completed", "partial", "not_run"]
    readiness_status: Literal["ready", "not_ready", "unavailable", "not_checked"]
    readiness_reason_counts: dict[str, int] = Field(default_factory=dict)
    failure_reason_counts: dict[str, int] = Field(default_factory=dict)
    case_counts: RealBenchmarkCaseCounts
    evaluation: AnswerQualityEvaluationReport | None = None
    release_qualification_status: Literal["not_run"] = "not_run"


class AnswerQualityBenchmarkRunner:
    """Map, preflight, observe and score answer-only cases in fixed order."""

    def __init__(
        self,
        *,
        mapping: RealRuntimeMappingResolver | _MappingPort,
        readiness: CanonicalCorpusReadinessService | _ReadinessPort,
        trusted_persistence_context: TrustedPersistenceContext,
        observer: RealBenchmarkObserverPort,
        gold: AnswerQualityGoldPort,
        judge: AnswerQualityJudgePort,
        pins: RuntimeVersionPins,
        workspace_selector: UUID,
        environment_selector: UUID,
    ) -> None:
        self._mapping = mapping
        self._readiness = readiness
        self._context = trusted_persistence_context
        self._observer = observer
        self._gold = gold
        self._judge = judge
        self._pins = pins
        self._workspace = workspace_selector
        self._environment = environment_selector
        self._last_report: AnswerQualityBenchmarkReport | None = None

    @property
    def supports_real_qualification(self) -> bool:
        from spine.evals.real_benchmark_production import RealBenchmarkProductionObserver

        return (
            type(self._observer) is RealBenchmarkProductionObserver
            and self._observer.uses_durable_production_flow
        )

    @property
    def production_qualification_evidence(
        self,
    ) -> ProductionQAACompositionEvidence | None:
        if not self.supports_real_qualification:
            return None
        return self._observer._observer.composition.real_qualification_evidence  # type: ignore[attr-defined]

    def owns_completed_report(self, report: object) -> bool:
        return self._last_report is report and self.supports_real_qualification

    async def run(
        self,
        *,
        loaded: LoadedAnswerQualityDataset,
        manifest: RealRuntimeMappingManifest,
    ) -> AnswerQualityBenchmarkReport:
        self._validate_inputs(loaded, manifest)
        dataset = loaded.dataset
        manifest_digest = runtime_mapping_manifest_digest(manifest)
        resolutions: dict[str, RealRuntimeCaseResolution] = {}
        all_bindings: dict[tuple[str, str], object] = {}
        conflicting_binding_keys: set[tuple[str, str]] = set()
        counts: Counter[str] = Counter()
        failures: Counter[str] = Counter()

        for case in dataset.cases:
            case_input = answer_quality_observation_input(case, dataset=dataset)
            try:
                resolution = await self._mapping.resolve(
                    manifest=manifest,
                    case=case_input,
                    workspace_selector=self._workspace,
                    environment_selector=self._environment,
                )
            except RealRuntimeResolutionError as exc:
                failures[exc.category.value] += 1
                counts["mapping_unavailable"] += 1
                continue
            except Exception:
                failures["mapping_unavailable"] += 1
                counts["mapping_unavailable"] += 1
                continue
            if (
                type(resolution) is not RealRuntimeCaseResolution
                or resolution.manifest_sha256 != manifest_digest
                or resolution.request.case_id != case.case_id
                or resolution.request.question != case.question
                or resolution.request.workspace_id != self._workspace
                or resolution.request.environment_id != self._environment
                or {item.dataset_source_id for item in resolution.bindings}
                != set(case.retrieval_source_ids)
                or {item.dataset_revision_id for item in resolution.bindings}
                != set(case.retrieval_source_revision_ids)
            ):
                failures["mapping_invalid"] += 1
                counts["mapping_unavailable"] += 1
                continue
            for binding in resolution.bindings:
                key = (binding.dataset_source_id, binding.dataset_revision_id)
                prior = all_bindings.setdefault(key, binding)
                if prior != binding:
                    conflicting_binding_keys.add(key)
            resolutions[case.case_id] = resolution

        if conflicting_binding_keys:
            failures["mapping_conflict"] += len(conflicting_binding_keys)
            for case_id, resolution in tuple(resolutions.items()):
                if any(
                    (binding.dataset_source_id, binding.dataset_revision_id)
                    in conflicting_binding_keys
                    for binding in resolution.bindings
                ):
                    del resolutions[case_id]
                    counts["mapping_unavailable"] += 1
            for key in conflicting_binding_keys:
                all_bindings.pop(key, None)

        readiness_status: Literal[
            "ready", "not_ready", "unavailable", "not_checked"
        ] = "not_checked"
        readiness_reasons: Counter[str] = Counter()
        ready_revisions: set[UUID] = set()
        if all_bindings:
            try:
                request = corpus_readiness_request_from_bindings(
                    tuple(all_bindings.values()),
                    projection_kind=self._pins.projection_kind,
                )
                report = await self._readiness.inspect(
                    context=self._context,
                    request=request,
                )
                expected_revisions = {
                    (
                        item.dataset_source_id,
                        item.dataset_revision_id,
                        item.workspace_id,
                        item.environment_id,
                        item.source_object_id,
                        item.source_revision_id,
                        item.canonical_acceptance_id,
                    )
                    for item in request.revisions
                }
                reported_revisions = {
                    (
                        item.dataset_source_id,
                        item.dataset_revision_id,
                        item.workspace_id,
                        item.environment_id,
                        item.source_object_id,
                        item.source_revision_id,
                        item.canonical_acceptance_id,
                    )
                    for item in report.revisions
                }
                if (
                    type(report) is not CorpusReadinessReport
                    or report.workspace_id != self._workspace
                    or report.environment_id != self._environment
                    or report.projection_kind != request.projection_kind
                    or reported_revisions != expected_revisions
                    or len(report.revisions) != len(expected_revisions)
                ):
                    raise RealBenchmarkContractError(
                        "answer-quality readiness report does not match request"
                    )
                readiness_status = report.status.value
                for item in report.revisions:
                    readiness_reasons[item.reason.value] += 1
                    if item.status is CorpusReadinessStatus.READY:
                        ready_revisions.add(item.source_revision_id)
            except Exception:
                readiness_status = "unavailable"
                readiness_reasons["readiness_unavailable"] += 1
                failures["readiness_unavailable"] += 1

        evaluations = []
        for case in dataset.cases:
            resolution = resolutions.get(case.case_id)
            if resolution is None:
                continue
            if not set(resolution.request.available_revision_ids) <= ready_revisions:
                counts["readiness_blocked"] += 1
                continue
            try:
                observed = await self._observer.observe(resolution=resolution)
                observed.validate_disposition()
            except Exception:
                counts["failed"] += 1
                failures["observer_failed"] += 1
                continue
            counts[observed.status] += 1
            if observed.failure_category is not None:
                failures[observed.failure_category] += 1
            if observed.observation is None:
                continue
            try:
                finalized = FinalizedRealObservation.from_observation(
                    observation=observed.observation,
                    resolution=resolution,
                )
                evaluation_case = AnswerQualityEvaluationAdapter().join_gold(
                    finalized,
                    gold=self._gold,
                )
                output = finalized.observation.answer
                answer_text = output.answer.text if output.answer is not None else None
                cited_revisions = (
                    tuple(
                        item.evidence_reference.source_revision_id
                        for item in output.answer.citations
                    )
                    if output.answer is not None
                    else ()
                )
                evaluations.append(
                    await evaluate_answer_quality_case(
                        evaluation_case,
                        observed_answer=answer_text,
                        observed_outcome=output.outcome.value,
                        cited_source_revision_ids=cited_revisions,
                        judge=self._judge,
                    )
                )
            except Exception:
                failures["evaluation_failed"] += 1

        evaluation = (
            aggregate_answer_quality_evaluations(
                tuple(evaluations),
                dataset_version=dataset.version,
                dataset_sha256=dataset.sha256,
                expected_case_count=len(dataset.cases),
            )
            if evaluations
            else None
        )
        total = len(dataset.cases)
        attempted = sum(
            counts[key]
            for key in ("observed", "partial", "unavailable", "denied", "failed")
        )
        production_status: Literal["completed", "partial", "blocked", "unavailable"] = (
            "completed"
            if counts["observed"] == total
            else "blocked"
            if attempted == 0 and (counts["readiness_blocked"] or counts["mapping_unavailable"])
            else "partial"
            if attempted
            else "unavailable"
        )
        result = AnswerQualityBenchmarkReport(
            run_id=self._run_id(dataset.version, dataset.sha256, manifest_digest),
            dataset_version=dataset.version,
            dataset_sha256=dataset.sha256,
            mapping_sha256=manifest_digest,
            workspace_id=self._workspace,
            environment_id=self._environment,
            runtime_pins=self._pins,
            production_observation_status=production_status,
            evaluation_status=(
                evaluation.evaluation_status if evaluation is not None else "not_run"
            ),
            readiness_status=readiness_status,
            readiness_reason_counts=dict(sorted(readiness_reasons.items())),
            failure_reason_counts=dict(sorted(failures.items())),
            case_counts=RealBenchmarkCaseCounts(
                total=total,
                observed=counts["observed"],
                partial=counts["partial"],
                unavailable=counts["unavailable"],
                denied=counts["denied"],
                failed=counts["failed"],
                readiness_blocked=counts["readiness_blocked"],
                mapping_unavailable=counts["mapping_unavailable"],
            ),
            evaluation=evaluation,
        )
        self._last_report = result
        return result

    def _validate_inputs(
        self,
        loaded: LoadedAnswerQualityDataset,
        manifest: RealRuntimeMappingManifest,
    ) -> None:
        if (
            type(loaded) is not LoadedAnswerQualityDataset
            or loaded.validation_report.validation_status != "passed"
            or loaded.validation_report.dataset_version != loaded.dataset.version
            or loaded.validation_report.dataset_sha256 != loaded.dataset.sha256
            or loaded.validation_report.case_count != len(loaded.dataset.cases)
        ):
            raise RealBenchmarkContractError(
                "answer-quality dataset failed structural validation"
            )
        if (
            type(manifest) is not RealRuntimeMappingManifest
            or manifest.dataset_version != loaded.dataset.version
            or manifest.dataset_sha256 != loaded.dataset.sha256
        ):
            raise RealBenchmarkContractError(
                "answer-quality mapping manifest does not match dataset"
            )

    def _run_id(self, version: str, digest: str, mapping_digest: str) -> str:
        payload = {
            "dataset_version": version,
            "dataset_sha256": digest,
            "mapping_sha256": mapping_digest,
            "workspace_id": str(self._workspace),
            "environment_id": str(self._environment),
            "runtime_pins": self._pins.model_dump(mode="json"),
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()


__all__ = [
    "ANSWER_QUALITY_BENCHMARK_REPORT_SCHEMA",
    "AnswerQualityBenchmarkReport",
    "AnswerQualityBenchmarkRunner",
]
