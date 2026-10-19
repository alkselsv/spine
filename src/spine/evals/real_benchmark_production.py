"""Connect the deterministic real benchmark to the production observer."""

from __future__ import annotations

from typing import Protocol

from spine.evals.production_contract import (
    CASE_EXECUTION_RECORD_V2_SCHEMA,
    CaseExecutionDisposition,
    CaseExecutionRecordV2,
    ProductionContractCaseRequest,
    ProductionContractObserver,
)
from spine.evals.real_benchmark_runner import (
    REAL_BENCHMARK_REPORT_V2_SCHEMA,
    RealBenchmarkContractError,
    RealObserverResult,
)
from spine.evals.real_qa_r1 import RealRuntimeCaseResolution


class _ProductionObserverPort(Protocol):
    async def observe_v2(
        self, *, request: ProductionContractCaseRequest
    ) -> CaseExecutionRecordV2: ...


class RealBenchmarkProductionObserver:
    """Adapt validated production execution records to the R1 runner contract.

    The observer receives only the resolver's gold-blind request. This adapter
    never invokes retrieval, generation, persistence, or disclosure itself.
    """

    report_schema_version = REAL_BENCHMARK_REPORT_V2_SCHEMA

    def __init__(self, *, observer: ProductionContractObserver | _ProductionObserverPort) -> None:
        self._observer = observer

    async def observe(
        self, *, resolution: RealRuntimeCaseResolution
    ) -> RealObserverResult:
        if type(resolution) is not RealRuntimeCaseResolution:
            raise RealBenchmarkContractError("production benchmark resolution is invalid")
        request = resolution.request
        record = await self._observer.observe_v2(request=request)
        if type(record) is not CaseExecutionRecordV2:
            raise RealBenchmarkContractError("production observer did not return a v2 record")
        record = CaseExecutionRecordV2.model_validate(record.model_dump())
        if record.schema_version != CASE_EXECUTION_RECORD_V2_SCHEMA:
            raise RealBenchmarkContractError("production observer record schema is unsupported")
        if (
            record.case_id != request.case_id
            or record.workspace_id != request.workspace_id
            or record.environment_id != request.environment_id
        ):
            raise RealBenchmarkContractError("production observer identity does not match request")

        observation = record.observation
        if observation is not None:
            observation.validate()
            if (
                observation.case_id != request.case_id
                or observation.question.subject.text != request.question
                or observation.question.scope.workspace_id != request.workspace_id
                or observation.question.scope.environment_id != request.environment_id
                or (
                    observation.context_bundle is not None
                    and any(
                        evidence.source_revision_id not in request.available_revision_ids
                        for evidence in observation.context_bundle.evidence_references
                    )
                )
            ):
                raise RealBenchmarkContractError(
                    "production observer observation does not match resolved request"
                )

        if record.disposition is CaseExecutionDisposition.OBSERVED:
            status = "observed"
        elif record.disposition is CaseExecutionDisposition.DENIED:
            status = "denied"
        elif record.disposition is CaseExecutionDisposition.UNAVAILABLE:
            status = "unavailable"
        elif record.failure_category is not None and record.failure_category.value == "partial_failure":
            status = "partial"
        else:
            status = "failed"

        return RealObserverResult(
            status=status,
            observation=observation,
            failure_category=(
                record.failure_category.value if record.failure_category is not None else None
            ),
            observer_elapsed_ms=record.observer_elapsed_ms,
            worker_execution_elapsed_ns=record.worker_execution_elapsed_ns,
            worker_execution_attempt_id=record.worker_execution_attempt_id,
            execution_record_schema_version=record.schema_version,
        )


__all__ = ["RealBenchmarkProductionObserver"]
