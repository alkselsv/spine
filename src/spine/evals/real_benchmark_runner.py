"""Deterministic orchestration for an explicitly selected local real dataset.

The runner owns stage ordering and safe reporting. Canonical readiness and the
gold-blind observer remain injected application boundaries.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from typing import Literal, Protocol
from uuid import UUID

from pydantic import Field, model_validator

from spine.application.corpus_readiness import (
    CanonicalCorpusReadinessService,
    CorpusReadinessRequest,
    CorpusReadinessReport,
    CorpusReadinessStatus,
)
from spine.application.persistence.context import TrustedPersistenceContext
from spine.domain.common import DefinitionModel
from spine.evals.corpus_preparation import corpus_readiness_request_from_bindings
from spine.evals.production_contract import ProductionContractObservation
from spine.evals.real_evaluation import (
    FinalizedRealObservation,
    RealEvaluationAdapter,
    RealEvaluationResult,
    RealGoldCasePort,
    RealEvaluationRuntimeIdentity,
    REAL_EVALUATOR_VERSION,
    aggregate_real_evaluations,
    evaluate_real_case,
)
from spine.evals.real_qa_r1 import (
    REAL_R1_DATASET_DIGEST,
    REAL_R1_DATASET_VERSION,
    LoadedRealDataset,
    RealCaseObservationInput,
    RealRuntimeCaseResolution,
    RealRuntimeMappingManifest,
    RealRuntimeMappingResolver,
    RealRuntimeResolutionError,
    runtime_mapping_manifest_digest,
)


REAL_BENCHMARK_RUN_SCHEMA = "r1-local-benchmark-run:v1"
REAL_BENCHMARK_REPORT_SCHEMA = "r1-local-benchmark-report:v1"
REAL_BENCHMARK_REPORT_V2_SCHEMA = "r1-local-benchmark-report:v2"
REAL_BENCHMARK_REPORT_VERSION = "v1"


class RealBenchmarkContractError(ValueError):
    """The real benchmark inputs or injected boundary violated their contract."""


class RealObserverResult(DefinitionModel):
    """One safe observer disposition; observation data stays local to evaluation."""

    status: Literal["observed", "partial", "unavailable", "denied", "failed"]
    observation: ProductionContractObservation | None = None
    observer_elapsed_ms: int | None = Field(default=None, ge=0)
    worker_execution_elapsed_ns: int | None = Field(default=None, ge=0)
    worker_execution_attempt_id: UUID | None = None
    execution_record_schema_version: Literal[
        "r1-case-execution-record:v1", "r1-case-execution-record:v2"
    ] | None = None
    failure_category: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[a-z][a-z0-9_.:-]{0,127}$",
    )

    def validate_disposition(self) -> "RealObserverResult":
        if self.status == "observed":
            if type(self.observation) is not ProductionContractObservation:
                raise RealBenchmarkContractError("observer did not return a typed observation")
        elif self.status != "partial" and self.observation is not None:
            raise RealBenchmarkContractError("unavailable observer result cannot carry observation")
        if self.execution_record_schema_version == "r1-case-execution-record:v2" and (
            self.observer_elapsed_ms is None
            and self.status == "observed"
        ):
            raise RealBenchmarkContractError("observed v2 result requires observer elapsed time")
        if self.execution_record_schema_version == "r1-case-execution-record:v1" and (
            self.worker_execution_elapsed_ns is not None
            or self.worker_execution_attempt_id is not None
        ):
            raise RealBenchmarkContractError("v1 execution record cannot carry worker timing")
        if (self.worker_execution_elapsed_ns is None) != (
            self.worker_execution_attempt_id is None
        ):
            raise RealBenchmarkContractError("worker timing fields must be paired")
        if self.worker_execution_attempt_id is not None and self.observation is not None:
            if self.worker_execution_attempt_id != self.observation.question.correlation.attempt_id:
                raise RealBenchmarkContractError("worker timing belongs to another attempt")
        return self


class RealBenchmarkObserverPort(Protocol):
    """Gold-blind application seam for one resolved case."""

    async def observe(
        self, *, resolution: RealRuntimeCaseResolution
    ) -> RealObserverResult: ...


class RuntimeVersionPins(DefinitionModel):
    observer_seam_version: str = Field(min_length=1, max_length=128)
    application_version: str = Field(min_length=1, max_length=256)
    projection_version: str = Field(min_length=1, max_length=256)
    model_version: str = Field(min_length=1, max_length=256)
    prompt_version: str = Field(min_length=1, max_length=256)
    validation_policy_version: str = Field(min_length=1, max_length=128)
    projection_kind: str = Field(min_length=1, max_length=64)


class RealBenchmarkRun(DefinitionModel):
    schema_version: Literal["r1-local-benchmark-run:v1"] = REAL_BENCHMARK_RUN_SCHEMA
    run_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    workspace_id: UUID
    environment_id: UUID
    dataset_version: str
    dataset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    mapping_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_pins: RuntimeVersionPins
    runtime_identity_version: Literal[
        "r1-real-evaluation-runtime-identity:v1"
    ] = "r1-real-evaluation-runtime-identity:v1"
    evaluator_version: Literal["v1"] = REAL_EVALUATOR_VERSION
    report_schema_version: str = REAL_BENCHMARK_REPORT_SCHEMA


class RealBenchmarkCaseCounts(DefinitionModel):
    total: int = Field(ge=0)
    observed: int = Field(ge=0)
    partial: int = Field(ge=0)
    unavailable: int = Field(ge=0)
    denied: int = Field(ge=0)
    failed: int = Field(ge=0)
    readiness_blocked: int = Field(ge=0)
    mapping_unavailable: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_total(self) -> "RealBenchmarkCaseCounts":
        counted = sum(
            getattr(self, field)
            for field in (
                "observed",
                "partial",
                "unavailable",
                "denied",
                "failed",
                "readiness_blocked",
                "mapping_unavailable",
            )
        )
        if counted != self.total:
            raise ValueError("real benchmark case counts must sum to total")
        return self


class RealBenchmarkReport(DefinitionModel):
    """Metadata-only report; deliberately has no case/source identity fields."""

    schema_version: Literal["r1-local-benchmark-report:v1"] = REAL_BENCHMARK_REPORT_SCHEMA
    run: RealBenchmarkRun
    structural_validation_status: Literal["passed"]
    production_observation_status: Literal["completed", "partial", "blocked", "unavailable"]
    evaluation_status: Literal["completed", "partial", "failed", "not_run"]
    release_qualification_status: Literal["not_run"] = "not_run"
    readiness_status: Literal["ready", "not_ready", "unavailable", "not_checked"]
    readiness_reason_counts: dict[str, int] = Field(default_factory=dict)
    failure_reason_counts: dict[str, int] = Field(default_factory=dict)
    case_counts: RealBenchmarkCaseCounts
    evaluation: RealEvaluationResult | None = None


class RealBenchmarkExecutionTiming(DefinitionModel):
    """V2 timing metadata without case or source identities."""

    observer_elapsed_ms: int | None = Field(default=None, ge=0)
    worker_execution_elapsed_ns: int | None = Field(default=None, ge=0)
    worker_execution_attempt_id: UUID | None = None

    @model_validator(mode="after")
    def validate_worker_timing(self) -> "RealBenchmarkExecutionTiming":
        if (self.worker_execution_elapsed_ns is None) != (
            self.worker_execution_attempt_id is None
        ):
            raise ValueError("worker timing duration and attempt identity must be paired")
        return self


class RealBenchmarkReportV2(RealBenchmarkReport):
    """Versioned metadata report that retains the observer's two time domains."""

    schema_version: Literal["r1-local-benchmark-report:v2"] = (
        REAL_BENCHMARK_REPORT_V2_SCHEMA
    )
    execution_timings: tuple[RealBenchmarkExecutionTiming, ...] = ()


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
        self, *, context: TrustedPersistenceContext, request: CorpusReadinessRequest
    ) -> CorpusReadinessReport: ...


