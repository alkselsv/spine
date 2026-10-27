"""PostgreSQL ledger for Sheets absence candidates and policy decisions."""

from __future__ import annotations

import json
from collections.abc import Callable
from uuid import UUID

from sqlalchemy import insert, select, update

from spine.application.google_sheets_lifecycle import (
    GoogleSheetsAbsenceCandidate,
    GoogleSheetsLifecycleRepository,
    GoogleSheetsRemovalDecision,
    InMemoryGoogleSheetsLifecycleRepository,
)
from spine.application.persistence.context import (
    PersistenceOperation,
    TrustedPersistenceContext,
)
from spine.infrastructure.persistence.postgresql_mappings import (
    google_sheets_absence_candidates,
    google_sheets_removal_decisions,
)


class PostgreSQLGoogleSheetsLifecycleRepository(GoogleSheetsLifecycleRepository):
    """Store content-free, replayable lifecycle evidence under workspace RLS."""

    def __init__(
        self,
        uow_factory: Callable[[TrustedPersistenceContext], object],
        context_factory: Callable[[PersistenceOperation, UUID], TrustedPersistenceContext],
    ) -> None:
        self._uow_factory = uow_factory
        self._context_factory = context_factory

    async def resolve_decision(
        self, decision_id: UUID, *, workspace_id: UUID, environment_id: UUID
    ) -> GoogleSheetsRemovalDecision | None:
        async with self._uow("google_sheets_removal_decision_read", decision_id) as uow:
            result = await uow._execute(
                select(google_sheets_removal_decisions).where(
                    google_sheets_removal_decisions.c.decision_id == decision_id,
                    google_sheets_removal_decisions.c.workspace_id == workspace_id,
                    google_sheets_removal_decisions.c.environment_id == environment_id,
                )
            )
            row = result.mappings().one_or_none()
            await uow.rollback()
            return _decision(row) if row is not None else None

    async def record_candidate(
        self, candidate: GoogleSheetsAbsenceCandidate
    ) -> GoogleSheetsAbsenceCandidate:
        async with self._uow("google_sheets_absence_candidate", candidate.candidate_id) as uow:
            result = await uow._execute(
                select(google_sheets_absence_candidates).where(
                    google_sheets_absence_candidates.c.candidate_id == candidate.candidate_id,
                    google_sheets_absence_candidates.c.workspace_id == candidate.workspace_id,
                    google_sheets_absence_candidates.c.environment_id == candidate.environment_id,
                ).with_for_update()
            )
            row = result.mappings().one_or_none()
            if row is not None:
                existing = _candidate(row)
                if existing != candidate:
                    raise ValueError("Google Sheets absence candidate conflicts with replay")
                await uow.rollback()
                return existing
            await uow._execute(
                insert(google_sheets_absence_candidates).values(
                    candidate_id=candidate.candidate_id,
                    workspace_id=candidate.workspace_id,
                    environment_id=candidate.environment_id,
                    connection_id=candidate.connection_id,
                    sync_run_id=candidate.sync_run_id,
                    scope_identity=candidate.scope_identity,
                    external_object_id=candidate.external_object_id,
                    source_object_id=candidate.source_object_id,
                    expected_current_revision_id=candidate.expected_current_revision_id,
                    record=candidate.model_dump(mode="json"),
                    observed_at=candidate.observed_at,
                )
            )
            await uow.commit()
            return candidate

    async def record_decision(
        self, decision: GoogleSheetsRemovalDecision
    ) -> GoogleSheetsRemovalDecision:
        async with self._uow("google_sheets_removal_decision", decision.decision_id) as uow:
            result = await uow._execute(
                select(google_sheets_removal_decisions).where(
                    google_sheets_removal_decisions.c.decision_id == decision.decision_id,
                    google_sheets_removal_decisions.c.workspace_id == decision.workspace_id,
                    google_sheets_removal_decisions.c.environment_id == decision.environment_id,
                ).with_for_update()
            )
            row = result.mappings().one_or_none()
            if row is None:
                await uow._execute(
                    insert(google_sheets_removal_decisions).values(**_decision_columns(decision))
                )
                await uow.commit()
                return decision
            existing = _decision(row)
            memory = InMemoryGoogleSheetsLifecycleRepository()
            memory.decisions[existing.decision_id] = existing
            result = await memory.record_decision(decision)
            if result != existing:
                await uow._execute(
                    update(google_sheets_removal_decisions)
                    .where(
                        google_sheets_removal_decisions.c.decision_id == decision.decision_id,
                        google_sheets_removal_decisions.c.workspace_id == decision.workspace_id,
                        google_sheets_removal_decisions.c.environment_id == decision.environment_id,
                    )
                    .values(
                        state=result.state.value,
                        record=result.model_dump(mode="json"),
                    )
                )
                await uow.commit()
                return result
            await uow.rollback()
            return result

    def _uow(self, operation: str, identity: UUID):
        context = self._context_factory(PersistenceOperation(operation), identity)
        return self._uow_factory(context)


def _candidate(row: object) -> GoogleSheetsAbsenceCandidate:
    return GoogleSheetsAbsenceCandidate.model_validate_json(
        json.dumps(_record(row))
    )


def _decision(row: object) -> GoogleSheetsRemovalDecision:
    return GoogleSheetsRemovalDecision.model_validate_json(
        json.dumps(_record(row))
    )


def _record(row: object) -> object:
    try:
        return row["record"]  # type: ignore[index]
    except (KeyError, TypeError):
        return row._mapping["record"]  # type: ignore[attr-defined]


def _decision_columns(decision: GoogleSheetsRemovalDecision) -> dict[str, object]:
    return {
        "decision_id": decision.decision_id,
        "candidate_id": decision.candidate_id,
        "workspace_id": decision.workspace_id,
        "environment_id": decision.environment_id,
        "sync_run_id": decision.sync_run_id,
        "connection_id": decision.connection_id,
        "policy_version_id": decision.policy_version_id,
        "source_object_id": decision.source_object_id,
        "policy_authorized": decision.policy_authorized,
        "state": decision.state.value,
        "record": decision.model_dump(mode="json"),
        "decided_at": decision.decided_at,
    }


__all__ = ["PostgreSQLGoogleSheetsLifecycleRepository"]
