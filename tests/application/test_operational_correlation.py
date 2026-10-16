from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import pytest

from spine.application.operational_correlation import (
    InMemoryOperationalCorrelationStore,
    OperationalCorrelationAccessDenied,
    OperationalCorrelationProjector,
    OperationalCorrelationReader,
)
from spine.application.outbox_inspection import OutboxDeliveryInspectionItem
from spine.auth import AuthorizationRole
from spine.domain.capabilities.answer_question import AnswerQuestionOutcome
from spine.domain.capabilities.answer_question_runs import AnswerQuestionRun
from spine.domain.common import ActorKind, ActorRef, EnvironmentKind
from spine.domain.feedback import FeedbackOutcome, FeedbackRecord, FeedbackTargetKind
from spine.domain.metrics import OperationalMetricEvent, OperationalMeasurement
from spine.domain.operational_correlation import (
    CorrelationCoverageState,
    CorrelationReferenceKind,
    CorrelationSourceKind,
    OperationalCorrelationQuery,
)


WORKSPACE = UUID("20000000-0000-0000-0000-000000000366")
ENVIRONMENT = UUID("30000000-0000-0000-0000-000000000366")
OTHER_WORKSPACE = UUID("20000000-0000-0000-0000-000000000367")
TRACE = UUID("50000000-0000-0000-0000-000000000366")
CORRELATION = UUID("60000000-0000-0000-0000-000000000366")
RUN = UUID("70000000-0000-0000-0000-000000000366")
FEEDBACK = UUID("80000000-0000-0000-0000-000000000366")
DELIVERY = UUID("90000000-0000-0000-0000-000000000366")
NOW = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)


def authority(workspace: UUID = WORKSPACE) -> SimpleNamespace:
    return SimpleNamespace(
        roles=frozenset({AuthorizationRole.ADMINISTRATOR}),
        scope=SimpleNamespace(workspace_id=workspace, environment_id=ENVIRONMENT),
    )


def run() -> AnswerQuestionRun:
    return AnswerQuestionRun.start(
        run_id=RUN,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        environment=EnvironmentKind.DEVELOPMENT,
        subject=ActorRef(kind=ActorKind.HUMAN, id=UUID(int=1)),
        idempotency_key="correlation-test-run",
        command_digest="a" * 64,
        input_artifact_ids=(UUID(int=2),),
        diagnostic_context=SimpleNamespace(
            trace_id=TRACE,
            correlation_id=CORRELATION,
            causation_id=None,
        ),
        created_at=NOW,
    )


def feedback() -> FeedbackRecord:
    return FeedbackRecord(
        feedback_id=FEEDBACK,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        subject_id=UUID(int=3),
        trace_id=TRACE,
        run_id=RUN,
        message_id=UUID(int=4),
        target_kind=FeedbackTargetKind.ANSWER,
        target_id=UUID(int=5),
        outcome=FeedbackOutcome.ANSWER_HELPFUL,
        comment="protected comment must not enter the view",
        created_at=NOW,
    )


def metric() -> OperationalMetricEvent:
    return OperationalMetricEvent(
        event_id=UUID(int=6),
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        trace_id=TRACE,
        correlation_id=CORRELATION,
        source_identity="o01.operational-observation:v1",
        measurement=OperationalMeasurement.RETRIEVAL_LATENCY,
        event_at=NOW,
        deduplication_key="observation:correlation-test",
        logical_operation_id=RUN,
        duration_ms=42,
    )


def delivery() -> OutboxDeliveryInspectionItem:
    return OutboxDeliveryInspectionItem(
        event_id=DELIVERY,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        status="quarantined",
        attempt_count=2,
        created_at=NOW,
        updated_at=NOW,
        quarantined_at=NOW,
        last_error_code="consumer.unavailable",
        correlation_id=CORRELATION,
    )


def query(kind: CorrelationReferenceKind = CorrelationReferenceKind.CORRELATION) -> OperationalCorrelationQuery:
    return OperationalCorrelationQuery(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        reference_kind=kind,
        reference_id=CORRELATION if kind is CorrelationReferenceKind.CORRELATION else TRACE,
    )


