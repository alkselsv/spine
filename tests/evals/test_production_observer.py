from __future__ import annotations

from collections.abc import Iterator
from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.evals.production_contract import (
    CaseExecutionDisposition,
    CaseExecutionFailureCategory,
    CaseExecutionRecordV2,
    ProductionContractApplicationFlow,
    ProductionContractAuthorizationDenied,
    ProductionContractCaseRequest,
    ProductionContractObservation,
    ProductionContractObserver,
    ProductionContractFlowResult,
    ProductionContractPartialFailure,
    ProductionContractUnavailable,
)
from spine.evals.offline_runner import OfflineEvaluationRunner
from spine.evals.qa_r1 import load_r1_dataset
from tests.evals.test_production_contract import _safe_observation
from tests.evals.test_unauthorized_retrieval_safety import (
    UNAUTHORIZED_RETRIEVAL_SAFETY_SEAM,
    _allowed_observation,
    _request as _allowed_request,
)


def _request() -> ProductionContractCaseRequest:
    return ProductionContractCaseRequest.from_case(load_r1_dataset().cases[1])


class _Clock:
    def __init__(self, values: Iterator[float]) -> None:
        self._values = iter(values)

    def __call__(self) -> float:
        return next(self._values)


class _Flow(ProductionContractApplicationFlow):
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls = 0

    async def execute(self, request: ProductionContractCaseRequest) -> object:
        self.calls += 1
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


@pytest.mark.asyncio
async def test_observer_freezes_valid_observation_before_returning_record() -> None:
    request = _request()
    flow = _Flow(_safe_observation(load_r1_dataset().cases[1]))

    record = await ProductionContractObserver(
        flow=flow,
        clock=_Clock(iter((10.0, 10.125))),
    ).observe(request=request)

    assert record.disposition is CaseExecutionDisposition.OBSERVED
    assert record.observation is not None
    assert record.observation.case_id == request.case_id
    assert record.observation.answer.outcome.value == "abstained"
    assert record.observer_elapsed_ms == 125
    assert record.failure_category is None
    assert flow.calls == 1


@pytest.mark.asyncio
async def test_observer_accepts_allowed_observation_with_exact_context_lineage() -> None:
    case = load_r1_dataset().cases[0]
    record = await ProductionContractObserver(
        flow=_Flow(_allowed_observation()),
        clock=_Clock(iter((10.0, 10.001))),
        index_version=UNAUTHORIZED_RETRIEVAL_SAFETY_SEAM,
    ).observe(request=_allowed_request(case.case_id))

    assert record.disposition is CaseExecutionDisposition.OBSERVED
    assert record.observation is not None
    assert record.observation.context_bundle is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "disposition", "category"),
    [
        (
            ProductionContractAuthorizationDenied(),
            CaseExecutionDisposition.DENIED,
            CaseExecutionFailureCategory.AUTHORIZATION_DENIED,
        ),
        (
            ProductionContractUnavailable(),
            CaseExecutionDisposition.UNAVAILABLE,
            CaseExecutionFailureCategory.RUNTIME_UNAVAILABLE,
        ),
        (
            ProductionContractPartialFailure(),
            CaseExecutionDisposition.FAILED,
            CaseExecutionFailureCategory.PARTIAL_FAILURE,
        ),
    ],
)
async def test_observer_maps_safe_application_failures_without_protected_payload(
    error: BaseException,
    disposition: CaseExecutionDisposition,
    category: CaseExecutionFailureCategory,
) -> None:
    flow = _Flow(error)

    record = await ProductionContractObserver(
        flow=flow,
        clock=_Clock(iter((1.0, 1.010))),
    ).observe(request=_request())

    assert record.disposition is disposition
    assert record.observation is None
    assert record.failure_category is category
    assert record.observer_elapsed_ms == 10
    assert "protected" not in record.model_dump_json()


@pytest.mark.asyncio
async def test_observer_preserves_completed_attempt_metadata_for_partial_failure() -> None:
    attempt_id = UUID("81000000-0000-0000-0000-000000000471")
    record = await ProductionContractObserver(
        flow=_Flow(
            ProductionContractPartialFailure(
                correlation_id=UUID("82000000-0000-0000-0000-000000000471"),
                completed_attempt_id=attempt_id,
                completed_attempt_number=2,
            )
        ),
        clock=_Clock(iter((1.0, 1.005))),
    ).observe(request=_request())

    assert record.completed_attempt_id == attempt_id
    assert record.completed_attempt_number == 2
    assert record.observer_elapsed_ms == 5


