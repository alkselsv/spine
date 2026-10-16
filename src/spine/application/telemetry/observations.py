"""Closed OperationalObservation:v1 contract and R1 operating policy.

Observations are diagnostic evidence only. They are deliberately unsuitable as
canonical audit authority and never carry content or provider-owned payloads.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Literal, Protocol, runtime_checkable
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator

from spine.application.diagnostics import DiagnosticContext
from spine.auth import AuthorizationRole, AuthorizedRequestContext
from spine.domain.common import ValidatedDefinitionModel


OPERATIONAL_OBSERVATION_SCHEMA = "operational-observation:v1"
OPERATIONAL_TELEMETRY_POLICY_SCHEMA = "operational-telemetry-policy:v1"


class OperationalComponent(str, Enum):
    API = "api"
    OUTBOX = "outbox"
    WORKER = "worker"
    PROJECTION = "projection"
    RETRIEVAL = "retrieval"
    GENERATION = "generation"
    RUN = "run"
    SSE = "sse"


class OperationalStage(str, Enum):
    ACCEPTANCE = "acceptance"
    ENQUEUE = "enqueue"
    DISPATCH = "dispatch"
    PROJECTION = "projection"
    RETRIEVAL = "retrieval"
    GENERATION = "generation"
    PERSISTENCE = "persistence"
    DELIVERY = "delivery"


class OperationalOperation(str, Enum):
    HTTP_REQUEST = "http_request"
    OUTBOX_ENQUEUE = "outbox_enqueue"
    OUTBOX_DELIVERY = "outbox_delivery"
    PROJECTION_BUILD = "projection_build"
    CONTEXT_RETRIEVAL = "context_retrieval"
    ANSWER_GENERATION = "answer_generation"
    RUN_PERSISTENCE = "run_persistence"
    SSE_DELIVERY = "sse_delivery"


class OperationalObservationOutcome(str, Enum):
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RETRY_SCHEDULED = "retry_scheduled"
    QUARANTINED = "quarantined"
    DENIED = "denied"
    CANCELLED = "cancelled"


class SafeOperationalFailureCode(str, Enum):
    CONTEXT_INVALID = "context_invalid"
    CONTEXT_MISMATCH = "context_mismatch"
    ACCESS_DENIED = "access_denied"
    TIMEOUT = "timeout"
    RETRY_EXHAUSTED = "retry_exhausted"
    PROJECTION_UNAVAILABLE = "projection_unavailable"
    RETRIEVAL_UNAVAILABLE = "retrieval_unavailable"
    GENERATION_UNAVAILABLE = "generation_unavailable"
    PERSISTENCE_UNAVAILABLE = "persistence_unavailable"
    VALIDATION_FAILED = "validation_failed"
    INTERNAL_FAILURE = "internal_failure"


_FAILURE_OUTCOMES = frozenset(
    {
        OperationalObservationOutcome.FAILED,
        OperationalObservationOutcome.RETRY_SCHEDULED,
        OperationalObservationOutcome.QUARANTINED,
        OperationalObservationOutcome.DENIED,
    }
)


class OperationalMetricDimensions(ValidatedDefinitionModel):
    """The complete low-cardinality metric-label allowlist."""

    component: OperationalComponent
    stage: OperationalStage
    operation: OperationalOperation
    outcome: OperationalObservationOutcome
    safe_failure_code: SafeOperationalFailureCode | None = None


@dataclass(frozen=True, slots=True)
class _TrustedObservationContext:
    workspace_id: UUID
    environment_id: UUID
    trace_id: UUID
    correlation_id: UUID | None
    causation_id: UUID | None


class OperationalObservation(ValidatedDefinitionModel):
    """One immutable, tenant-scoped, content-free operational observation."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        revalidate_instances="always",
        arbitrary_types_allowed=True,
    )

    schema_version: Literal[OPERATIONAL_OBSERVATION_SCHEMA] = (
        OPERATIONAL_OBSERVATION_SCHEMA
    )
    observation_id: UUID
    observed_at: datetime
    workspace_id: UUID
    environment_id: UUID
    trace_id: UUID
    correlation_id: UUID | None = None
    causation_id: UUID | None = None
    component: OperationalComponent
    stage: OperationalStage
    operation: OperationalOperation
    outcome: OperationalObservationOutcome
    safe_failure_code: SafeOperationalFailureCode | None = None
    duration_ms: int | None = Field(default=None, ge=0, le=86_400_000)
    attempt_number: int | None = Field(default=None, ge=1, le=10_000)
    run_id: UUID | None = None
    attempt_id: UUID | None = None
    message_id: UUID | None = None
    issued_context: _TrustedObservationContext = Field(exclude=True, repr=False)

    @field_validator(
        "observation_id",
        "workspace_id",
        "environment_id",
        "trace_id",
        "correlation_id",
        "causation_id",
        "run_id",
        "attempt_id",
        "message_id",
    )
    @classmethod
    def reject_zero_id(cls, value: UUID | None) -> UUID | None:
        if value is not None and value.int == 0:
            raise ValueError("operational observation identifiers must be non-zero")
        return value

    @field_validator("observed_at")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("observed_at must be an aware UTC timestamp")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def validate_failure_code(self) -> "OperationalObservation":
        if (self.outcome in _FAILURE_OUTCOMES) != (
            self.safe_failure_code is not None
        ):
            raise ValueError(
                "failure outcomes require exactly one closed safe failure code"
            )
        return self

    def metric_dimensions(self) -> OperationalMetricDimensions:
        return OperationalMetricDimensions(
            component=self.component,
            stage=self.stage,
            operation=self.operation,
            outcome=self.outcome,
            safe_failure_code=self.safe_failure_code,
        )


