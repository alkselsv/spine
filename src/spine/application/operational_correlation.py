"""Application service for the tenant-scoped operational correlation view."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol
from uuid import UUID

from spine.application.outbox_inspection import OutboxDeliveryInspectionItem
from spine.application.persistence.context import TrustedPersistenceContext
from spine.application.telemetry.observations import OperationalObservation
from spine.auth import AuthorizationRole, AuthorizedRequestContext
from spine.domain.capabilities.answer_question_runs import AnswerQuestionRun
from spine.domain.feedback import FeedbackRecord
from spine.domain.metrics import MetricCoverageState, OperationalMetricEvent
from spine.domain.operational_correlation import (
    CorrelationCoverageState,
    CorrelationReferenceKind,
    CorrelationSourceKind,
    CorrelationSourceLineage,
    OperationalCorrelationQuery,
    OperationalCorrelationRecord,
    OperationalCorrelationView,
)


class OperationalCorrelationAccessDenied(PermissionError):
    """Safe authorization failure that does not disclose reference existence."""


class OperationalCorrelationStoreUnavailable(RuntimeError):
    """The derived read model cannot be read safely."""


class OperationalCorrelationStore(Protocol):
    async def append(self, record: OperationalCorrelationRecord) -> None: ...

    async def query(
        self, query: OperationalCorrelationQuery
    ) -> tuple[OperationalCorrelationRecord, ...]: ...


class OperationalCorrelationReaderPort(Protocol):
    async def read(
        self,
        *,
        query: OperationalCorrelationQuery,
        authority: AuthorizedRequestContext[object],
        context: TrustedPersistenceContext | None = None,
    ) -> OperationalCorrelationView: ...


class InMemoryOperationalCorrelationStore:
    """Deterministic fake for application and contract tests."""

    def __init__(self) -> None:
        self._records: dict[tuple[UUID, UUID, str], OperationalCorrelationRecord] = {}

    async def append(self, record: OperationalCorrelationRecord) -> None:
        key = (record.workspace_id, record.environment_id, record.deduplication_key)
        existing = self._records.get(key)
        if existing is not None:
            if existing != record:
                raise ValueError("correlation deduplication key was reused for different data")
            return
        self._records[key] = record

    async def query(
        self, query: OperationalCorrelationQuery
    ) -> tuple[OperationalCorrelationRecord, ...]:
        scoped = tuple(
            record
            for record in self._records.values()
            if record.workspace_id == query.workspace_id
            and record.environment_id == query.environment_id
        )
        values = _connected_records(
            scoped,
            seed=lambda record: _matches(record, query.reference_kind, query.reference_id),
        )
        return tuple(
            sorted(
                values,
                key=lambda item: (
                    item.observed_at,
                    item.source_kind.value,
                    item.source_record_id.int,
                ),
            )[: query.limit]
        )


class OperationalCorrelationProjector:
    """Materialize safe source facts without becoming a canonical authority."""

    def __init__(self, store: OperationalCorrelationStore) -> None:
        self._store = store

    async def observe_metric(self, event: OperationalMetricEvent) -> None:
        await self._store.append(
            OperationalCorrelationRecord(
                workspace_id=event.workspace_id,
                environment_id=event.environment_id,
                source_kind=CorrelationSourceKind.METRIC,
                source_identity=event.source_identity,
                source_schema_version=event.schema_version,
                source_record_id=event.event_id,
                deduplication_key=f"metric:{event.deduplication_key}",
                observed_at=event.event_at,
                trace_id=event.trace_id,
                correlation_id=event.correlation_id,
                run_id=event.logical_operation_id,
                attempt_id=event.attempt_id,
                metric_measurement=event.measurement,
                metric_coverage=(
                    MetricCoverageState.COMPLETE
                    if event.complete_coverage
                    else MetricCoverageState.DEGRADED
                ),
                metric_value=event.value,
                metric_currency=event.currency,
            )
        )

    async def observe_observation(self, observation: OperationalObservation) -> None:
        """Bridge O01 directly when the O02 projection is not composed yet."""

        event = OperationalMetricEvent.from_observation(
            observation,
            measurement=_measurement_for_observation(observation),
        )
        await self.observe_metric(event)

    async def observe_delivery(
        self,
        item: OutboxDeliveryInspectionItem,
        *,
        environment_id: UUID | None = None,
    ) -> None:
        resolved_environment = item.environment_id or environment_id
        if resolved_environment is None:
            raise ValueError("delivery correlation requires an environment scope")
        await self._store.append(
            OperationalCorrelationRecord(
                workspace_id=item.workspace_id,
                environment_id=resolved_environment,
                source_kind=CorrelationSourceKind.DELIVERY,
                source_identity="o06.outbox-delivery:v1",
                source_schema_version="outbox-delivery:v1",
                source_record_id=item.event_id,
                deduplication_key=(
                    f"delivery:{item.event_id}:{item.updated_at.isoformat()}"
                ),
                observed_at=item.updated_at,
                correlation_id=item.correlation_id,
                delivery_event_id=item.event_id,
                delivery_status=item.status,
                delivery_attempt_count=item.attempt_count,
                delivery_created_at=item.created_at,
                delivery_updated_at=item.updated_at,
                delivery_quarantined_at=item.quarantined_at,
                delivery_last_error_code=item.last_error_code,
            )
        )

    async def observe_run(self, run: AnswerQuestionRun) -> None:
        await self._store.append(
            OperationalCorrelationRecord(
                workspace_id=run.workspace_id,
                environment_id=run.environment_id,
                source_kind=CorrelationSourceKind.RUN,
                source_identity="canonical.answer-question-run:v1",
                source_schema_version=run.schema_version,
                source_record_id=run.run_id,
                deduplication_key=f"run:{run.run_id}",
                observed_at=run.completed_at or run.created_at,
                trace_id=run.diagnostic_context.trace_id,
                correlation_id=run.diagnostic_context.correlation_id,
                run_id=run.run_id,
                run_state=run.state.value,
                run_outcome=run.outcome,
            )
        )

    async def observe_feedback(self, feedback: FeedbackRecord) -> None:
        await self._store.append(
            OperationalCorrelationRecord(
                workspace_id=feedback.workspace_id,
                environment_id=feedback.environment_id,
                source_kind=CorrelationSourceKind.USEFULNESS,
                source_identity="canonical.qa-feedback:v1",
                source_schema_version=feedback.schema_version,
                source_record_id=feedback.feedback_id,
                deduplication_key=f"feedback:{feedback.feedback_id}:v{feedback.version}",
                observed_at=feedback.created_at,
                trace_id=feedback.trace_id,
                run_id=feedback.run_id,
                message_id=feedback.message_id,
                feedback_id=feedback.feedback_id,
                feedback_target_kind=feedback.target_kind,
                feedback_outcome=feedback.outcome,
            )
        )


class OperationalCorrelationReader:
    def __init__(self, *, store: OperationalCorrelationStore) -> None:
        self._store = store

    async def read(
        self,
        *,
        query: OperationalCorrelationQuery,
        authority: AuthorizedRequestContext[object],
        context: TrustedPersistenceContext | None = None,
    ) -> OperationalCorrelationView:
        del context
        if (
            authority.roles != frozenset({AuthorizationRole.ADMINISTRATOR})
            or authority.scope.workspace_id != query.workspace_id
            or authority.scope.environment_id != query.environment_id
        ):
            raise OperationalCorrelationAccessDenied(
                "Operational correlation is unavailable."
            )
        try:
            records = await self._store.query(query)
        except Exception as error:
            raise OperationalCorrelationStoreUnavailable(
                "Operational correlation is unavailable."
            ) from error
        return _view(query, records)


class UnavailableOperationalCorrelationReader:
    async def read(
        self,
        *,
        query: OperationalCorrelationQuery,
        authority: AuthorizedRequestContext[object],
        context: TrustedPersistenceContext | None = None,
    ) -> OperationalCorrelationView:
        del context
        if (
            authority.roles != frozenset({AuthorizationRole.ADMINISTRATOR})
            or authority.scope.workspace_id != query.workspace_id
            or authority.scope.environment_id != query.environment_id
        ):
            raise OperationalCorrelationAccessDenied(
                "Operational correlation is unavailable."
            )
        return OperationalCorrelationView(
            workspace_id=query.workspace_id,
            environment_id=query.environment_id,
            reference_kind=query.reference_kind,
            reference_id=query.reference_id,
            coverage=CorrelationCoverageState.UNAVAILABLE,
            missing_sources=tuple(CorrelationSourceKind),
        )


def _matches(
    record: OperationalCorrelationRecord,
    kind: CorrelationReferenceKind,
    reference_id: UUID,
) -> bool:
    return {
        CorrelationReferenceKind.TRACE: record.trace_id,
        CorrelationReferenceKind.CORRELATION: record.correlation_id,
        CorrelationReferenceKind.RUN: record.run_id,
        CorrelationReferenceKind.DELIVERY: record.delivery_event_id,
        CorrelationReferenceKind.FEEDBACK: record.feedback_id,
    }[kind] == reference_id


def _connected_records(
    records: tuple[OperationalCorrelationRecord, ...],
    *,
    seed: Callable[[OperationalCorrelationRecord], bool],
) -> tuple[OperationalCorrelationRecord, ...]:
    """Return the bounded connected component over trusted reference IDs."""

    selected = {record for record in records if seed(record)}
    changed = True
    while changed:
        changed = False
        references = {
            reference
            for record in selected
            for reference in _references(record)
        }
        for record in records:
            if record in selected:
                continue
            if references.intersection(_references(record)):
                selected.add(record)
                changed = True
    return tuple(selected)


def _references(record: OperationalCorrelationRecord) -> frozenset[tuple[str, UUID]]:
    values: list[tuple[str, UUID]] = []
    for kind, value in (
        ("trace", record.trace_id),
        ("correlation", record.correlation_id),
        ("run", record.run_id),
        ("attempt", record.attempt_id),
        ("message", record.message_id),
        ("feedback", record.feedback_id),
        ("delivery", record.delivery_event_id),
    ):
        if value is not None:
            values.append((kind, value))
    return frozenset(values)


def _view(
    query: OperationalCorrelationQuery,
    records: tuple[OperationalCorrelationRecord, ...],
) -> OperationalCorrelationView:
    if not records:
        return OperationalCorrelationView(
            workspace_id=query.workspace_id,
            environment_id=query.environment_id,
            reference_kind=query.reference_kind,
            reference_id=query.reference_id,
            coverage=CorrelationCoverageState.UNAVAILABLE,
            missing_sources=tuple(CorrelationSourceKind),
        )
    present = {record.source_kind for record in records}
    missing = tuple(kind for kind in CorrelationSourceKind if kind not in present)
    conflict_codes = _conflicts(records)
    coverage = (
        CorrelationCoverageState.COMPLETE
        if not missing and not conflict_codes
        else CorrelationCoverageState.INCOMPLETE
    )
    lineage = tuple(
        CorrelationSourceLineage(
            source_kind=record.source_kind,
            source_identity=record.source_identity,
            source_schema_version=record.source_schema_version,
            source_record_id=record.source_record_id,
        )
        for record in records
    )
    return OperationalCorrelationView(
        workspace_id=query.workspace_id,
        environment_id=query.environment_id,
        reference_kind=query.reference_kind,
        reference_id=query.reference_id,
        coverage=coverage,
        records=records,
        missing_sources=missing,
        lineage=lineage,
        trace_ids=tuple(sorted({item.trace_id for item in records if item.trace_id is not None}, key=lambda item: item.int)),
        correlation_ids=tuple(sorted({item.correlation_id for item in records if item.correlation_id is not None}, key=lambda item: item.int)),
        conflict_codes=conflict_codes,
    )


def _conflicts(records: tuple[OperationalCorrelationRecord, ...]) -> tuple[str, ...]:
    expected = {
        CorrelationSourceKind.METRIC: "operational-metric-event:v1",
        CorrelationSourceKind.DELIVERY: "outbox-delivery:v1",
        CorrelationSourceKind.RUN: "answer-question-run:v1",
        CorrelationSourceKind.USEFULNESS: "qa-feedback:v1",
    }
    conflicts: set[str] = set()
    for record in records:
        if record.source_schema_version != expected[record.source_kind]:
            conflicts.add("source_schema_version_mismatch")
        if record.metric_coverage in {
            MetricCoverageState.STALE,
            MetricCoverageState.DEGRADED,
            MetricCoverageState.UNAVAILABLE,
            MetricCoverageState.DENIED,
        }:
            conflicts.add("metric_coverage_incomplete")
    by_run: dict[UUID, set[object]] = {}
    for record in records:
        if record.run_id is not None and record.run_outcome is not None:
            by_run.setdefault(record.run_id, set()).add(record.run_outcome)
    if any(len(outcomes) > 1 for outcomes in by_run.values()):
        conflicts.add("conflicting_run_outcome")
    return tuple(sorted(conflicts))


def _measurement_for_observation(observation: OperationalObservation):
    from spine.application.telemetry.metrics import measurement_for_observation

    return measurement_for_observation(observation)


__all__ = [
    "InMemoryOperationalCorrelationStore",
    "OperationalCorrelationAccessDenied",
    "OperationalCorrelationProjector",
    "OperationalCorrelationReader",
    "OperationalCorrelationReaderPort",
    "OperationalCorrelationStore",
    "OperationalCorrelationStoreUnavailable",
    "UnavailableOperationalCorrelationReader",
]