@pytest.mark.asyncio
async def test_runner_retains_safe_non_observed_record_metadata() -> None:
    case = load_r1_dataset().cases[1]
    record = await ProductionContractObserver(
        flow=_Flow(ProductionContractUnavailable()),
        clock=_Clock(iter((1.0, 1.005))),
    ).observe(request=ProductionContractCaseRequest.from_case(case))

    class RecordPort:
        index_version = "r1-production-contract-observation:v1"

        async def observe(self, *, request: ProductionContractCaseRequest):
            del request
            return record

    report = await OfflineEvaluationRunner(
        dataset=load_r1_dataset().model_copy(update={"cases": (case,)}),
        execution_mode="production-contract",
        production_observer=RecordPort(),  # type: ignore[arg-type]
    ).run()

    scenario = report.scenarios[0]
    assert scenario.status == "unavailable"
    assert scenario.execution_disposition is CaseExecutionDisposition.UNAVAILABLE
    assert scenario.execution_failure_category is CaseExecutionFailureCategory.RUNTIME_UNAVAILABLE
    assert scenario.observer_elapsed_ms == 5


@pytest.mark.asyncio
async def test_observer_rejects_invalid_observation_and_keeps_failure_safe() -> None:
    case = load_r1_dataset().cases[1]
    invalid = _safe_observation(case).model_copy(update={"case_id": "wrong:case"})

    record = await ProductionContractObserver(
        flow=_Flow(invalid),
        clock=_Clock(iter((4.0, 4.004))),
    ).observe(request=ProductionContractCaseRequest.from_case(case))

    assert record.disposition is CaseExecutionDisposition.FAILED
    assert record.observation is None
    assert record.failure_category is CaseExecutionFailureCategory.INVALID_CASE
    assert record.observer_elapsed_ms == 4


@pytest.mark.asyncio
async def test_observer_does_not_impute_latency_for_invalid_preflight_request() -> None:
    request = ProductionContractCaseRequest.model_construct(
        case_id="valid-case",
        question="synthetic",
        language="en",
        workspace_id=UUID(int=0),
        environment_id=UUID(int=2),
        principal_id=UUID(int=3),
        available_revision_ids=(),
        retrieval_strategy="synthetic",
    )
    flow = _Flow(_safe_observation(load_r1_dataset().cases[1]))
    clock_calls = 0

    def clock() -> float:
        nonlocal clock_calls
        clock_calls += 1
        return 10.0

    record = await ProductionContractObserver(flow=flow, clock=clock).observe(
        request=request
    )

    assert record.disposition is CaseExecutionDisposition.FAILED
    assert record.failure_category is CaseExecutionFailureCategory.INVALID_SCOPE
    assert record.observer_elapsed_ms is None
    assert flow.calls == 0
    assert clock_calls == 0


@pytest.mark.asyncio
async def test_observer_repeated_invocations_remain_independent_and_deterministic() -> None:
    flow = _Flow(_safe_observation(load_r1_dataset().cases[1]))
    observer = ProductionContractObserver(
        flow=flow,
        clock=_Clock(iter((1.0, 1.002, 2.0, 2.002))),
    )

    first = await observer.observe(request=_request())
    second = await observer.observe(request=_request())

    assert first.disposition is second.disposition is CaseExecutionDisposition.OBSERVED
    assert first.observation == second.observation
    assert first.observer_elapsed_ms == second.observer_elapsed_ms == 2
    assert flow.calls == 2


@pytest.mark.asyncio
async def test_observed_record_round_trip_revalidates_observation_before_release() -> None:
    record = await ProductionContractObserver(
        flow=_Flow(_safe_observation(load_r1_dataset().cases[1])),
        clock=_Clock(iter((3.0, 3.001))),
    ).observe(request=_request())

    parsed = type(record).model_validate(record.model_dump(mode="python"))

    assert parsed == record


@pytest.mark.asyncio
async def test_v1_execution_record_keeps_accepted_timing_and_serialization() -> None:
    record = await ProductionContractObserver(
        flow=_Flow(_safe_observation(load_r1_dataset().cases[1])),
        clock=_Clock(iter((3.0, 3.007))),
    ).observe(request=_request())

    payload = record.model_dump(mode="json")
    assert record.schema_version == "r1-case-execution-record:v1"
    assert record.observer_elapsed_ms == 7
    assert "worker_execution_elapsed_ns" not in payload
    assert "worker_execution_attempt_id" not in payload
    assert type(record).model_validate_json(record.model_dump_json()) == record