class OperationalObservationRegistry:
    """Trusted construction and revalidation boundary for the v1 schema."""

    def build(
        self,
        *,
        observation_id: UUID,
        observed_at: datetime,
        workspace_id: UUID,
        environment_id: UUID,
        diagnostic_context: DiagnosticContext,
        component: OperationalComponent,
        stage: OperationalStage,
        operation: OperationalOperation,
        outcome: OperationalObservationOutcome,
        safe_failure_code: SafeOperationalFailureCode | None = None,
        duration_ms: int | None = None,
        attempt_number: int | None = None,
        run_id: UUID | None = None,
        attempt_id: UUID | None = None,
        message_id: UUID | None = None,
    ) -> OperationalObservation:
        if type(diagnostic_context) is not DiagnosticContext:
            raise ValueError("trusted diagnostic context is required")
        issued = _TrustedObservationContext(
            workspace_id=workspace_id,
            environment_id=environment_id,
            trace_id=diagnostic_context.trace_id,
            correlation_id=diagnostic_context.correlation_id,
            causation_id=diagnostic_context.causation_id,
        )
        return self.validate(
            OperationalObservation(
                observation_id=observation_id,
                observed_at=observed_at,
                workspace_id=workspace_id,
                environment_id=environment_id,
                trace_id=diagnostic_context.trace_id,
                correlation_id=diagnostic_context.correlation_id,
                causation_id=diagnostic_context.causation_id,
                component=component,
                stage=stage,
                operation=operation,
                outcome=outcome,
                safe_failure_code=safe_failure_code,
                duration_ms=duration_ms,
                attempt_number=attempt_number,
                run_id=run_id,
                attempt_id=attempt_id,
                message_id=message_id,
                issued_context=issued,
            )
        )

    def validate(self, observation: OperationalObservation) -> OperationalObservation:
        if type(observation) is not OperationalObservation:
            raise ValueError("operational observation must use the canonical schema")
        issued = observation.issued_context
        if type(issued) is not _TrustedObservationContext or (
            observation.workspace_id,
            observation.environment_id,
            observation.trace_id,
            observation.correlation_id,
            observation.causation_id,
        ) != (
            issued.workspace_id,
            issued.environment_id,
            issued.trace_id,
            issued.correlation_id,
            issued.causation_id,
        ):
            raise ValueError("operational observation context is invalid")
        return OperationalObservation.model_validate(
            observation.model_dump(mode="python", round_trip=True)
            | {"issued_context": issued},
            strict=True,
        )


