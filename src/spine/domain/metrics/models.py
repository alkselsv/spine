"""Pure, versioned contracts for operational metrics.

The models deliberately contain only safe operational facts.  They do not
carry prompts, answers, source text, provider errors or diagnostic payloads.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Literal, Protocol
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from spine.domain.capabilities.answer_question import AnswerQuestionOutcome
from spine.domain.common import ValidatedDefinitionModel
from spine.domain.capabilities.answer_question_runs import AnswerQuestionAttempt, AnswerQuestionRun
from spine.domain.feedback import FeedbackOutcome, FeedbackRecord


OPERATIONAL_METRIC_SCHEMA = "operational-metric:v1"
OPERATIONAL_METRIC_EVENT_SCHEMA = "operational-metric-event:v1"
_IDENTIFIER = r"^[a-z][a-z0-9_.:-]{0,127}$"


class MetricCoverageState(str, Enum):
    COMPLETE = "complete"
    STALE = "stale"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    DENIED = "denied"


class OperationalMeasurement(str, Enum):
    ANSWER_RATE = "qa.answer_rate"
    API_LATENCY = "api.latency"
    INGESTION_LATENCY = "ingestion.processing_latency"
    PROJECTION_LATENCY = "projection.publication_latency"
    RETRIEVAL_LATENCY = "qa.retrieval_latency"
    GENERATION_LATENCY = "qa.generation_latency"
    QA_LATENCY = "qa.terminal_latency"
    INGESTION_PROCESSING_LAG = "ingestion.processing_lag"
    PROJECTION_INDEXING_LAG = "projection.indexing_lag"
    INGESTION_SEQUENCE_GAP = "ingestion.sequence_gap"
    PROJECTION_SEQUENCE_GAP = "projection.sequence_gap"
    RETRY_ATTEMPTS = "operations.retry_attempts"
    COST = "qa.cost"
    USEFULNESS = "qa.usefulness"


class _ObservationOutcome(Protocol):
    value: str


class OperationalObservationLike(Protocol):
    observation_id: UUID
    workspace_id: UUID
    environment_id: UUID
    observed_at: datetime
    outcome: _ObservationOutcome
    component: str
    stage: str
    operation: str
    run_id: UUID | None
    attempt_id: UUID | None
    attempt_number: int | None
    duration_ms: int | None


def _non_zero(value: UUID, field_name: str) -> UUID:
    if value.int == 0:
        raise ValueError(f"{field_name} must be non-zero")
    return value


def _utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be an aware UTC timestamp")
    return value.astimezone(timezone.utc)


class MetricWindow(ValidatedDefinitionModel):
    """A non-empty half-open UTC interval ``[start, end)``."""

    start: datetime
    end: datetime

    @field_validator("start", "end")
    @classmethod
    def validate_utc(cls, value: datetime, info: Any) -> datetime:
        return _utc(value, info.field_name)

    @model_validator(mode="after")
    def validate_order(self) -> "MetricWindow":
        if self.start >= self.end:
            raise ValueError("metric window must be non-empty and half-open")
        return self


class OperationalMetricEvent(ValidatedDefinitionModel):
    """One safe source fact accepted by the metrics projector."""

    schema_version: Literal[OPERATIONAL_METRIC_EVENT_SCHEMA] = OPERATIONAL_METRIC_EVENT_SCHEMA
    event_id: UUID
    workspace_id: UUID
    environment_id: UUID
    source_identity: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER)
    measurement: OperationalMeasurement
    event_at: datetime
    deduplication_key: str = Field(min_length=1, max_length=256)
    logical_operation_id: UUID | None = None
    attempt_id: UUID | None = None
    attempt_number: int | None = Field(default=None, ge=1, le=10_000)
    terminal: bool = False
    trusted: bool = True
    complete_coverage: bool = True
    duration_ms: int | None = Field(default=None, ge=0, le=86_400_000)
    sequence_number: int | None = Field(default=None, ge=0)
    source_watermark_at: datetime | None = None
    projection_watermark_at: datetime | None = None
    value: Decimal | None = Field(default=None, allow_inf_nan=False)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    answer_outcome: AnswerQuestionOutcome | None = None
    accepted: bool | None = None
    execution_started: bool | None = None
    accepted_evidence: bool | None = None
    user_initiated: bool | None = None
    feedback_id: UUID | None = None
    feedback_outcome: FeedbackOutcome | None = None

    @field_validator("event_id", "workspace_id", "environment_id", "logical_operation_id", "attempt_id")
    @classmethod
    def validate_ids(cls, value: UUID | None, info: Any) -> UUID | None:
        return None if value is None else _non_zero(value, info.field_name)

    @field_validator("event_at", "source_watermark_at", "projection_watermark_at")
    @classmethod
    def validate_times(cls, value: datetime | None, info: Any) -> datetime | None:
        return None if value is None else _utc(value, info.field_name)

    @model_validator(mode="after")
    def validate_shape(self) -> "OperationalMetricEvent":
        if not self.trusted:
            raise ValueError("untrusted metric events cannot enter the read model")
        if self.measurement is OperationalMeasurement.COST:
            if self.value is None or self.value < 0 or self.currency is None:
                raise ValueError("cost events require a non-negative value and currency")
        elif self.currency is not None:
            raise ValueError("currency is only valid for cost events")
        if self.measurement in {
            OperationalMeasurement.INGESTION_PROCESSING_LAG,
            OperationalMeasurement.PROJECTION_INDEXING_LAG,
        } and (self.source_watermark_at is None or self.projection_watermark_at is None):
            raise ValueError("lag events require both trusted watermark timestamps")
        if (
            self.measurement in {
                OperationalMeasurement.INGESTION_PROCESSING_LAG,
                OperationalMeasurement.PROJECTION_INDEXING_LAG,
            }
            and self.projection_watermark_at < self.source_watermark_at
        ):
            raise ValueError("projection watermark cannot precede source watermark")
        if self.measurement in {
            OperationalMeasurement.INGESTION_SEQUENCE_GAP,
            OperationalMeasurement.PROJECTION_SEQUENCE_GAP,
        } and self.sequence_number is None:
            raise ValueError("sequence-gap events require a trusted sequence number")
        if self.measurement is OperationalMeasurement.ANSWER_RATE:
            if any(value is None for value in (self.answer_outcome, self.accepted, self.execution_started, self.accepted_evidence, self.user_initiated)):
                raise ValueError("answer-rate events require canonical run classification")
        elif any(value is not None for value in (self.answer_outcome, self.accepted, self.execution_started, self.accepted_evidence, self.user_initiated)):
            raise ValueError("answer-rate classification is only valid for answer-rate events")
        if self.measurement is OperationalMeasurement.USEFULNESS:
            if self.feedback_id is None or self.feedback_outcome is None:
                raise ValueError("usefulness events require immutable feedback identity")
            if self.source_identity != "canonical.qa-feedback:v1":
                raise ValueError("usefulness events require the canonical feedback source")
        elif self.feedback_id is not None or self.feedback_outcome is not None:
            raise ValueError("feedback identity is only valid for usefulness events")
        return self

    @classmethod
    def from_observation(
        cls,
        observation: OperationalObservationLike,
        *,
        measurement: OperationalMeasurement,
        source_identity: str = "o01.operational-observation:v1",
    ) -> "OperationalMetricEvent":
        """Map the approved O01 observation without copying protected payloads."""

        return cls(
            event_id=observation.observation_id,
            workspace_id=observation.workspace_id,
            environment_id=observation.environment_id,
            source_identity=source_identity,
            measurement=measurement,
            event_at=observation.observed_at,
            deduplication_key=f"observation:{observation.observation_id}:{measurement.value}",
            logical_operation_id=observation.run_id or observation.observation_id,
            attempt_id=observation.attempt_id,
            attempt_number=observation.attempt_number,
            terminal=observation.outcome.value
            in {"succeeded", "failed", "quarantined", "denied", "cancelled"},
            duration_ms=observation.duration_ms,
        )

    @classmethod
    def from_answer_rate_fact(cls, fact: "AnswerRateFact") -> "OperationalMetricEvent":
        return cls(
            event_id=fact.run_id,
            workspace_id=fact.workspace_id,
            environment_id=fact.environment_id,
            source_identity="canonical.answer-question-run:v1",
            measurement=OperationalMeasurement.ANSWER_RATE,
            event_at=fact.event_at,
            deduplication_key=f"answer-rate:{fact.run_id}",
            logical_operation_id=fact.run_id,
            terminal=True,
            answer_outcome=fact.outcome,
            accepted=fact.accepted,
            execution_started=fact.execution_started,
            accepted_evidence=fact.accepted_evidence,
            user_initiated=fact.user_initiated,
        )

    @classmethod
    def from_feedback(cls, feedback: FeedbackRecord) -> "OperationalMetricEvent":
        return cls(
            event_id=feedback.feedback_id,
            workspace_id=feedback.workspace_id,
            environment_id=feedback.environment_id,
            source_identity="canonical.qa-feedback:v1",
            measurement=OperationalMeasurement.USEFULNESS,
            event_at=feedback.created_at,
            deduplication_key=f"feedback:{feedback.feedback_id}:{feedback.version}",
            feedback_id=feedback.feedback_id,
            feedback_outcome=feedback.outcome,
        )

    @classmethod
    def from_answer_question_attempt(
        cls,
        attempt: AnswerQuestionAttempt,
    ) -> "OperationalMetricEvent | None":
        """Project exact canonical usage without provider payloads."""

        if attempt.usage.cost == 0:
            return None
        return cls(
            event_id=attempt.attempt_id,
            workspace_id=attempt.workspace_id,
            environment_id=attempt.environment_id,
            source_identity="canonical.answer-question-usage:v1",
            measurement=OperationalMeasurement.COST,
            event_at=attempt.completed_at or attempt.started_at,
            deduplication_key=f"answer-question-usage:{attempt.attempt_id}",
            logical_operation_id=attempt.run_id,
            attempt_id=attempt.attempt_id,
            attempt_number=attempt.attempt_number,
            terminal=attempt.state.value != "running",
            value=attempt.usage.cost,
            currency=attempt.usage.currency,
        )


class FeedbackMetricFact(ValidatedDefinitionModel):
    """The immutable, content-free portion of a #298 feedback record."""

    feedback_id: UUID
    workspace_id: UUID
    environment_id: UUID
    event_at: datetime
    outcome: FeedbackOutcome

    @field_validator("feedback_id", "workspace_id", "environment_id")
    @classmethod
    def validate_ids(cls, value: UUID, info: Any) -> UUID:
        return _non_zero(value, info.field_name)

    @field_validator("event_at")
    @classmethod
    def validate_event_at(cls, value: datetime) -> datetime:
        return _utc(value, "event_at")

    @classmethod
    def from_feedback(cls, feedback: FeedbackRecord) -> "FeedbackMetricFact":
        return cls(
            feedback_id=feedback.feedback_id,
            workspace_id=feedback.workspace_id,
            environment_id=feedback.environment_id,
            event_at=feedback.created_at,
            outcome=feedback.outcome,
        )


