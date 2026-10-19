from __future__ import annotations

from uuid import UUID

import pytest

from spine.evals.production_contract import (
    CASE_EXECUTION_RECORD_V2_SCHEMA,
    CaseExecutionDisposition,
    CaseExecutionRecordV2,
)
from spine.evals.real_benchmark_production import (
    RealBenchmarkProductionObserver,
)
from spine.evals.real_benchmark_runner import RealObserverResult
from spine.evals.real_benchmark_runner import (
    REAL_BENCHMARK_REPORT_V2_SCHEMA,
    RealBenchmarkRunner,
    RuntimeVersionPins,
)
from spine.evals.qa_r1 import load_r1_dataset
from tests.evals.test_production_contract import _safe_observation
from tests.evals.test_real_benchmark_runner import (
    _readiness_report,
    _readiness_request,
    _scenario,
)
from spine.application.corpus_readiness import CorpusReadinessReason, CorpusReadinessStatus


@pytest.mark.asyncio
async def test_production_adapter_returns_validated_observation_and_distinct_v2_timings() -> None:
    _loaded, _manifest, resolution = _scenario()
    source_case = load_r1_dataset().cases[1]
    case = source_case.model_copy(
        update={
            "case_id": resolution.request.case_id,
            "question": resolution.request.question,
            "workspace_id": resolution.request.workspace_id,
            "environment_id": resolution.request.environment_id,
            "principal_id": resolution.request.principal_id,
            "available_revision_ids": resolution.request.available_revision_ids,
        }
    )
    observation = _safe_observation(case)
    attempt_id = observation.question.correlation.attempt_id
    record = CaseExecutionRecordV2(
        case_id=resolution.request.case_id,
        workspace_id=resolution.request.workspace_id,
        environment_id=resolution.request.environment_id,
        disposition=CaseExecutionDisposition.OBSERVED,
        observation=observation,
        correlation_id=(
            observation.question.correlation.correlation_id
            or observation.question.correlation.trace_id
        ),
        observer_elapsed_ms=12_345,
        worker_execution_elapsed_ns=678_901,
        worker_execution_attempt_id=attempt_id,
    )

    class Observer:
        def __init__(self) -> None:
            self.request = None

        async def observe_v2(self, *, request):
            self.request = request
            return record

    observer = Observer()
    adapter = RealBenchmarkProductionObserver(observer=observer)

    result = await adapter.observe(resolution=resolution)

    assert observer.request is resolution.request
    assert not hasattr(observer.request, "expected_answer")
    assert result == RealObserverResult(
        status="observed",
        observation=observation,
        observer_elapsed_ms=12_345,
        worker_execution_elapsed_ns=678_901,
        worker_execution_attempt_id=attempt_id,
        execution_record_schema_version=CASE_EXECUTION_RECORD_V2_SCHEMA,
    )


@pytest.mark.asyncio
async def test_production_adapter_maps_safe_non_success_disposition_without_observation() -> None:
    _loaded, _manifest, resolution = _scenario()
    record = CaseExecutionRecordV2(
        case_id=resolution.request.case_id,
        workspace_id=resolution.request.workspace_id,
        environment_id=resolution.request.environment_id,
        disposition=CaseExecutionDisposition.UNAVAILABLE,
        failure_category="runtime_unavailable",
        correlation_id=UUID(int=44),
    )

    class Observer:
        async def observe_v2(self, *, request):
            return record

    result = await RealBenchmarkProductionObserver(observer=Observer()).observe(
        resolution=resolution
    )

    assert result.status == "unavailable"
    assert result.observation is None
    assert result.failure_category == "runtime_unavailable"
    assert result.observer_elapsed_ms is None
    assert result.worker_execution_elapsed_ns is None


@pytest.mark.asyncio
async def test_production_adapter_rejects_record_scope_mismatch_without_exposing_payload() -> None:
    _loaded, _manifest, resolution = _scenario()
    record = CaseExecutionRecordV2(
        case_id=resolution.request.case_id,
        workspace_id=resolution.request.workspace_id,
        environment_id=resolution.request.environment_id,
        disposition=CaseExecutionDisposition.UNAVAILABLE,
        failure_category="runtime_unavailable",
        correlation_id=UUID(int=44),
    ).model_copy(update={"workspace_id": UUID(int=999)})

    class Observer:
        async def observe_v2(self, *, request):
            return record

    with pytest.raises(ValueError, match="production observer identity does not match request"):
        await RealBenchmarkProductionObserver(observer=Observer()).observe(
            resolution=resolution
        )


@pytest.mark.asyncio
async def test_runner_writes_v2_report_with_nullable_timings_and_no_case_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded, manifest, resolution = _scenario()
    monkeypatch.setattr(
        "spine.evals.real_benchmark_runner.corpus_readiness_request_from_bindings",
        lambda bindings, *, projection_kind: _readiness_request(
            bindings, projection_kind=projection_kind
        ),
    )

    class Mapping:
        async def resolve(self, **_kwargs):
            return resolution

    class Readiness:
        async def inspect(self, **_kwargs):
            return _readiness_report(CorpusReadinessStatus.READY, CorpusReadinessReason.READY)

    class Observer:
        report_schema_version = REAL_BENCHMARK_REPORT_V2_SCHEMA

        async def observe(self, *, resolution):
            assert resolution is not None
            return RealObserverResult(
                status="unavailable",
                failure_category="runtime_unavailable",
                observer_elapsed_ms=12_345,
                worker_execution_elapsed_ns=678_901,
                worker_execution_attempt_id=UUID(int=55),
                execution_record_schema_version=CASE_EXECUTION_RECORD_V2_SCHEMA,
            )

    runner = RealBenchmarkRunner(
        mapping=Mapping(),
        readiness=Readiness(),
        trusted_persistence_context=object(),
        observer=Observer(),
        gold=object(),
        pins=RuntimeVersionPins(
            observer_seam_version="observer:v2",
            application_version="app:v1",
            projection_version="projection:v1",
            model_version="model:v1",
            prompt_version="prompt:v1",
            validation_policy_version="policy:v1",
            projection_kind="qa",
        ),
        workspace_selector=resolution.request.workspace_id,
        environment_selector=resolution.request.environment_id,
    )

    report = await runner.run(loaded=loaded, manifest=manifest)
    serialized = report.model_dump_json()

    assert report.schema_version == REAL_BENCHMARK_REPORT_V2_SCHEMA
    assert report.run.report_schema_version == REAL_BENCHMARK_REPORT_V2_SCHEMA
    assert len(report.execution_timings) == 1
    assert report.execution_timings[0].observer_elapsed_ms == 12_345
    assert report.execution_timings[0].worker_execution_elapsed_ns == 678_901
    assert report.execution_timings[0].worker_execution_attempt_id == UUID(int=55)
    for forbidden in ("case:opaque-3", "source:opaque-7", "question", "answer"):
        assert forbidden not in serialized
