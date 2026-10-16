"""Safe, versioned contracts for the operational correlation read model.

The correlation model is a derived administrative view.  It contains only
allow-listed identities, lifecycle outcomes and operational metadata; source
payloads, prompts, answers, provider errors and stack traces are deliberately
not part of the contract.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any, Literal
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator

from spine.domain.capabilities.answer_question import AnswerQuestionOutcome
from spine.domain.common import ValidatedDefinitionModel
from spine.domain.feedback import FeedbackOutcome, FeedbackTargetKind
from spine.domain.metrics import MetricCoverageState, OperationalMeasurement


OPERATIONAL_CORRELATION_RECORD_SCHEMA = "operational-correlation-record:v1"
OPERATIONAL_CORRELATION_VIEW_SCHEMA = "operational-correlation-view:v1"


class CorrelationReferenceKind(str, Enum):
    TRACE = "trace"
    CORRELATION = "correlation"
    RUN = "run"
    DELIVERY = "delivery"
    FEEDBACK = "feedback"


class CorrelationSourceKind(str, Enum):
    METRIC = "operational_metric"
    DELIVERY = "delivery_metadata"
    RUN = "canonical_run"
    USEFULNESS = "usefulness_feedback"


class CorrelationCoverageState(str, Enum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    UNAVAILABLE = "unavailable"
    DENIED = "denied"


def _non_zero(value: UUID, field_name: str) -> UUID:
    if value.int == 0:
        raise ValueError(f"{field_name} must be a non-zero UUID")
    return value


def _utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be an aware UTC timestamp")
    return value


class OperationalCorrelationQuery(ValidatedDefinitionModel):
    """One tenant-scoped lookup by a trusted reference identity."""

    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")

    schema_version: Literal["operational-correlation-query:v1"] = (
        "operational-correlation-query:v1"
    )
    workspace_id: UUID
    environment_id: UUID
    reference_kind: CorrelationReferenceKind
    reference_id: UUID
    limit: int = Field(default=100, ge=1, le=100)

    @field_validator("workspace_id", "environment_id", "reference_id")
    @classmethod
    def validate_ids(cls, value: UUID, info: Any) -> UUID:
        return _non_zero(value, info.field_name)


class OperationalCorrelationRecord(ValidatedDefinitionModel):
    """One immutable, deduplicated safe source observation."""

    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")

    schema_version: Literal[OPERATIONAL_CORRELATION_RECORD_SCHEMA] = (
        OPERATIONAL_CORRELATION_RECORD_SCHEMA
    )
    workspace_id: UUID
    environment_id: UUID
    source_kind: CorrelationSourceKind
    source_identity: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_.:-]{0,127}$")
    source_schema_version: str = Field(
        min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_.:-]{0,127}$"
    )
    source_record_id: UUID
    deduplication_key: str = Field(min_length=1, max_length=256)
    observed_at: datetime
    trace_id: UUID | None = None
    correlation_id: UUID | None = None
    run_id: UUID | None = None
    attempt_id: UUID | None = None
    message_id: UUID | None = None
    feedback_id: UUID | None = None
    delivery_event_id: UUID | None = None

    metric_measurement: OperationalMeasurement | None = None
    metric_coverage: MetricCoverageState | None = None
    metric_value: Decimal | None = Field(default=None, allow_inf_nan=False)
    metric_numerator: int | None = Field(default=None, ge=0)
    metric_denominator: int | None = Field(default=None, ge=0)
    metric_currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")

    delivery_status: Literal["quarantined"] | None = None
    delivery_attempt_count: int | None = Field(default=None, ge=1)
    delivery_created_at: datetime | None = None
    delivery_updated_at: datetime | None = None
    delivery_quarantined_at: datetime | None = None
    delivery_last_error_code: str | None = Field(
        default=None, pattern=r"^[a-z][a-z0-9_.:-]{0,127}$"
    )

    run_state: str | None = Field(default=None, min_length=1, max_length=32, pattern=r"^[a-z][a-z0-9_.:-]{0,31}$")
    run_outcome: AnswerQuestionOutcome | None = None
    feedback_target_kind: FeedbackTargetKind | None = None
    feedback_outcome: FeedbackOutcome | None = None

    @field_validator(
        "workspace_id",
        "environment_id",
        "source_record_id",
        "trace_id",
        "correlation_id",
        "run_id",
        "attempt_id",
        "message_id",
        "feedback_id",
        "delivery_event_id",
    )
    @classmethod
    def validate_ids(cls, value: UUID | None, info: Any) -> UUID | None:
        return None if value is None else _non_zero(value, info.field_name)

    @field_validator(
        "observed_at",
        "delivery_created_at",
        "delivery_updated_at",
        "delivery_quarantined_at",
    )
    @classmethod
    def validate_times(cls, value: datetime | None, info: Any) -> datetime | None:
        return None if value is None else _utc(value, info.field_name)

    @model_validator(mode="after")
    def validate_source_shape(self) -> "OperationalCorrelationRecord":
        if self.source_kind is CorrelationSourceKind.METRIC:
            if self.metric_measurement is None or self.metric_coverage is None:
                raise ValueError("metric records require measurement and coverage")
        elif self.source_kind is CorrelationSourceKind.DELIVERY:
            if self.delivery_event_id != self.source_record_id or self.delivery_status != "quarantined":
                raise ValueError("delivery records require quarantined delivery metadata")
            if self.delivery_attempt_count is None or self.delivery_quarantined_at is None:
                raise ValueError("delivery records require bounded quarantine metadata")
        elif self.source_kind is CorrelationSourceKind.RUN:
            if self.run_id != self.source_record_id or self.run_state is None:
                raise ValueError("run records require canonical run identity and state")
        elif self.source_kind is CorrelationSourceKind.USEFULNESS:
            if (
                self.feedback_id != self.source_record_id
                or self.feedback_target_kind is None
                or self.feedback_outcome is None
            ):
                raise ValueError("usefulness records require immutable feedback identity")
        if self.metric_measurement is not OperationalMeasurement.COST and self.metric_currency is not None:
            raise ValueError("metric currency is only valid for cost records")
        if self.feedback_outcome is not None and self.feedback_outcome.target_kind is not self.feedback_target_kind:
            raise ValueError("feedback target kind does not match feedback outcome")
        return self


class CorrelationSourceLineage(ValidatedDefinitionModel):
    """Safe lineage identity, without source payloads."""

    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")

    source_kind: CorrelationSourceKind
    source_identity: str = Field(min_length=1, max_length=128)
    source_schema_version: str = Field(min_length=1, max_length=128)
    source_record_id: UUID

    @field_validator("source_record_id")
    @classmethod
    def validate_id(cls, value: UUID) -> UUID:
        return _non_zero(value, "source_record_id")


class OperationalCorrelationView(ValidatedDefinitionModel):
    """Bounded read model returned to an authorized operator."""

    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")

    schema_version: Literal[OPERATIONAL_CORRELATION_VIEW_SCHEMA] = (
        OPERATIONAL_CORRELATION_VIEW_SCHEMA
    )
    workspace_id: UUID
    environment_id: UUID
    reference_kind: CorrelationReferenceKind
    reference_id: UUID
    coverage: CorrelationCoverageState
    records: tuple[OperationalCorrelationRecord, ...] = ()
    missing_sources: tuple[CorrelationSourceKind, ...] = ()
    lineage: tuple[CorrelationSourceLineage, ...] = ()
    trace_ids: tuple[UUID, ...] = ()
    correlation_ids: tuple[UUID, ...] = ()
    conflict_codes: tuple[str, ...] = ()

    @field_validator("workspace_id", "environment_id", "reference_id")
    @classmethod
    def validate_ids(cls, value: UUID, info: Any) -> UUID:
        return _non_zero(value, info.field_name)

    @field_validator("trace_ids", "correlation_ids")
    @classmethod
    def validate_reference_ids(cls, value: tuple[UUID, ...]) -> tuple[UUID, ...]:
        if any(item.int == 0 for item in value) or len(set(value)) != len(value):
            raise ValueError("correlation references must be unique non-zero UUIDs")
        return value

    @model_validator(mode="after")
    def validate_view(self) -> "OperationalCorrelationView":
        if len(self.records) > 100:
            raise ValueError("correlation view is bounded to 100 records")
        if any(
            record.workspace_id != self.workspace_id or record.environment_id != self.environment_id
            for record in self.records
        ):
            raise ValueError("correlation records must share the requested tenant scope")
        if self.coverage is CorrelationCoverageState.UNAVAILABLE and self.records:
            raise ValueError("unavailable correlation cannot contain records")
        if self.coverage is CorrelationCoverageState.COMPLETE and self.missing_sources:
            raise ValueError("complete correlation cannot have missing sources")
        return self


__all__ = [
    "CorrelationCoverageState",
    "CorrelationReferenceKind",
    "CorrelationSourceKind",
    "CorrelationSourceLineage",
    "OPERATIONAL_CORRELATION_RECORD_SCHEMA",
    "OPERATIONAL_CORRELATION_VIEW_SCHEMA",
    "OperationalCorrelationQuery",
    "OperationalCorrelationRecord",
    "OperationalCorrelationView",
]