class AnswerRateFact(ValidatedDefinitionModel):
    """Explicit canonical Q&A classification used by the answer-rate metric."""

    run_id: UUID
    workspace_id: UUID
    environment_id: UUID
    event_at: datetime
    outcome: AnswerQuestionOutcome
    accepted: bool
    execution_started: bool
    accepted_evidence: bool = False
    user_initiated: bool = True

    @field_validator("run_id", "workspace_id", "environment_id")
    @classmethod
    def validate_ids(cls, value: UUID, info: Any) -> UUID:
        return _non_zero(value, info.field_name)

    @field_validator("event_at")
    @classmethod
    def validate_event_at(cls, value: datetime) -> datetime:
        return _utc(value, "event_at")

    @model_validator(mode="after")
    def validate_classification(self) -> "AnswerRateFact":
        if self.accepted_evidence and (
            not self.accepted
            or not self.execution_started
            or self.outcome is not AnswerQuestionOutcome.ANSWERED
        ):
            raise ValueError("accepted evidence requires an accepted completed run")
        return self

    @classmethod
    def from_run(
        cls,
        run: AnswerQuestionRun,
        *,
        accepted: bool = True,
        execution_started: bool = True,
        accepted_evidence: bool = False,
    ) -> "AnswerRateFact":
        if run.state.value != "completed" or run.outcome is None or run.completed_at is None:
            raise ValueError("answer-rate facts require a terminal canonical run")
        return cls(
            run_id=run.run_id,
            workspace_id=run.workspace_id,
            environment_id=run.environment_id,
            event_at=run.completed_at,
            outcome=run.outcome,
            accepted=accepted,
            execution_started=execution_started,
            accepted_evidence=accepted_evidence,
            user_initiated=run.subject.kind.value == "human",
        )

