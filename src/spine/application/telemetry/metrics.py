"""Deterministic operational metrics projection and read boundary."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Awaitable, Literal, Protocol
from inspect import isawaitable
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from spine.auth import AuthorizationRole, AuthorizedRequestContext
from spine.domain.capabilities.answer_question import AnswerQuestionOutcome
from spine.domain.common import ValidatedDefinitionModel
from spine.domain.feedback import FeedbackOutcome, FeedbackRecord
from spine.domain.metrics import (
    AnswerRateFact,
    FeedbackMetricFact,
    MetricCoverageState,
    MetricWindow,
    OperationalMetricEvent,
    OperationalMetricMeasurement,
    OperationalMeasurement,
    OperationalObservationLike,
)
from spine.domain.capabilities.answer_question_runs import AnswerQuestionAttempt, AnswerQuestionRun
from spine.application.telemetry.observations import OperationalObservation, OperationalOperation


class OperationalMetricsQuery(ValidatedDefinitionModel):
    """Tenant-scoped query for a half-open metrics window."""

    schema_version: Literal["operational-metrics-query:v1"] = "operational-metrics-query:v1"
    workspace_id: UUID
    environment_id: UUID
    window: MetricWindow
    measurements: tuple[OperationalMeasurement, ...] | None = None

    @field_validator("workspace_id", "environment_id")
    @classmethod
    def validate_ids(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("metric query identifiers must be non-zero")
        return value

    @model_validator(mode="after")
    def validate_measurements(self) -> "OperationalMetricsQuery":
        if self.measurements is not None and len(set(self.measurements)) != len(self.measurements):
            raise ValueError("metric identities must be unique")
        return self


class OperationalMetricsPage(ValidatedDefinitionModel):
    schema_version: Literal["operational-metrics-page:v1"] = "operational-metrics-page:v1"
    window: MetricWindow
    measurements: tuple[OperationalMetricMeasurement, ...]


class OperationalMetricsAccessDenied(PermissionError):
    """Safe denial without disclosing whether a tenant has measurements."""


class OperationalMetricsStore(Protocol):
    async def append(self, event: OperationalMetricEvent) -> None: ...

    async def append_answer_rate_fact(self, fact: AnswerRateFact) -> None: ...

    async def append_feedback(self, feedback: FeedbackRecord) -> None: ...

    async def answer_rate_facts(
        self,
        query: OperationalMetricsQuery,
        *,
        now: datetime,
        retention: timedelta,
    ) -> tuple[AnswerRateFact, ...]: ...

    async def feedback_facts(
        self,
        query: OperationalMetricsQuery,
        *,
        now: datetime,
        retention: timedelta,
    ) -> tuple[FeedbackMetricFact, ...]: ...

    async def query_events(
        self,
        query: OperationalMetricsQuery,
        *,
        now: datetime,
        retention: timedelta,
    ) -> tuple[OperationalMetricEvent, ...]: ...


class InMemoryOperationalMetricsStore:
    """Reference store used by domain/application contract tests."""

    def __init__(self) -> None:
        self._events: dict[tuple[UUID, UUID, str], OperationalMetricEvent] = {}
        self._answer_rate_facts: dict[tuple[UUID, UUID, UUID], AnswerRateFact] = {}
        self._feedback: dict[tuple[UUID, UUID, UUID, int], FeedbackRecord] = {}

    async def append(self, event: OperationalMetricEvent) -> None:
        key = (event.workspace_id, event.environment_id, event.deduplication_key)
        previous = self._events.get(key)
        if previous is not None and previous != event:
            raise ValueError("metric deduplication key was reused for different data")
        self._events[key] = event

    async def append_answer_rate_fact(self, fact: AnswerRateFact) -> None:
        key = (fact.workspace_id, fact.environment_id, fact.run_id)
        previous = self._answer_rate_facts.get(key)
        if previous is not None and previous != fact:
            raise ValueError("answer-rate run identity was reused for different data")
        self._answer_rate_facts[key] = fact

    async def append_feedback(self, feedback: FeedbackRecord) -> None:
        key = (feedback.workspace_id, feedback.environment_id, feedback.feedback_id, feedback.version)
        previous = self._feedback.get(key)
        if previous is not None and previous != feedback:
            raise ValueError("feedback identity was reused for different data")
        self._feedback[key] = feedback

    async def answer_rate_facts(self, query: OperationalMetricsQuery, *, now: datetime, retention: timedelta) -> tuple[AnswerRateFact, ...]:
        cutoff = now - retention
        return tuple(
            fact
            for fact in self._answer_rate_facts.values()
            if fact.workspace_id == query.workspace_id and fact.environment_id == query.environment_id
            and fact.event_at >= cutoff
            and query.window.start <= fact.event_at < query.window.end
        )

    async def feedback_facts(self, query: OperationalMetricsQuery, *, now: datetime, retention: timedelta) -> tuple[FeedbackMetricFact, ...]:
        cutoff = now - retention
        return tuple(
            FeedbackMetricFact.from_feedback(record)
            for record in self._feedback.values()
            if record.workspace_id == query.workspace_id and record.environment_id == query.environment_id
            and record.created_at >= cutoff
            and query.window.start <= record.created_at < query.window.end
        )

    async def query_events(
        self,
        query: OperationalMetricsQuery,
        *,
        now: datetime,
        retention: timedelta,
    ) -> tuple[OperationalMetricEvent, ...]:
        cutoff = now.astimezone(timezone.utc) - retention
        identities = set(query.measurements) if query.measurements is not None else None
        return tuple(
            sorted(
                (
                    event
                    for event in self._events.values()
                    if event.workspace_id == query.workspace_id
                    and event.environment_id == query.environment_id
                    and event.event_at >= cutoff
                    and query.window.start <= event.event_at < query.window.end
                    and (identities is None or event.measurement in identities)
                ),
                key=lambda item: (item.event_at, item.event_id.int),
            )
        )


class OperationalMetricsProjector:
    """Project O01 observations and canonical Q&A/feedback facts."""

    def __init__(self, store: OperationalMetricsStore) -> None:
        self._store = store

    async def observe(self, event: OperationalMetricEvent) -> None:
        await self._store.append(event)

    async def observe_observation(
        self,
        observation: OperationalObservation,
        *,
        measurement: OperationalMeasurement | None = None,
    ) -> None:
        """Accept one approved O01 observation at the metrics seam."""
        expected = measurement_for_observation(observation)
        if measurement is not None and measurement is not expected:
            raise ValueError(f"{observation.operation.value} observations map to {expected.value}")
        await self.observe(
            OperationalMetricEvent.from_observation(
                observation,
                measurement=expected,
            )
        )

    async def answer_run(self, fact: AnswerRateFact) -> None:
        await self._store.append_answer_rate_fact(fact)
        await self._store.append(OperationalMetricEvent.from_answer_rate_fact(fact))

    async def answer_question_run(
        self,
        run: AnswerQuestionRun,
        *,
        accepted: bool = True,
        execution_started: bool = True,
        accepted_evidence: bool = False,
    ) -> None:
        await self.answer_run(
            AnswerRateFact.from_run(
                run,
                accepted=accepted,
                execution_started=execution_started,
                accepted_evidence=accepted_evidence,
            )
        )

    async def answer_question_attempt(self, attempt: AnswerQuestionAttempt) -> None:
        event = OperationalMetricEvent.from_answer_question_attempt(attempt)
        if event is not None:
            await self.observe(event)

    async def feedback(self, record: FeedbackRecord) -> None:
        await self._store.append_feedback(record)
        await self._store.append(OperationalMetricEvent.from_feedback(record))


class OperationalMetricsReader:
    def __init__(
        self,
        *,
        store: OperationalMetricsStore,
        clock: Callable[[], datetime],
        retention: timedelta = timedelta(days=90),
        refresh_interval: timedelta,
        answer_rate_facts: Callable[[OperationalMetricsQuery], Iterable[AnswerRateFact] | Awaitable[Iterable[AnswerRateFact]]] | None = None,
        feedback_facts: Callable[[OperationalMetricsQuery], Iterable[FeedbackMetricFact] | Awaitable[Iterable[FeedbackMetricFact]]] | None = None,
    ) -> None:
        if retention != timedelta(days=90):
            raise ValueError("operational metrics retention is fixed at 90 days")
        if refresh_interval <= timedelta(0):
            raise ValueError("operational metrics refresh interval must be positive")
        self._store = store
        self._clock = clock
        self._retention = retention
        self._refresh_interval = refresh_interval
        self._answer_rate_facts = answer_rate_facts
        self._feedback_facts = feedback_facts

    async def read(
        self,
        *,
        query: OperationalMetricsQuery,
        authority: AuthorizedRequestContext[object],
    ) -> OperationalMetricsPage:
        if (
            authority.roles != frozenset({AuthorizationRole.ADMINISTRATOR})
            or authority.scope.workspace_id != query.workspace_id
            or authority.scope.environment_id != query.environment_id
        ):
            raise OperationalMetricsAccessDenied("Operational metrics are unavailable.")
        now = self._clock().astimezone(timezone.utc)
        events = await self._store.query_events(query, now=now, retention=self._retention)
        answer_facts = await self._answer_rate_source(query, now)
        feedback = await self._feedback_source(query, now)
        return OperationalMetricsPage(
            window=query.window,
            measurements=self._aggregate(query, events, now, answer_facts, feedback),
        )

    async def _answer_rate_source(self, query: OperationalMetricsQuery, now: datetime) -> tuple[AnswerRateFact, ...]:
        result = (
            self._answer_rate_facts(query)
            if self._answer_rate_facts is not None
            else self._store.answer_rate_facts(query, now=now, retention=self._retention)
        )
        if isawaitable(result):
            result = await result
        return tuple(result)

    async def _feedback_source(self, query: OperationalMetricsQuery, now: datetime) -> tuple[FeedbackMetricFact, ...]:
        result = (
            self._feedback_facts(query)
            if self._feedback_facts is not None
            else self._store.feedback_facts(query, now=now, retention=self._retention)
        )
        if isawaitable(result):
            result = await result
        return tuple(result)

    def _aggregate(
        self,
        query: OperationalMetricsQuery,
        events: tuple[OperationalMetricEvent, ...],
        now: datetime,
        answer_facts: tuple[AnswerRateFact, ...],
        feedback_records: tuple[FeedbackMetricFact, ...],
    ) -> tuple[OperationalMetricMeasurement, ...]:
        requested = query.measurements or tuple(OperationalMeasurement)
        results: list[OperationalMetricMeasurement] = []
        for measurement in requested:
            matching = tuple(event for event in events if event.measurement is measurement)
            if measurement is OperationalMeasurement.ANSWER_RATE:
                results.extend(self._answer_rate(query, now, answer_facts, matching))
            elif measurement is OperationalMeasurement.USEFULNESS:
                results.extend(self._usefulness(query, now, feedback_records, matching))
            elif measurement is OperationalMeasurement.COST:
                results.extend(self._cost(query, now, matching))
            elif measurement in {
                OperationalMeasurement.INGESTION_SEQUENCE_GAP,
                OperationalMeasurement.PROJECTION_SEQUENCE_GAP,
            }:
                results.extend(self._sequence_gap(query, now, measurement, matching))
            else:
                results.extend(self._numeric(query, now, measurement, matching))
        return tuple(results)

    def _answer_rate(self, query: OperationalMetricsQuery, now: datetime, configured_facts: tuple[AnswerRateFact | FeedbackMetricFact, ...], events: tuple[OperationalMetricEvent, ...]) -> tuple[OperationalMetricMeasurement, ...]:
        facts = tuple(
            fact
            for fact in configured_facts
            if fact.workspace_id == query.workspace_id
            and fact.environment_id == query.environment_id
            and fact.event_at >= now - self._retention
            and query.window.start <= fact.event_at < query.window.end
        )
        if not facts:
            facts = tuple(
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
            )
        numerator = sum(
            fact.accepted_evidence
            and fact.accepted
            and fact.user_initiated
            and fact.execution_started
            and fact.outcome is AnswerQuestionOutcome.ANSWERED
            for fact in facts
        )
        denominator = sum(
            fact.accepted
            and fact.execution_started
            and fact.user_initiated
            and fact.outcome
            in {
                AnswerQuestionOutcome.ANSWERED,
                AnswerQuestionOutcome.ABSTAINED,
                AnswerQuestionOutcome.CLARIFICATION_REQUIRED,
                AnswerQuestionOutcome.FAILED,
                AnswerQuestionOutcome.CANCELLED,
            }
            for fact in facts
        )
        return (
            self._measurement(
                query,
                now,
                OperationalMeasurement.ANSWER_RATE,
                source="canonical.answer-question-run:v1",
                coverage=self._coverage(bool(facts), False, max((fact.event_at for fact in facts), default=None), now),
                numerator=numerator if facts else None,
                denominator=denominator if facts else None,
                latest=max((fact.event_at for fact in facts), default=None),
                deduplication_identity="answer-rate:run-id:v1",
            ),
        )

    def _usefulness(self, query: OperationalMetricsQuery, now: datetime, configured_records: tuple[AnswerRateFact | FeedbackMetricFact, ...], events: tuple[OperationalMetricEvent, ...]) -> tuple[OperationalMetricMeasurement, ...]:
        records = tuple(
            record
            for record in configured_records
            if record.workspace_id == query.workspace_id
            and record.environment_id == query.environment_id
            and record.event_at >= now - self._retention
            and query.window.start <= record.event_at < query.window.end
        )
        if not records:
            helpful = sum(
                event.feedback_outcome in {FeedbackOutcome.ANSWER_HELPFUL, FeedbackOutcome.CITATION_HELPFUL}
                for event in events
            )
            latest = max((event.event_at for event in events), default=None)
        else:
            helpful = sum(record.outcome in {FeedbackOutcome.ANSWER_HELPFUL, FeedbackOutcome.CITATION_HELPFUL} for record in records)
            latest = max((record.event_at for record in records), default=None)
        return (
            self._measurement(
                query,
                now,
                OperationalMeasurement.USEFULNESS,
                source="canonical.qa-feedback:v1",
                coverage=self._coverage(bool(records or events), False, latest, now),
                value=Decimal(helpful) if records or events else None,
                latest=latest,
                deduplication_identity="usefulness:feedback-id-version:v1",
            ),
        )

    def _cost(self, query: OperationalMetricsQuery, now: datetime, events: tuple[OperationalMetricEvent, ...]) -> list[OperationalMetricMeasurement]:
        grouped: dict[tuple[str, str], list[OperationalMetricEvent]] = defaultdict(list)
        for event in events:
            assert event.currency is not None
            grouped[(event.currency, event.source_identity)].append(event)
        if not grouped:
            return [self._measurement(query, now, OperationalMeasurement.COST, source="canonical.answer-question-usage:v1", coverage=MetricCoverageState.UNAVAILABLE, deduplication_identity="cost:event-id:v1")]
        return [
            self._measurement(
                query,
                now,
                OperationalMeasurement.COST,
                source=values[0].source_identity,
                coverage=self._coverage(True, any(not event.complete_coverage for event in values), max(event.event_at for event in values), now),
                value=sum((event.value or Decimal("0") for event in values), Decimal("0")),
                currency=currency,
                latest=max(event.event_at for event in values),
                deduplication_identity=f"cost:event-id:v1:{currency}",
            )
            for (currency, source), values in sorted(grouped.items())
        ]

    def _numeric(self, query: OperationalMetricsQuery, now: datetime, measurement: OperationalMeasurement, events: tuple[OperationalMetricEvent, ...]) -> tuple[OperationalMetricMeasurement, ...]:
        sources = sorted({event.source_identity for event in events})
        if len(sources) > 1:
            return tuple(
                item
                for source in sources
                for item in self._numeric(
                    query,
                    now,
                    measurement,
                    tuple(event for event in events if event.source_identity == source),
                )
            )
        terminal: dict[UUID, OperationalMetricEvent] = {}
        for event in events:
            if not event.terminal or event.duration_ms is None:
                continue
            key = event.logical_operation_id or event.event_id
            if key not in terminal or event.event_at > terminal[key].event_at:
                terminal[key] = event
        values = tuple(event.duration_ms for event in terminal.values() if event.duration_ms is not None)
        average = None if not values else sum(values, 0) / Decimal(len(values))
        latest = max((event.event_at for event in events), default=None)
        if measurement in {OperationalMeasurement.INGESTION_PROCESSING_LAG, OperationalMeasurement.PROJECTION_INDEXING_LAG}:
            lag_values = [
                (event.projection_watermark_at - event.source_watermark_at).total_seconds() * 1000
                for event in events
                if event.source_watermark_at is not None and event.projection_watermark_at is not None
            ]
            average = None if not lag_values else sum(lag_values, 0) / len(lag_values)
        if measurement is OperationalMeasurement.RETRY_ATTEMPTS:
            retry_attempts = {
                (event.logical_operation_id or event.event_id, event.attempt_id or event.attempt_number)
                for event in events
                if event.attempt_number is not None and event.attempt_number > 1
            }
            average = Decimal(len(retry_attempts))
        watermark_event = max(events, key=lambda event: event.event_at) if events else None
        return (
            self._measurement(
                query,
                now,
                measurement,
                source=events[0].source_identity if events else "o01.operational-observation:v1",
                coverage=self._coverage(bool(events), any(not event.complete_coverage for event in events), latest, now),
                value=None if average is None else Decimal(str(average)),
                latest=latest,
                source_watermark_at=(watermark_event.source_watermark_at if watermark_event else None),
                projection_watermark_at=(watermark_event.projection_watermark_at if watermark_event else None),
                deduplication_identity=f"{measurement.value}:logical-operation:v1",
            ),
        )

    def _sequence_gap(self, query: OperationalMetricsQuery, now: datetime, measurement: OperationalMeasurement, events: tuple[OperationalMetricEvent, ...]) -> tuple[OperationalMetricMeasurement, ...]:
        sources = sorted({event.source_identity for event in events})
        if len(sources) > 1:
            return tuple(
                item
                for source in sources
                for item in self._sequence_gap(
                    query,
                    now,
                    measurement,
                    tuple(event for event in events if event.source_identity == source),
                )
            )
        sequences = sorted({event.sequence_number for event in events if event.sequence_number is not None})
        gap = None if not sequences else sequences[-1] - sequences[0] + 1 - len(sequences)
        watermark_event = max(events, key=lambda event: event.event_at) if events else None
        return (
            self._measurement(
                query,
                now,
                measurement,
                source=events[0].source_identity if events else "o01.operational-observation:v1",
                coverage=self._coverage(bool(events), any(not event.complete_coverage for event in events), max((event.event_at for event in events), default=None), now),
                value=None if gap is None else Decimal(gap),
                sequence_gap=gap,
                latest=max((event.event_at for event in events), default=None),
                source_watermark_at=(watermark_event.source_watermark_at if watermark_event else None),
                projection_watermark_at=(watermark_event.projection_watermark_at if watermark_event else None),
                deduplication_identity=f"{measurement.value}:sequence:v1",
            ),
        )

    def _measurement(self, query: OperationalMetricsQuery, now: datetime, measurement: OperationalMeasurement, *, source: str, coverage: MetricCoverageState, deduplication_identity: str, value: Decimal | None = None, numerator: int | None = None, denominator: int | None = None, currency: str | None = None, latest: datetime | None = None, sequence_gap: int | None = None, source_watermark_at: datetime | None = None, projection_watermark_at: datetime | None = None) -> OperationalMetricMeasurement:
        return OperationalMetricMeasurement(
            source_identity=source,
            measurement_identity=measurement,
            workspace_id=query.workspace_id,
            environment_id=query.environment_id,
            window=query.window,
            aggregation_revision="operational-metrics-aggregation:v1",
            deduplication_identity=deduplication_identity,
            coverage=coverage,
            value=value,
            numerator=numerator,
            denominator=denominator,
            currency=currency,
            event_timestamp=latest,
            source_watermark_at=source_watermark_at,
            projection_watermark_at=projection_watermark_at,
            sequence_gap=sequence_gap,
        )

    def _coverage(self, has_data: bool, incomplete: bool, latest: datetime | None, now: datetime) -> MetricCoverageState:
        if not has_data:
            return MetricCoverageState.UNAVAILABLE
        if incomplete:
            return MetricCoverageState.DEGRADED
        if self._refresh_interval is not None and latest is not None and now > latest + self._refresh_interval + timedelta(minutes=5):
            return MetricCoverageState.STALE
        return MetricCoverageState.COMPLETE


def measurement_for_observation(observation: OperationalObservationLike) -> OperationalMeasurement:
    """Map each approved O01 operation to exactly one latency measurement."""
    mapping = {
        OperationalOperation.HTTP_REQUEST.value: OperationalMeasurement.API_LATENCY,
        OperationalOperation.OUTBOX_ENQUEUE.value: OperationalMeasurement.INGESTION_LATENCY,
        OperationalOperation.OUTBOX_DELIVERY.value: OperationalMeasurement.INGESTION_LATENCY,
        OperationalOperation.PROJECTION_BUILD.value: OperationalMeasurement.PROJECTION_LATENCY,
        OperationalOperation.CONTEXT_RETRIEVAL.value: OperationalMeasurement.RETRIEVAL_LATENCY,
        OperationalOperation.ANSWER_GENERATION.value: OperationalMeasurement.GENERATION_LATENCY,
        OperationalOperation.RUN_PERSISTENCE.value: OperationalMeasurement.QA_LATENCY,
        OperationalOperation.SSE_DELIVERY.value: OperationalMeasurement.API_LATENCY,
    }
    try:
        return mapping[observation.operation]
    except KeyError as exc:
        raise ValueError(f"unsupported O01 operation: {observation.operation}") from exc


__all__ = [
    "InMemoryOperationalMetricsStore",
    "OperationalMetricsAccessDenied",
    "OperationalMetricsPage",
    "OperationalMetricsProjector",
    "OperationalMetricsQuery",
    "OperationalMetricsReader",
    "OperationalMetricsStore",
    "measurement_for_observation",
]
