"""Production operational-correlation projection over canonical PostgreSQL facts."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select, text

from spine.application.operational_correlation import (
    InMemoryOperationalCorrelationStore,
    OperationalCorrelationAccessDenied,
    OperationalCorrelationProjector,
    OperationalCorrelationReader,
)
from spine.application.persistence.context import EnvironmentScope, TrustedPersistenceContext
from spine.application.persistence.errors import AnswerQuestionRunNotFoundError
from spine.application.persistence.unit_of_work import UnitOfWorkFactory
from spine.auth import AuthorizationRole, AuthorizedRequestContext
from spine.domain.feedback import FeedbackRecord
from spine.domain.metrics import MetricCoverageState, OperationalMeasurement
from spine.domain.operational_correlation import (
    CorrelationReferenceKind,
    CorrelationSourceKind,
    OperationalCorrelationQuery,
    OperationalCorrelationRecord,
    OperationalCorrelationView,
)
from spine.infrastructure.persistence.postgresql_mappings import (
    feedback_records,
)


class PostgreSQLOperationalCorrelationReader:
    """Build a bounded safe view from the exact canonical records for one run."""

    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def read(
        self,
        *,
        query: OperationalCorrelationQuery,
        authority: AuthorizedRequestContext[object],
        context: TrustedPersistenceContext,
    ) -> OperationalCorrelationView:
        _authorize(query=query, authority=authority, context=context)
        store = InMemoryOperationalCorrelationStore()
        projector = OperationalCorrelationProjector(store)
        if query.reference_kind is not CorrelationReferenceKind.RUN:
            return await OperationalCorrelationReader(store=store).read(
                query=query,
                authority=authority,
                context=context,
            )

        async with self._uow_factory(context) as uow:
            try:
                run = await uow.answer_question_runs.resolve_run(query.reference_id)
            except AnswerQuestionRunNotFoundError:
                await uow.rollback()
                return await OperationalCorrelationReader(store=store).read(
                    query=query,
                    authority=authority,
                    context=context,
                )
            attempts = await uow.answer_question_runs.list_attempts(run.run_id)
            feedback_rows = (
                await uow._execute(  # type: ignore[attr-defined]
                    select(feedback_records)
                    .where(
                        feedback_records.c.workspace_id == query.workspace_id,
                        feedback_records.c.environment_id == query.environment_id,
                        feedback_records.c.run_id == run.run_id,
                    )
                    .order_by(
                        feedback_records.c.created_at,
                        feedback_records.c.feedback_id,
                        feedback_records.c.version,
                    )
                )
            ).mappings().all()
            delivery_rows = (
                await uow._execute(  # type: ignore[attr-defined]
                    text(
                        "SELECT * FROM spine.resolve_run_outbox_delivery_correlation("
                        ":workspace_id, :environment_id, :run_id)"
                    ),
                    {
                        "workspace_id": query.workspace_id,
                        "environment_id": query.environment_id,
                        "run_id": run.run_id,
                    },
                )
            ).mappings().all()

            await projector.observe_run(run)
            for row in feedback_rows:
                await projector.observe_feedback(FeedbackRecord.model_validate(dict(row)))
            if attempts:
                await store.append(_attempt_metric(run=run, attempt=attempts[-1]))
            for row in delivery_rows:
                await store.append(_delivery_record(run_id=run.run_id, row=row))
            await uow.commit()

        return await OperationalCorrelationReader(store=store).read(
            query=query,
            authority=authority,
            context=context,
        )


def _authorize(
    *,
    query: OperationalCorrelationQuery,
    authority: AuthorizedRequestContext[object],
    context: TrustedPersistenceContext,
) -> None:
    scope = context.scope
    if (
        authority.roles != frozenset({AuthorizationRole.ADMINISTRATOR})
        or authority.scope.workspace_id != query.workspace_id
        or authority.scope.environment_id != query.environment_id
        or not isinstance(scope, EnvironmentScope)
        or scope.workspace_id != query.workspace_id
        or scope.environment_id != query.environment_id
        or context.acting_subject_id != authority.acting_subject_id
    ):
        raise OperationalCorrelationAccessDenied(
            "Operational correlation is unavailable."
        )


def _attempt_metric(*, run, attempt) -> OperationalCorrelationRecord:
    completed_at = attempt.completed_at or attempt.started_at
    duration_ms = max(
        0,
        int((completed_at - attempt.started_at).total_seconds() * 1_000),
    )
    event = attempt.events[-1]
    return OperationalCorrelationRecord(
        workspace_id=run.workspace_id,
        environment_id=run.environment_id,
        source_kind=CorrelationSourceKind.METRIC,
        source_identity="canonical.answer-question-attempt:v1",
        source_schema_version="operational-metric-event:v1",
        source_record_id=event.event_id,
        deduplication_key=f"attempt-metric:{attempt.attempt_id}",
        observed_at=completed_at,
        trace_id=attempt.diagnostic_context.trace_id,
        correlation_id=attempt.diagnostic_context.correlation_id,
        run_id=run.run_id,
        attempt_id=attempt.attempt_id,
        metric_measurement=OperationalMeasurement.QA_LATENCY,
        metric_coverage=MetricCoverageState.COMPLETE,
        metric_value=Decimal(duration_ms),
    )


def _delivery_record(*, run_id, row) -> OperationalCorrelationRecord:
    status = row["status"]
    return OperationalCorrelationRecord(
        workspace_id=row["workspace_id"],
        environment_id=row["environment_id"],
        source_kind=CorrelationSourceKind.DELIVERY,
        source_identity="canonical.outbox-delivery:v1",
        source_schema_version="outbox-delivery:v1",
        source_record_id=row["event_id"],
        deduplication_key=f"delivery:{row['event_id']}:{row['version']}",
        observed_at=row["updated_at"],
        trace_id=row["trace_id"],
        correlation_id=row["correlation_id"],
        run_id=run_id,
        delivery_event_id=row["event_id"],
        delivery_status=status,
        delivery_attempt_count=row["attempt_count"],
        delivery_created_at=row["created_at"],
        delivery_updated_at=row["updated_at"],
        delivery_quarantined_at=row["quarantined_at"],
        delivery_last_error_code=row["last_error_code"],
    )


__all__ = ["PostgreSQLOperationalCorrelationReader"]