class OperationalMetricMeasurement(ValidatedDefinitionModel):
    """One published measurement, including its data-quality state."""

    schema_version: Literal[OPERATIONAL_METRIC_SCHEMA] = OPERATIONAL_METRIC_SCHEMA
    source_identity: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER)
    measurement_identity: OperationalMeasurement
    workspace_id: UUID
    environment_id: UUID
    window: MetricWindow
    aggregation_revision: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER)
    deduplication_identity: str = Field(min_length=1, max_length=256)
    coverage: MetricCoverageState
    value: Decimal | None = Field(default=None, allow_inf_nan=False)
    numerator: int | None = Field(default=None, ge=0)
    denominator: int | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    event_timestamp: datetime | None = None
    source_watermark_at: datetime | None = None
    projection_watermark_at: datetime | None = None
    sequence_gap: int | None = Field(default=None, ge=0)

    @field_validator("workspace_id", "environment_id")
    @classmethod
    def validate_scope(cls, value: UUID, info: Any) -> UUID:
        return _non_zero(value, info.field_name)

    @field_validator("event_timestamp", "source_watermark_at", "projection_watermark_at")
    @classmethod
    def validate_optional_utc(cls, value: datetime | None, info: Any) -> datetime | None:
        return None if value is None else _utc(value, info.field_name)

    @model_validator(mode="after")
    def validate_measurement(self) -> "OperationalMetricMeasurement":
        if self.measurement_identity is OperationalMeasurement.ANSWER_RATE:
            if (
                self.coverage not in {MetricCoverageState.UNAVAILABLE, MetricCoverageState.DENIED}
                and (self.numerator is None or self.denominator is None)
            ):
                raise ValueError("answer rate requires numerator and denominator")
        elif self.numerator is not None or self.denominator is not None:
            raise ValueError("numerator and denominator are only valid for answer rate")
        if (
            self.measurement_identity is OperationalMeasurement.COST
            and self.coverage not in {MetricCoverageState.UNAVAILABLE, MetricCoverageState.DENIED}
            and self.currency is None
        ):
            raise ValueError("cost measurements require currency")
        if self.coverage is MetricCoverageState.DENIED and any(
            value is not None
            for value in (self.value, self.numerator, self.denominator, self.currency)
        ):
            raise ValueError("denied measurements cannot disclose values")
        return self


__all__ = [
    "AnswerRateFact",
    "FeedbackMetricFact",
    "MetricCoverageState",
    "MetricWindow",
    "OPERATIONAL_METRIC_EVENT_SCHEMA",
    "OPERATIONAL_METRIC_SCHEMA",
    "OperationalMetricEvent",
    "OperationalObservationLike",
    "OperationalMetricMeasurement",
    "OperationalMeasurement",
]