@pytest.mark.asyncio
async def test_correlation_connects_safe_operational_sources_without_content() -> None:
    store = InMemoryOperationalCorrelationStore()
    projector = OperationalCorrelationProjector(store)
    await projector.observe_metric(metric())
    await projector.observe_delivery(delivery())
    await projector.observe_run(run())
    await projector.observe_feedback(feedback())

    result = await OperationalCorrelationReader(store=store).read(
        query=query(), authority=authority()
    )

    assert result.coverage is CorrelationCoverageState.COMPLETE
    assert result.missing_sources == ()
    assert {item.source_kind for item in result.records} == set(CorrelationSourceKind)
    assert result.trace_ids == (TRACE,)
    assert result.correlation_ids == (CORRELATION,)
    payload = result.model_dump_json()
    assert "protected comment" not in payload
    assert '"comment"' not in payload
    assert "input_artifact_ids" not in payload
    assert "provider" not in payload
    assert {item.source_record_id for item in result.lineage} >= {
        RUN,
        FEEDBACK,
        DELIVERY,
    }


@pytest.mark.asyncio
async def test_correlation_marks_missing_sources_and_deduplicates_replay() -> None:
    store = InMemoryOperationalCorrelationStore()
    projector = OperationalCorrelationProjector(store)
    await projector.observe_metric(metric())
    await projector.observe_metric(metric())

    result = await OperationalCorrelationReader(store=store).read(
        query=query(), authority=authority()
    )

    assert result.coverage is CorrelationCoverageState.INCOMPLETE
    assert CorrelationSourceKind.RUN in result.missing_sources
    assert len(result.records) == 1


@pytest.mark.asyncio
async def test_correlation_denial_does_not_enumerate_other_workspace() -> None:
    store = InMemoryOperationalCorrelationStore()
    projector = OperationalCorrelationProjector(store)
    await projector.observe_metric(metric())

    with pytest.raises(OperationalCorrelationAccessDenied, match="unavailable"):
        await OperationalCorrelationReader(store=store).read(
            query=query(), authority=authority(OTHER_WORKSPACE)
        )


@pytest.mark.asyncio
async def test_correlation_run_reference_follows_shared_trace() -> None:
    store = InMemoryOperationalCorrelationStore()
    projector = OperationalCorrelationProjector(store)
    await projector.observe_metric(metric())
    await projector.observe_run(run())

    result = await OperationalCorrelationReader(store=store).read(
        query=query(CorrelationReferenceKind.TRACE), authority=authority()
    )

    assert {item.source_kind for item in result.records} == {
        CorrelationSourceKind.METRIC,
        CorrelationSourceKind.RUN,
    }
    assert result.coverage is CorrelationCoverageState.INCOMPLETE
    assert result.records[0].metric_value is None
    assert Decimal("42") not in result.model_dump().values()


@pytest.mark.asyncio
async def test_correlation_keeps_degraded_metric_coverage_explicit() -> None:
    store = InMemoryOperationalCorrelationStore()
    await OperationalCorrelationProjector(store).observe_metric(
        metric().model_copy(update={"complete_coverage": False})
    )

    result = await OperationalCorrelationReader(store=store).read(
        query=query(), authority=authority()
    )

    assert result.coverage is CorrelationCoverageState.INCOMPLETE
    assert result.conflict_codes == ("metric_coverage_incomplete",)


@pytest.mark.asyncio
async def test_correlation_marks_schema_mismatch_incomplete() -> None:
    store = InMemoryOperationalCorrelationStore()
    record = (
        await _metric_record(store)
    ).model_copy(update={"source_schema_version": "operational-metric-event:v9"})
    mismatched_store = InMemoryOperationalCorrelationStore()
    await mismatched_store.append(record)

    result = await OperationalCorrelationReader(store=mismatched_store).read(
        query=query(), authority=authority()
    )

    assert result.coverage is CorrelationCoverageState.INCOMPLETE
    assert result.conflict_codes == ("source_schema_version_mismatch",)


@pytest.mark.asyncio
async def test_correlation_without_any_source_is_unavailable() -> None:
    result = await OperationalCorrelationReader(
        store=InMemoryOperationalCorrelationStore()
    ).read(query=query(), authority=authority())

    assert result.coverage is CorrelationCoverageState.UNAVAILABLE
    assert result.records == ()


async def _metric_record(store: InMemoryOperationalCorrelationStore):
    projector = OperationalCorrelationProjector(store)
    await projector.observe_metric(metric())
    result = await OperationalCorrelationReader(store=store).read(
        query=query(), authority=authority()
    )
    return result.records[0]
