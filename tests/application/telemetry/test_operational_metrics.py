from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.application.telemetry import (
    InMemoryOperationalMetricsStore,
    OperationalMetricsAccessDenied,
    OperationalMetricsProjector,
    OperationalMetricsQuery,
    OperationalMetricsReader,
)
from spine.auth import AuthorizationRole, RequestedAuthorizationScope
from spine.domain.capabilities.answer_question import AnswerQuestionOutcome
from spine.domain.metrics import (
    AnswerRateFact,
    MetricCoverageState,
    MetricWindow,
    OperationalMetricEvent,
    OperationalMeasurement,
)
from spine.application.telemetry.metrics import measurement_for_observation
from spine.application.telemetry.observations import OperationalOperation


NOW = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)
WORKSPACE = UUID("20000000-0000-0000-0000-000000000370")
ENVIRONMENT = UUID("30000000-0000-0000-0000-000000000370")


def query(*, start: datetime = NOW - timedelta(hours=1), end: datetime = NOW + timedelta(hours=1), measurement: OperationalMeasurement | None = None) -> OperationalMetricsQuery:
    return OperationalMetricsQuery(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        window=MetricWindow(start=start, end=end),
        measurements=None if measurement is None else (measurement,),
    )


def authority(workspace: UUID = WORKSPACE) -> SimpleNamespace:
    return SimpleNamespace(
        roles=frozenset({AuthorizationRole.ADMINISTRATOR}),
        scope=RequestedAuthorizationScope(workspace_id=workspace, environment_id=ENVIRONMENT),
    )


def event(
    event_id: int,
    *,
    measurement: OperationalMeasurement = OperationalMeasurement.RETRIEVAL_LATENCY,
    at: datetime = NOW,
    duration_ms: int | None = 20,
    deduplication_key: str | None = None,
    logical_operation_id: UUID | None = None,
    terminal: bool = True,
    complete_coverage: bool = True,
    value: Decimal | None = None,
    currency: str | None = None,
    attempt_number: int | None = 1,
    sequence_number: int | None = None,
) -> OperationalMetricEvent:
    return OperationalMetricEvent(
        event_id=UUID(int=event_id),
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        source_identity="test.source:v1",
        measurement=measurement,
        event_at=at,
        deduplication_key=deduplication_key or f"event:{event_id}",
        logical_operation_id=logical_operation_id,
        attempt_number=attempt_number,
        terminal=terminal,
        complete_coverage=complete_coverage,
        duration_ms=duration_ms,
        sequence_number=sequence_number,
        value=value,
        currency=currency,
    )


@pytest.mark.asyncio
async def test_metrics_use_half_open_windows_and_deduplicate_replays() -> None:
    store = InMemoryOperationalMetricsStore()
    projector = OperationalMetricsProjector(store)
    logical_id = UUID(int=100)
    first = event(1, logical_operation_id=logical_id, deduplication_key="same-event")
    replay = event(1, logical_operation_id=logical_id, deduplication_key="same-event")
    at_end = event(2, at=NOW + timedelta(hours=1), logical_operation_id=UUID(int=101))
    await projector.observe(first)
    await projector.observe(replay)
    await projector.observe(at_end)

    page = await OperationalMetricsReader(store=store, clock=lambda: NOW, refresh_interval=timedelta(minutes=5)).read(
        query=query(end=NOW + timedelta(hours=1), measurement=OperationalMeasurement.RETRIEVAL_LATENCY),
        authority=authority(),
    )

    assert page.measurements[0].value == Decimal("20")
    assert page.measurements[0].coverage is MetricCoverageState.COMPLETE


@pytest.mark.asyncio
async def test_answer_rate_excludes_denied_and_pre_execution_cancellation() -> None:
    store = InMemoryOperationalMetricsStore()
    projector = OperationalMetricsProjector(store)
    facts = (
        AnswerRateFact(run_id=UUID(int=1), workspace_id=WORKSPACE, environment_id=ENVIRONMENT, event_at=NOW, outcome=AnswerQuestionOutcome.ANSWERED, accepted=True, execution_started=True, accepted_evidence=True, user_initiated=True),
        AnswerRateFact(run_id=UUID(int=2), workspace_id=WORKSPACE, environment_id=ENVIRONMENT, event_at=NOW, outcome=AnswerQuestionOutcome.ABSTAINED, accepted=True, execution_started=True, user_initiated=True),
        AnswerRateFact(run_id=UUID(int=3), workspace_id=WORKSPACE, environment_id=ENVIRONMENT, event_at=NOW, outcome=AnswerQuestionOutcome.DENIED, accepted=False, execution_started=False, user_initiated=True),
        AnswerRateFact(run_id=UUID(int=4), workspace_id=WORKSPACE, environment_id=ENVIRONMENT, event_at=NOW, outcome=AnswerQuestionOutcome.CANCELLED, accepted=True, execution_started=False, user_initiated=True),
        AnswerRateFact(run_id=UUID(int=5), workspace_id=WORKSPACE, environment_id=ENVIRONMENT, event_at=NOW, outcome=AnswerQuestionOutcome.ANSWERED, accepted=True, execution_started=True, accepted_evidence=True, user_initiated=False),
    )
    for fact in facts:
        await projector.answer_run(fact)

    page = await OperationalMetricsReader(store=store, clock=lambda: NOW, refresh_interval=timedelta(minutes=5)).read(
        query=query(measurement=OperationalMeasurement.ANSWER_RATE),
        authority=authority(),
    )

    measurement = page.measurements[0]
    assert (measurement.numerator, measurement.denominator) == (1, 2)