class OperationalObservationPolicy(ValidatedDefinitionModel):
    """The approved, explicit R1 retention/export/sampling profile."""

    schema_version: Literal[OPERATIONAL_TELEMETRY_POLICY_SCHEMA] = (
        OPERATIONAL_TELEMETRY_POLICY_SCHEMA
    )
    retention_days: Literal[30] = 30
    error_sample_percent: Literal[100] = 100
    successful_trace_sample_percent: Literal[10] = 10
    exporter: Literal["disabled"] = "disabled"

    @property
    def retention(self) -> timedelta:
        return timedelta(days=self.retention_days)

    def retains(self, observation: OperationalObservation) -> bool:
        if observation.outcome in _FAILURE_OUTCOMES:
            return True
        return observation.trace_id.int % 100 < self.successful_trace_sample_percent


@runtime_checkable
class OperationalObservationSink(Protocol):
    async def emit(self, observation: OperationalObservation) -> None: ...


class RecordingOperationalObservationSink:
    def __init__(self, registry: OperationalObservationRegistry) -> None:
        self._registry = registry
        self._observations: list[OperationalObservation] = []

    @property
    def observations(self) -> tuple[OperationalObservation, ...]:
        return tuple(self._observations)

    async def emit(self, observation: OperationalObservation) -> None:
        self._observations.append(self._registry.validate(observation))


class DisabledOperationalObservationExporter:
    """Explicit R1 profile: no observation leaves the Spine deployment."""

    enabled = False

    async def emit(self, observation: OperationalObservation) -> None:
        del observation


class OperationalObservationEmitter:
    """Build, sample and best-effort emit safe observations."""

    def __init__(
        self,
        *,
        registry: OperationalObservationRegistry,
        sink: OperationalObservationSink,
        policy: OperationalObservationPolicy,
        clock: Callable[[], datetime],
        observation_id_source: Callable[[], UUID],
    ) -> None:
        self._registry = registry
        self._sink = sink
        self._policy = policy
        self._clock = clock
        self._observation_id_source = observation_id_source
        self._pending_traces: dict[UUID, list[OperationalObservation]] = {}
        self._error_traces: set[UUID] = set()

    async def emit(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        diagnostic_context: DiagnosticContext,
        component: OperationalComponent,
        stage: OperationalStage,
        operation: OperationalOperation,
        outcome: OperationalObservationOutcome,
        safe_failure_code: SafeOperationalFailureCode | None = None,
        duration_ms: int | None = None,
        attempt_number: int | None = None,
        run_id: UUID | None = None,
        attempt_id: UUID | None = None,
        message_id: UUID | None = None,
    ) -> bool:
        # Contract and trusted-context failures are fail-closed. Only failures
        # in the diagnostic sink itself are isolated from the business effect.
        observation = self._registry.build(
            observation_id=self._observation_id_source(),
            observed_at=self._clock(),
            workspace_id=workspace_id,
            environment_id=environment_id,
            diagnostic_context=diagnostic_context,
            component=component,
            stage=stage,
            operation=operation,
            outcome=outcome,
            safe_failure_code=safe_failure_code,
            duration_ms=duration_ms,
            attempt_number=attempt_number,
            run_id=run_id,
            attempt_id=attempt_id,
            message_id=message_id,
        )
        if observation.outcome in _FAILURE_OUTCOMES:
            self._error_traces.add(observation.trace_id)
            pending = self._pending_traces.pop(observation.trace_id, [])
            emitted = True
            for item in (*pending, observation):
                emitted = await self._emit_one(item) and emitted
            return emitted
        if observation.trace_id in self._error_traces:
            return await self._emit_one(observation)
        if self._policy.retains(observation):
            return await self._emit_one(observation)
        pending = self._pending_traces.setdefault(observation.trace_id, [])
        pending.append(observation)
        if (
            observation.operation is OperationalOperation.SSE_DELIVERY
            or (
                observation.component is OperationalComponent.WORKER
                and observation.operation is OperationalOperation.OUTBOX_DELIVERY
                and observation.outcome is OperationalObservationOutcome.SUCCEEDED
            )
            or observation.outcome is OperationalObservationOutcome.CANCELLED
        ):
            self._pending_traces.pop(observation.trace_id, None)
        return False

    async def _emit_one(self, observation: OperationalObservation) -> bool:
        try:
            await self._sink.emit(observation)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            return False
        return True