class RealBenchmarkRunner:
    """Run readiness, observation and evaluation in a fixed, auditable order."""

    def __init__(
        self,
        *,
        mapping: RealRuntimeMappingResolver | _MappingPort,
        readiness: CanonicalCorpusReadinessService | _ReadinessPort,
        trusted_persistence_context: TrustedPersistenceContext,
        observer: RealBenchmarkObserverPort,
        gold: RealGoldCasePort,
        pins: RuntimeVersionPins,
        workspace_selector: UUID,
        environment_selector: UUID,
    ) -> None:
        self._mapping = mapping
        self._readiness = readiness
        self._context = trusted_persistence_context
        self._observer = observer
        self._gold = gold
        self._pins = pins
        self._workspace = workspace_selector
        self._environment = environment_selector
        self._report_schema = getattr(
            observer, "report_schema_version", REAL_BENCHMARK_REPORT_SCHEMA
        )
        if self._report_schema not in {
            REAL_BENCHMARK_REPORT_SCHEMA,
            REAL_BENCHMARK_REPORT_V2_SCHEMA,
        }:
            raise RealBenchmarkContractError("observer report schema version is unsupported")

    async def run(
        self,
        *,
        loaded: LoadedRealDataset,
        manifest: RealRuntimeMappingManifest,
    ) -> RealBenchmarkReport:
        if (
            type(loaded) is not LoadedRealDataset
            or loaded.validation_report.validation_status != "passed"
            or loaded.dataset is None
            or loaded.dataset.version != REAL_R1_DATASET_VERSION
            or loaded.dataset.dataset_integrity_digest != REAL_R1_DATASET_DIGEST
            or loaded.validation_report.dataset_id != loaded.dataset.dataset_id
            or loaded.validation_report.version != loaded.dataset.version
            or loaded.validation_report.digest != loaded.dataset.dataset_integrity_digest
            or loaded.validation_report.case_count != len(loaded.dataset.cases)
        ):
            raise RealBenchmarkContractError("explicit real dataset failed structural validation")
        if type(manifest) is not RealRuntimeMappingManifest:
            raise RealBenchmarkContractError("explicit real mapping manifest is invalid")
        if (
            manifest.dataset_version != loaded.dataset.version
            or manifest.dataset_sha256 != loaded.dataset.dataset_integrity_digest
        ):
            raise RealBenchmarkContractError("mapping manifest does not match selected dataset")

        manifest_digest = runtime_mapping_manifest_digest(manifest)
        run = self._run_identity(loaded, manifest_digest, self._report_schema)
        cases = loaded.dataset.cases
        resolutions: dict[str, RealRuntimeCaseResolution] = {}
        mapping_failures = 0
        failure_reasons: Counter[str] = Counter()
        all_bindings = {}
        conflicting_binding_keys: set[tuple[str, str]] = set()
        for case in cases:
            case_input = RealCaseObservationInput.from_case(
                case,
                dataset_version=loaded.dataset.version,
                dataset_sha256=loaded.dataset.dataset_integrity_digest,
            )
            try:
                resolution = await self._mapping.resolve(
                    manifest=manifest,
                    case=case_input,
                    workspace_selector=self._workspace,
                    environment_selector=self._environment,
                )
            except RealRuntimeResolutionError as exc:
                failure_reasons[exc.category.value] += 1
                mapping_failures += 1
                continue
            except Exception:
                failure_reasons["mapping_unavailable"] += 1
                mapping_failures += 1
                continue
            if type(resolution) is not RealRuntimeCaseResolution:
                failure_reasons["mapping_invalid"] += 1
                mapping_failures += 1
                continue
            binding_by_dataset_revision = {
                item.dataset_revision_id: item for item in resolution.bindings
            }
            if (
                resolution.manifest_sha256 != manifest_digest
                or resolution.request.case_id != case.case_id
                or resolution.request.question != case.question
                or resolution.request.workspace_id != self._workspace
                or resolution.request.environment_id != self._environment
                or {item.dataset_source_id for item in resolution.bindings}
                != set(case.source_ids)
                or {item.dataset_revision_id for item in resolution.bindings}
                != set(case.source_revision_ids)
                or tuple(
                    binding_by_dataset_revision[item].source_revision_id
                    for item in case.available_source_revision_ids
                    if item in binding_by_dataset_revision
                )
                != resolution.request.available_revision_ids
            ):
                failure_reasons["mapping_invalid"] += 1
                mapping_failures += 1
                continue
            resolutions[case.case_id] = resolution
            for binding in resolution.bindings:
                key = (binding.dataset_source_id, binding.dataset_revision_id)
                prior = all_bindings.setdefault(key, binding)
                if prior != binding:
                    conflicting_binding_keys.add(key)
        if conflicting_binding_keys:
            failure_reasons["mapping_conflict"] += len(conflicting_binding_keys)
            for case_id, resolution in tuple(resolutions.items()):
                if any(
                    (binding.dataset_source_id, binding.dataset_revision_id)
                    in conflicting_binding_keys
                    for binding in resolution.bindings
                ):
                    del resolutions[case_id]
                    mapping_failures += 1
            for key in conflicting_binding_keys:
                all_bindings.pop(key, None)

        readiness_report: CorpusReadinessReport | None = None
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
                if (
                    request.workspace_id != self._workspace
                    or request.environment_id != self._environment
                ):
                    raise RealBenchmarkContractError("readiness scope does not match selectors")
                readiness_report = await self._readiness.inspect(
                    context=self._context,
                    request=request,
                )
                if type(readiness_report) is not CorpusReadinessReport:
                    raise RealBenchmarkContractError("readiness returned an invalid report")
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
                    for item in readiness_report.revisions
                }
                if (
                    readiness_report.workspace_id != request.workspace_id
                    or readiness_report.environment_id != request.environment_id
                    or readiness_report.projection_kind != request.projection_kind
                    or reported_revisions != expected_revisions
                    or len(readiness_report.revisions) != len(expected_revisions)
                ):
                    raise RealBenchmarkContractError(
                        "readiness report does not match the requested canonical scope"
                    )
                readiness_status = readiness_report.status.value
                for item in readiness_report.revisions:
                    readiness_reasons[item.reason.value] += 1
                    if item.status is CorpusReadinessStatus.READY:
                        ready_revisions.add(item.source_revision_id)
            except Exception:
                readiness_status = "unavailable"
                readiness_reasons["readiness_unavailable"] += 1
                failure_reasons["readiness_unavailable"] += 1

        counts: Counter[str] = Counter()
        scored = []
        execution_timings: list[RealBenchmarkExecutionTiming] = []
        failures = 0
        for case in cases:
            resolution = resolutions.get(case.case_id)
            if resolution is None:
                counts["mapping_unavailable"] += 1
                continue
            required = set(resolution.request.available_revision_ids)
            if not required <= ready_revisions:
                counts["readiness_blocked"] += 1
                continue
            try:
                result = await self._observer.observe(resolution=resolution)
                if type(result) is not RealObserverResult:
                    raise RealBenchmarkContractError("observer result is invalid")
                result.validate_disposition()
                if self._report_schema == REAL_BENCHMARK_REPORT_V2_SCHEMA:
                    if result.execution_record_schema_version != "r1-case-execution-record:v2":
                        raise RealBenchmarkContractError(
                            "v2 report observer did not return a v2 execution record"
                        )
                    execution_timings.append(
                        RealBenchmarkExecutionTiming(
                            observer_elapsed_ms=result.observer_elapsed_ms,
                            worker_execution_elapsed_ns=result.worker_execution_elapsed_ns,
                            worker_execution_attempt_id=result.worker_execution_attempt_id,
                        )
                    )
                elif result.execution_record_schema_version is not None:
                    if result.execution_record_schema_version != "r1-case-execution-record:v1":
                        raise RealBenchmarkContractError(
                            "v1 report cannot consume this execution record version"
                        )
            except Exception:
                counts["failed"] += 1
                failures += 1
                failure_reasons["observer_failed"] += 1
                continue
            counts[result.status] += 1
            if result.failure_category is not None:
                failure_reasons[result.failure_category] += 1
            if result.observation is None:
                continue
            try:
                finalized = FinalizedRealObservation.from_observation(
                    observation=result.observation,
                    resolution=resolution,
                )
                evaluation_case = RealEvaluationAdapter().join_gold(
                    finalized,
                    gold=self._gold,
                )
                identity = RealEvaluationRuntimeIdentity.from_observation(
                    finalized.observation,
                    model_version=self._pins.model_version,
                    prompt_version=self._pins.prompt_version,
                    validation_policy_version=self._pins.validation_policy_version,
                )
                scored.append(
                    evaluate_real_case(
                        finalized, evaluation_case, runtime_identity=identity
                    )
                )
            except Exception:
                failures += 1
                failure_reasons["evaluation_failed"] += 1

        evaluation: RealEvaluationResult | None = None
        if scored or failures:
            try:
                evaluation = aggregate_real_evaluations(
                    tuple(scored),
                    evaluation_failure_count=failures,
                    dataset_version=loaded.dataset.version,
                    dataset_sha256=loaded.dataset.dataset_integrity_digest,
                    population=self._gold.population,
                )
            except Exception:
                failures += 1

        total = len(cases)
        observer_attempts = sum(
            counts[key]
            for key in ("observed", "partial", "unavailable", "denied", "failed")
        )
        production_status = (
            "blocked"
            if observer_attempts == 0 and total
            else "completed"
            if counts["observed"] == total
            else "partial"
            if observer_attempts or counts["readiness_blocked"] or mapping_failures
            else "unavailable"
        )
        eval_status = (
            evaluation.evaluation_status
            if evaluation is not None
            else "partial"
            if observer_attempts
            else "not_run"
        )
        report_values = dict(
            run=run,
            structural_validation_status="passed",
            production_observation_status=production_status,
            evaluation_status=eval_status,
            readiness_status=readiness_status,
            readiness_reason_counts=dict(sorted(readiness_reasons.items())),
            failure_reason_counts=dict(sorted(failure_reasons.items())),
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
        if self._report_schema == REAL_BENCHMARK_REPORT_V2_SCHEMA:
            return RealBenchmarkReportV2(
                **report_values,
                execution_timings=tuple(execution_timings),
            )
        return RealBenchmarkReport(**report_values)

    def _run_identity(
        self,
        loaded: LoadedRealDataset,
        mapping_digest: str,
        report_schema_version: str,
    ) -> RealBenchmarkRun:
        identity = {
            "workspace_id": str(self._workspace),
            "environment_id": str(self._environment),
            "dataset_version": loaded.dataset.version,
            "dataset_sha256": loaded.dataset.dataset_integrity_digest,
            "mapping_sha256": mapping_digest,
            "pins": self._pins.model_dump(mode="json"),
            "evaluator_version": REAL_EVALUATOR_VERSION,
            "report_schema_version": report_schema_version,
        }
        run_id = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return RealBenchmarkRun(
            run_id=run_id,
            workspace_id=self._workspace,
            environment_id=self._environment,
            dataset_version=loaded.dataset.version,
            dataset_sha256=loaded.dataset.dataset_integrity_digest,
            mapping_sha256=mapping_digest,
            runtime_pins=self._pins,
            evaluator_version=REAL_EVALUATOR_VERSION,
            report_schema_version=report_schema_version,
        )


__all__ = [
    "REAL_BENCHMARK_REPORT_SCHEMA",
    "REAL_BENCHMARK_REPORT_V2_SCHEMA",
    "REAL_BENCHMARK_REPORT_VERSION",
    "REAL_BENCHMARK_RUN_SCHEMA",
    "RealBenchmarkCaseCounts",
    "RealBenchmarkExecutionTiming",
    "RealBenchmarkContractError",
    "RealBenchmarkObserverPort",
    "RealBenchmarkReport",
    "RealBenchmarkReportV2",
    "RealBenchmarkRun",
    "RealBenchmarkRunner",
    "RealObserverResult",
    "RuntimeVersionPins",
]