@pytest.mark.asyncio
async def test_costs_remain_exact_and_separate_by_currency() -> None:
    store = InMemoryOperationalMetricsStore()
    projector = OperationalMetricsProjector(store)
    await projector.observe(event(10, measurement=OperationalMeasurement.COST, value=Decimal("1.10"), currency="USD", duration_ms=None))
    await projector.observe(event(11, measurement=OperationalMeasurement.COST, value=Decimal("2.20"), currency="EUR", duration_ms=None))

    page = await OperationalMetricsReader(store=store, clock=lambda: NOW, refresh_interval=timedelta(minutes=5)).read(
        query=query(measurement=OperationalMeasurement.COST),
        authority=authority(),
    )

    assert {(item.currency, item.value) for item in page.measurements} == {
        ("USD", Decimal("1.10")),
        ("EUR", Decimal("2.20")),
    }


@pytest.mark.asyncio
async def test_metrics_distinguish_stale_degraded_and_unavailable() -> None:
    store = InMemoryOperationalMetricsStore()
    projector = OperationalMetricsProjector(store)
    await projector.observe(event(20, at=NOW - timedelta(minutes=11)))
    await projector.observe(event(21, at=NOW - timedelta(minutes=1), complete_coverage=False))
    reader = OperationalMetricsReader(
        store=store,
        clock=lambda: NOW,
        refresh_interval=timedelta(minutes=5),
    )

    degraded = await reader.read(query=query(measurement=OperationalMeasurement.RETRIEVAL_LATENCY), authority=authority())
    unavailable = await reader.read(query=query(measurement=OperationalMeasurement.GENERATION_LATENCY), authority=authority())

    assert degraded.measurements[0].coverage is MetricCoverageState.DEGRADED
    assert unavailable.measurements[0].coverage is MetricCoverageState.UNAVAILABLE


@pytest.mark.asyncio
async def test_metrics_mark_complete_but_old_data_stale() -> None:
    store = InMemoryOperationalMetricsStore()
    await store.append(event(22, at=NOW - timedelta(minutes=11)))
    reader = OperationalMetricsReader(
        store=store,
        clock=lambda: NOW,
        refresh_interval=timedelta(minutes=5),
    )

    page = await reader.read(query=query(measurement=OperationalMeasurement.RETRIEVAL_LATENCY), authority=authority())

    assert page.measurements[0].coverage is MetricCoverageState.STALE


@pytest.mark.asyncio
async def test_retry_attempts_and_sequence_gaps_are_separate_measurements() -> None:
    store = InMemoryOperationalMetricsStore()
    await store.append(event(23, measurement=OperationalMeasurement.RETRY_ATTEMPTS, duration_ms=None, attempt_number=2))
    await store.append(event(24, measurement=OperationalMeasurement.INGESTION_SEQUENCE_GAP, duration_ms=None, sequence_number=10))
    await store.append(event(25, measurement=OperationalMeasurement.INGESTION_SEQUENCE_GAP, duration_ms=None, sequence_number=12))

    retry_page = await OperationalMetricsReader(store=store, clock=lambda: NOW, refresh_interval=timedelta(minutes=5)).read(query=query(measurement=OperationalMeasurement.RETRY_ATTEMPTS), authority=authority())
    gap_page = await OperationalMetricsReader(store=store, clock=lambda: NOW, refresh_interval=timedelta(minutes=5)).read(query=query(measurement=OperationalMeasurement.INGESTION_SEQUENCE_GAP), authority=authority())

    assert retry_page.measurements[0].value == Decimal("1")
    assert gap_page.measurements[0].sequence_gap == 1


@pytest.mark.asyncio
async def test_metrics_denial_does_not_disclose_other_workspace_data() -> None:
    store = InMemoryOperationalMetricsStore()
    await store.append(event(30))
    reader = OperationalMetricsReader(store=store, clock=lambda: NOW, refresh_interval=timedelta(minutes=5))

    with pytest.raises(OperationalMetricsAccessDenied, match="unavailable"):
        await reader.read(query=query(), authority=authority(UUID("20000000-0000-0000-0000-000000000371")))


def test_metric_contract_rejects_untrusted_events_and_invalid_windows() -> None:
    with pytest.raises(ValidationError):
        MetricWindow(start=NOW, end=NOW)
    with pytest.raises(ValidationError):
        event(40, complete_coverage=True).model_copy(update={"trusted": False})


def test_o01_operation_maps_to_one_approved_latency_boundary() -> None:
    observation = SimpleNamespace(operation=OperationalOperation.CONTEXT_RETRIEVAL)
    assert measurement_for_observation(observation) is OperationalMeasurement.RETRIEVAL_LATENCY
