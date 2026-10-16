"""PostgreSQL adapter for the operational metrics source read model."""

from __future__ import annotations

from datetime import datetime, timedelta
from collections.abc import Mapping
from decimal import Decimal
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Result

from spine.application.telemetry.metrics import OperationalMetricsQuery
from spine.domain.metrics import (
    AnswerRateFact,
    FeedbackMetricFact,
    OperationalMetricEvent,
    OperationalMeasurement,
)
from spine.domain.feedback import FeedbackRecord
from spine.infrastructure.persistence.postgresql_mappings import operational_metric_events


class PostgreSQLMetricsExecutor(Protocol):
    def _guard_active(self) -> None: ...

    async def _execute(self, statement: object) -> Result: ...


class PostgreSQLOperationalMetricsStore:
    """Use an already tenant-scoped PostgreSQL unit of work."""

    def __init__(self, uow: PostgreSQLMetricsExecutor) -> None:
        self._uow = uow

    async def append(self, event: OperationalMetricEvent) -> None:
        self._uow._guard_active()
        existing = await self._uow._execute(
            select(operational_metric_events).where(
                operational_metric_events.c.workspace_id == event.workspace_id,
                operational_metric_events.c.environment_id == event.environment_id,
                operational_metric_events.c.deduplication_key == event.deduplication_key,
            )
        )
        row = existing.mappings().first()
        if row is not None:
            if _event_from_row(row) != event:
                raise ValueError("metric deduplication key was reused for different data")
            return
        await self._uow._execute(
            insert(operational_metric_events)
            .values(_values(event))
            .on_conflict_do_nothing(constraint="uq_operational_metric_events_deduplication")
        )
        persisted = await self._uow._execute(
            select(operational_metric_events).where(
                operational_metric_events.c.workspace_id == event.workspace_id,
                operational_metric_events.c.environment_id == event.environment_id,
                operational_metric_events.c.deduplication_key == event.deduplication_key,
            )
        )
        row = persisted.mappings().first()
        if row is None or _event_from_row(row) != event:
            raise ValueError("metric deduplication key was reused for different data")

    async def append_answer_rate_fact(self, fact: AnswerRateFact) -> None:
        await self.append(OperationalMetricEvent.from_answer_rate_fact(fact))

    async def query_events(
        self,
        query: OperationalMetricsQuery,
        *,
        now: datetime,
        retention: timedelta,
    ) -> tuple[OperationalMetricEvent, ...]:
        self._uow._guard_active()
        predicates = [
            operational_metric_events.c.workspace_id == query.workspace_id,
            operational_metric_events.c.environment_id == query.environment_id,
            operational_metric_events.c.event_at >= now - retention,
            operational_metric_events.c.event_at >= query.window.start,
            operational_metric_events.c.event_at < query.window.end,
        ]
        if query.measurements is not None:
            predicates.append(
                operational_metric_events.c.measurement.in_([item.value for item in query.measurements])
            )
        result = await self._uow._execute(
            select(operational_metric_events)
            .where(*predicates)
            .order_by(operational_metric_events.c.event_at, operational_metric_events.c.event_id)
        )
        return tuple(_event_from_row(row) for row in result.mappings().all())

    async def append_feedback(self, feedback: FeedbackRecord) -> None:
        await self.append(OperationalMetricEvent.from_feedback(feedback))

    async def answer_rate_facts(
        self,
        query: OperationalMetricsQuery,
        *,
        now: datetime,
        retention: timedelta,
    ) -> tuple[AnswerRateFact, ...]:
        if query.measurements is not None and OperationalMeasurement.ANSWER_RATE not in query.measurements:
            return ()
        events = await self.query_events(
            query.model_copy(update={"measurements": (OperationalMeasurement.ANSWER_RATE,)}),
            now=now,
            retention=retention,
        )
        return tuple(
            AnswerRateFact(
                run_id=event.event_id,
                workspace_id=event.workspace_id,
                environment_id=event.environment_id,
                event_at=event.event_at,
                outcome=event.answer_outcome,
                accepted=event.accepted,
                execution_started=event.execution_started,
                accepted_evidence=event.accepted_evidence,
                user_initiated=event.user_initiated,
            )
            for event in events
            if event.answer_outcome is not None
            and event.accepted is not None
            and event.execution_started is not None
            and event.accepted_evidence is not None
            and event.user_initiated is not None
        )

    async def feedback_facts(
        self,
        query: OperationalMetricsQuery,
        *,
        now: datetime,
        retention: timedelta,
    ) -> tuple[FeedbackMetricFact, ...]:
        if query.measurements is not None and OperationalMeasurement.USEFULNESS not in query.measurements:
            return ()
        events = await self.query_events(
            query.model_copy(update={"measurements": (OperationalMeasurement.USEFULNESS,)}),
            now=now,
            retention=retention,
        )
        return tuple(
            FeedbackMetricFact(
                feedback_id=event.feedback_id,
                workspace_id=event.workspace_id,
                environment_id=event.environment_id,
                event_at=event.event_at,
                outcome=event.feedback_outcome,
            )
            for event in events
            if event.feedback_id is not None and event.feedback_outcome is not None
        )

def _values(event: OperationalMetricEvent) -> dict[str, object]:
    return {
        "event_id": event.event_id,
        "workspace_id": event.workspace_id,
        "environment_id": event.environment_id,
        "source_identity": event.source_identity,
        "measurement": event.measurement.value,
        "event_at": event.event_at,
        "deduplication_key": event.deduplication_key,
        "logical_operation_id": event.logical_operation_id,
        "attempt_id": event.attempt_id,
        "attempt_number": event.attempt_number,
        "terminal": event.terminal,
        "complete_coverage": event.complete_coverage,
        "duration_ms": event.duration_ms,
        "sequence_number": event.sequence_number,
        "source_watermark_at": event.source_watermark_at,
        "projection_watermark_at": event.projection_watermark_at,
        "value": None if event.value is None else format(event.value, "f"),
        "currency": event.currency,
        "answer_outcome": event.answer_outcome.value if event.answer_outcome is not None else None,
        "accepted": event.accepted,
        "execution_started": event.execution_started,
        "accepted_evidence": event.accepted_evidence,
        "user_initiated": event.user_initiated,
        "feedback_id": event.feedback_id,
        "feedback_outcome": event.feedback_outcome.value if event.feedback_outcome is not None else None,
    }


def _event_from_row(row: Mapping[str, object]) -> OperationalMetricEvent:
    values = dict(row)
    values["measurement"] = OperationalMeasurement(values["measurement"])
    if values.get("value") is not None:
        values["value"] = Decimal(str(values["value"]))
    return OperationalMetricEvent.model_validate(values)


__all__ = ["PostgreSQLMetricsExecutor", "PostgreSQLOperationalMetricsStore"]
