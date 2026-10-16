"""PostgreSQL adapter for the safe operational correlation read model."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Protocol

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Result

from spine.application.operational_correlation import OperationalCorrelationStore
from spine.domain.operational_correlation import (
    CorrelationReferenceKind,
    OperationalCorrelationQuery,
    OperationalCorrelationRecord,
)
from spine.infrastructure.persistence.postgresql_mappings import (
    operational_correlation_records,
)


class PostgreSQLOperationalCorrelationExecutor(Protocol):
    def _guard_active(self) -> None: ...

    async def _execute(self, statement: object) -> Result: ...


class PostgreSQLOperationalCorrelationStore(OperationalCorrelationStore):
    """Insert-only, tenant-scoped storage for derived correlation facts."""

    def __init__(self, uow: PostgreSQLOperationalCorrelationExecutor) -> None:
        self._uow = uow

    async def append(self, record: OperationalCorrelationRecord) -> None:
        self._uow._guard_active()
        existing = await self._uow._execute(
            select(operational_correlation_records).where(
                operational_correlation_records.c.workspace_id == record.workspace_id,
                operational_correlation_records.c.environment_id == record.environment_id,
                operational_correlation_records.c.deduplication_key == record.deduplication_key,
            )
        )
        row = existing.mappings().first()
        if row is not None:
            if _record_from_row(row) != record:
                raise ValueError("correlation deduplication key was reused for different data")
            return
        await self._uow._execute(
            insert(operational_correlation_records)
            .values(_values(record))
            .on_conflict_do_nothing(constraint="uq_operational_correlation_records_deduplication")
        )
        persisted = await self._uow._execute(
            select(operational_correlation_records).where(
                operational_correlation_records.c.workspace_id == record.workspace_id,
                operational_correlation_records.c.environment_id == record.environment_id,
                operational_correlation_records.c.deduplication_key == record.deduplication_key,
            )
        )
        row = persisted.mappings().first()
        if row is None or _record_from_row(row) != record:
            raise ValueError("correlation deduplication key was reused for different data")

    async def query(
        self, query: OperationalCorrelationQuery
    ) -> tuple[OperationalCorrelationRecord, ...]:
        self._uow._guard_active()
        refs: set[tuple[str, object]] = {(query.reference_kind.value, query.reference_id)}
        values: dict[tuple[object, ...], OperationalCorrelationRecord] = {}
        for _ in range(8):
            predicates = _reference_predicates(refs)
            if not predicates:
                break
            result = await self._uow._execute(
                select(operational_correlation_records)
                .where(
                    operational_correlation_records.c.workspace_id == query.workspace_id,
                    operational_correlation_records.c.environment_id == query.environment_id,
                    or_(*predicates),
                )
                .order_by(
                    operational_correlation_records.c.observed_at,
                    operational_correlation_records.c.source_kind,
                    operational_correlation_records.c.source_record_id,
                )
                .limit(query.limit)
            )
            rows = tuple(_record_from_row(row) for row in result.mappings().all())
            before = len(values)
            for record in rows:
                key = (
                    record.source_kind,
                    record.source_record_id,
                    record.deduplication_key,
                )
                values[key] = record
                refs.update(_record_references(record))
            if len(values) == before:
                break
        return tuple(
            sorted(
                values.values(),
                key=lambda item: (
                    item.observed_at,
                    item.source_kind.value,
                    item.source_record_id.int,
                ),
            )[: query.limit]
        )


def _reference_predicates(refs: set[tuple[str, object]]) -> list[object]:
    columns = {
        CorrelationReferenceKind.TRACE.value: operational_correlation_records.c.trace_id,
        CorrelationReferenceKind.CORRELATION.value: operational_correlation_records.c.correlation_id,
        CorrelationReferenceKind.RUN.value: operational_correlation_records.c.run_id,
        "attempt": operational_correlation_records.c.attempt_id,
        "message": operational_correlation_records.c.message_id,
        CorrelationReferenceKind.FEEDBACK.value: operational_correlation_records.c.feedback_id,
        CorrelationReferenceKind.DELIVERY.value: operational_correlation_records.c.delivery_event_id,
    }
    grouped: dict[str, list[object]] = {}
    for kind, value in refs:
        grouped.setdefault(kind, []).append(value)
    return [columns[kind].in_(values) for kind, values in grouped.items() if kind in columns]


def _record_references(record: OperationalCorrelationRecord) -> set[tuple[str, object]]:
    return {
        (kind, value)
        for kind, value in (
            ("trace", record.trace_id),
            ("correlation", record.correlation_id),
            ("run", record.run_id),
            ("attempt", record.attempt_id),
            ("message", record.message_id),
            ("feedback", record.feedback_id),
            ("delivery", record.delivery_event_id),
        )
        if value is not None
    }


def _values(record: OperationalCorrelationRecord) -> dict[str, object]:
    values = record.model_dump(mode="python")
    values.pop("schema_version")
    for key in ("metric_measurement", "metric_coverage", "run_outcome", "feedback_target_kind", "feedback_outcome", "source_kind"):
        value = values.get(key)
        if value is not None:
            values[key] = value.value
    if values.get("metric_value") is not None:
        values["metric_value"] = format(values["metric_value"], "f")
    return values


def _record_from_row(row: Mapping[str, object]) -> OperationalCorrelationRecord:
    values = dict(row)
    for key in ("source_kind", "metric_measurement", "metric_coverage", "run_outcome", "feedback_target_kind", "feedback_outcome"):
        if values.get(key) is not None:
            values[key] = str(values[key])
    if values.get("metric_value") is not None:
        values["metric_value"] = Decimal(str(values["metric_value"]))
    return OperationalCorrelationRecord.model_validate(values)


__all__ = [
    "PostgreSQLOperationalCorrelationExecutor",
    "PostgreSQLOperationalCorrelationStore",
]