class OperationalObservationQuery(ValidatedDefinitionModel):
    schema_version: Literal["operational-observation-query:v1"] = (
        "operational-observation-query:v1"
    )
    workspace_id: UUID
    environment_id: UUID
    trace_id: UUID | None = None
    observed_from: datetime | None = None
    observed_until: datetime | None = None
    limit: int = Field(default=100, ge=1, le=1000)

    @field_validator("workspace_id", "environment_id", "trace_id")
    @classmethod
    def reject_zero_query_id(cls, value: UUID | None) -> UUID | None:
        if value is not None and value.int == 0:
            raise ValueError("operational query identifiers must be non-zero")
        return value

    @field_validator("observed_from", "observed_until")
    @classmethod
    def require_query_utc(cls, value: datetime | None) -> datetime | None:
        if value is not None and (
            value.tzinfo is None or value.utcoffset() != timedelta(0)
        ):
            raise ValueError("operational query timestamps must be UTC")
        return value

    @model_validator(mode="after")
    def validate_window(self) -> "OperationalObservationQuery":
        if (
            self.observed_from is not None
            and self.observed_until is not None
            and self.observed_from >= self.observed_until
        ):
            raise ValueError("operational query window must be half-open and non-empty")
        return self


class OperationalObservationPage(ValidatedDefinitionModel):
    schema_version: Literal["operational-observation-page:v1"] = (
        "operational-observation-page:v1"
    )
    observations: tuple[OperationalObservation, ...]


class OperationalObservationStore(Protocol):
    async def query(
        self,
        query: OperationalObservationQuery,
        *,
        now: datetime,
        retention: timedelta,
    ) -> tuple[OperationalObservation, ...]: ...


class InMemoryOperationalObservationStore:
    """Deterministic R1 store and qualification adapter."""

    def __init__(self, registry: OperationalObservationRegistry) -> None:
        self._registry = registry
        self._observations: list[OperationalObservation] = []
        self._legal_holds: set[UUID] = set()

    async def emit(self, observation: OperationalObservation) -> None:
        self._observations.append(self._registry.validate(observation))

    def place_legal_hold(self, observation_id: UUID) -> None:
        if observation_id.int == 0:
            raise ValueError("legal hold observation identity must be non-zero")
        self._legal_holds.add(observation_id)

    def release_legal_hold(self, observation_id: UUID) -> None:
        self._legal_holds.discard(observation_id)

    async def query(
        self,
        query: OperationalObservationQuery,
        *,
        now: datetime,
        retention: timedelta,
    ) -> tuple[OperationalObservation, ...]:
        cutoff = now - retention
        self._observations = [
            item
            for item in self._observations
            if item.observed_at >= cutoff or item.observation_id in self._legal_holds
        ]
        values = [
            item
            for item in self._observations
            if item.workspace_id == query.workspace_id
            and item.environment_id == query.environment_id
            and (query.trace_id is None or item.trace_id == query.trace_id)
            and (
                query.observed_from is None
                or item.observed_at >= query.observed_from
            )
            and (
                query.observed_until is None
                or item.observed_at < query.observed_until
            )
        ]
        values.sort(key=lambda item: (item.observed_at, item.observation_id.int))
        return tuple(values[: query.limit])


class OperationalTelemetryAccessDenied(PermissionError):
    """Safe denial without disclosing whether observations exist."""


class OperationalObservationReader:
    def __init__(
        self,
        *,
        store: OperationalObservationStore,
        clock: Callable[[], datetime],
        policy: OperationalObservationPolicy | None = None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._policy = policy or OperationalObservationPolicy()

    async def read(
        self,
        *,
        query: OperationalObservationQuery,
        authority: AuthorizedRequestContext[object],
    ) -> OperationalObservationPage:
        if (
            authority.roles != frozenset({AuthorizationRole.ADMINISTRATOR})
            or authority.scope.workspace_id != query.workspace_id
            or authority.scope.environment_id != query.environment_id
        ):
            raise OperationalTelemetryAccessDenied(
                "Operational observations are unavailable."
            )
        observations = await self._store.query(
            query,
            now=self._clock(),
            retention=self._policy.retention,
        )
        return OperationalObservationPage(observations=observations)