@pytest.mark.asyncio
async def test_v2_keeps_observer_wall_time_separate_from_worker_duration() -> None:
    observation = _safe_observation(load_r1_dataset().cases[1])
    attempt_id = observation.question.correlation.attempt_id
    flow = _Flow(
        ProductionContractFlowResult(
            observation=observation,
            worker_execution_elapsed_ns=125_000_000,
            worker_execution_attempt_id=attempt_id,
        )
    )
    record = await ProductionContractObserver(
        flow=flow,
        clock=_Clock(iter((10.0, 22.0))),
    ).observe_v2(request=_request())

    assert record.schema_version == "r1-case-execution-record:v2"
    assert record.observer_elapsed_ms == 12_000
    assert record.worker_execution_elapsed_ns == 125_000_000
    assert record.worker_execution_attempt_id == attempt_id
    assert CaseExecutionRecordV2.model_validate_json(record.model_dump_json()) == record


@pytest.mark.asyncio
async def test_v2_omits_worker_timing_when_sample_is_missing() -> None:
    record = await ProductionContractObserver(
        flow=_Flow(_safe_observation(load_r1_dataset().cases[1])),
        clock=_Clock(iter((1.0, 1.003))),
    ).observe_v2(request=_request())

    assert record.observer_elapsed_ms == 3
    assert record.worker_execution_elapsed_ns is None
    assert record.worker_execution_attempt_id is None


@pytest.mark.asyncio
async def test_v2_rejects_timing_for_a_different_attempt() -> None:
    observation = _safe_observation(load_r1_dataset().cases[1])
    wrong_attempt_id = UUID(int=observation.question.correlation.attempt_id.int + 1)
    record = await ProductionContractObserver(
        flow=_Flow(
            ProductionContractFlowResult(
                observation=observation,
                worker_execution_elapsed_ns=20,
                worker_execution_attempt_id=wrong_attempt_id,
            )
        ),
        clock=_Clock(iter((1.0, 1.001))),
    ).observe_v2(request=_request())

    assert record.disposition is CaseExecutionDisposition.FAILED
    assert record.failure_category is CaseExecutionFailureCategory.INVALID_OBSERVATION
    assert record.worker_execution_elapsed_ns is None


@pytest.mark.asyncio
async def test_v2_requires_non_negative_duration_and_paired_attempt_identity() -> None:
    observation = _safe_observation(load_r1_dataset().cases[1])
    attempt_id = observation.question.correlation.attempt_id
    record = await ProductionContractObserver(
        flow=_Flow(
            ProductionContractFlowResult(
                observation=observation,
                worker_execution_elapsed_ns=1,
                worker_execution_attempt_id=attempt_id,
            )
        ),
        clock=_Clock(iter((1.0, 1.001))),
    ).observe_v2(request=_request())
    payload = record.model_dump(mode="python")

    with pytest.raises(ValidationError):
        CaseExecutionRecordV2.model_validate(
            payload | {"worker_execution_attempt_id": None}
        )
    with pytest.raises(ValidationError):
        CaseExecutionRecordV2.model_validate(
            payload | {"worker_execution_elapsed_ns": -1}
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("clock_values", [(float("nan"),), (1.0, float("nan"))])
async def test_v2_preserves_v2_schema_when_observer_clock_is_invalid(
    clock_values: tuple[float, ...],
) -> None:
    record = await ProductionContractObserver(
        flow=_Flow(_safe_observation(load_r1_dataset().cases[1])),
        clock=_Clock(iter(clock_values)),
    ).observe_v2(request=_request())

    assert record.schema_version == "r1-case-execution-record:v2"
    assert record.disposition is CaseExecutionDisposition.FAILED
    assert record.failure_category is CaseExecutionFailureCategory.INVALID_TIMING
    assert record.observer_elapsed_ms is None
    assert record.worker_execution_elapsed_ns is None
    assert record.worker_execution_attempt_id is None
    assert CaseExecutionRecordV2.model_validate_json(record.model_dump_json()) == record


@pytest.mark.asyncio
async def test_v2_preserves_v2_schema_when_observer_clock_raises() -> None:
    def broken_clock() -> float:
        raise RuntimeError("clock unavailable")

    record = await ProductionContractObserver(
        flow=_Flow(_safe_observation(load_r1_dataset().cases[1])),
        clock=broken_clock,
    ).observe_v2(request=_request())

    assert record.schema_version == "r1-case-execution-record:v2"
    assert record.failure_category is CaseExecutionFailureCategory.INVALID_TIMING
    assert record.observer_elapsed_ms is None
    assert CaseExecutionRecordV2.model_validate_json(record.model_dump_json()) == record
