"""PostgreSQL ledger for Sheets absence candidates and policy decisions."""

from __future__ import annotations

import json
from collections.abc import AsyncIterable
from typing import Protocol
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from spine.application.google_sheets_lifecycle import (
    GoogleSheetsAbsenceCandidate,
    GoogleSheetsLifecycleRepository,
    GoogleSheetsLifecycleSource,
    GoogleSheetsLifecycleSourceReader,
    GoogleSheetsReconciliationCommand,
    GoogleSheetsRemovalDecision,
    InMemoryGoogleSheetsLifecycleRepository,
)
from spine.application.persistence.context import EnvironmentScope, TrustedPersistenceContext
from spine.application.persistence.unit_of_work import UnitOfWork, UnitOfWorkFactory
from spine.domain.sources import (
    RevisionKind,
    SourceObject,
    SourceRevisionReceiptAssociation,
)
from spine.domain.sources.google import (
    GOOGLE_SHEET_ROW_EXTRACTION_PROFILE,
    GoogleSheetRowOriginal,
    google_source_identity,
)
from spine.infrastructure.persistence.postgresql_mappings import (
    google_sheets_absence_candidates,
    google_sheets_removal_decisions,
)


class PostgreSQLGoogleSheetsLifecycleRepository(GoogleSheetsLifecycleRepository):
    """Store content-free, replayable lifecycle evidence under workspace RLS."""

    async def resolve_decision(
        self,
        uow: UnitOfWork,
        decision_id: UUID,
        *,
        workspace_id: UUID,
        environment_id: UUID,
    ) -> GoogleSheetsRemovalDecision | None:
        result = await uow._execute(
            select(google_sheets_removal_decisions).where(
                google_sheets_removal_decisions.c.decision_id == decision_id,
                google_sheets_removal_decisions.c.workspace_id == workspace_id,
                google_sheets_removal_decisions.c.environment_id == environment_id,
            )
        )
        row = result.mappings().one_or_none()
        return _decision(row) if row is not None else None

    async def list_pending_decisions_for_source(
        self,
        uow: UnitOfWork,
        source_object_id: UUID,
        *,
        workspace_id: UUID,
        environment_id: UUID,
    ) -> tuple[GoogleSheetsRemovalDecision, ...]:
        result = await uow._execute(
            select(google_sheets_removal_decisions).where(
                google_sheets_removal_decisions.c.source_object_id == source_object_id,
                google_sheets_removal_decisions.c.workspace_id == workspace_id,
                google_sheets_removal_decisions.c.environment_id == environment_id,
                google_sheets_removal_decisions.c.state == "pending",
            )
        )
        return tuple(_decision(row) for row in result.mappings().all())

    async def record_candidate(
        self, uow: UnitOfWork, candidate: GoogleSheetsAbsenceCandidate
    ) -> GoogleSheetsAbsenceCandidate:
        await uow._execute(
            insert(google_sheets_absence_candidates)
            .values(**_candidate_columns(candidate))
            .on_conflict_do_nothing()
        )
        result = await uow._execute(
            select(google_sheets_absence_candidates).where(
                google_sheets_absence_candidates.c.candidate_id == candidate.candidate_id,
                google_sheets_absence_candidates.c.workspace_id == candidate.workspace_id,
                google_sheets_absence_candidates.c.environment_id == candidate.environment_id,
            )
        )
        row = result.mappings().one_or_none()
        if row is None:
            raise ValueError("Google Sheets absence candidate was not visible after upsert")
        existing = _candidate(row)
        if existing != candidate:
            raise ValueError("Google Sheets absence candidate conflicts with replay")
        return existing

    async def record_decision(
        self, uow: UnitOfWork, decision: GoogleSheetsRemovalDecision
    ) -> GoogleSheetsRemovalDecision:
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
                insert(google_sheets_removal_decisions)
                .values(**_decision_columns(decision))
                .on_conflict_do_nothing()
            )
            result = await uow._execute(
                select(google_sheets_removal_decisions).where(
                    google_sheets_removal_decisions.c.decision_id == decision.decision_id,
                    google_sheets_removal_decisions.c.workspace_id == decision.workspace_id,
                    google_sheets_removal_decisions.c.environment_id == decision.environment_id,
                )
                .with_for_update()
            )
            row = result.mappings().one_or_none()
        if row is None:
            raise ValueError("Google Sheets lifecycle decision was not visible after upsert")
        existing = _decision(row)
        memory = InMemoryGoogleSheetsLifecycleRepository()
        memory.decisions[existing.decision_id] = existing
        updated = await memory.record_decision(decision)
        if updated != existing:
            await uow._execute(
                update(google_sheets_removal_decisions)
                .where(
                    google_sheets_removal_decisions.c.decision_id == decision.decision_id,
                    google_sheets_removal_decisions.c.workspace_id == decision.workspace_id,
                    google_sheets_removal_decisions.c.environment_id == decision.environment_id,
                )
                .values(state=updated.state.value, record=updated.model_dump(mode="json"))
            )
        return updated


class GoogleSheetsOriginalContentReader(Protocol):
    def read_finalized(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        source_revision_id: UUID,
        receipt: object,
        max_bytes: int,
    ) -> AsyncIterable[bytes]: ...


class PostgreSQLGoogleSheetsLifecycleSourceReader(GoogleSheetsLifecycleSourceReader):
    """Find current row identities through tenant history and authorized originals."""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        original_reader: GoogleSheetsOriginalContentReader,
        *,
        max_original_bytes: int = 16_777_216,
    ) -> None:
        if max_original_bytes <= 0:
            raise ValueError("max_original_bytes must be positive")
        self._uow_factory = uow_factory
        self._original_reader = original_reader
        self._max_original_bytes = max_original_bytes

    async def list_current_sources(
        self,
        command: GoogleSheetsReconciliationCommand,
        context: TrustedPersistenceContext,
    ) -> tuple[GoogleSheetsLifecycleSource, ...]:
        if not isinstance(context.scope, EnvironmentScope) or (
            context.scope.workspace_id, context.scope.environment_id
        ) != (command.workspace_id, command.environment_id):
            raise ValueError("Sheets lifecycle source lookup requires its reconciliation tenant")

        pending: list[
            tuple[SourceObject, UUID, SourceRevisionReceiptAssociation, bool, UUID | None]
        ] = []
        async with self._uow_factory(context) as uow:
            after = None
            while True:
                page = await uow.sources.list_sources(limit=500, after=after)
                for source in page:
                    if (
                        source.identity_mode.value != "connector"
                        or source.connection_id != command.connection_id
                        or source.external_namespace != "google"
                        or source.external_generation != "google-source-key:v1"
                    ):
                        continue
                    head = await uow.canonical.resolve_current(source.source_object_id)
                    if head is None:
                        continue
                    is_tombstoned = head.current_revision_kind is RevisionKind.TOMBSTONE
                    revision_id = head.current_revision_id
                    if is_tombstoned:
                        acceptance = await uow.canonical.resolve_acceptance(head.acceptance_id)
                        if acceptance is None or acceptance.predecessor_revision_id is None:
                            raise ValueError("Sheets tombstone has no content predecessor")
                        revision_id = acceptance.predecessor_revision_id
                    revision = await uow.sources.resolve_revision(revision_id)
                    if (
                        revision is None
                        or revision.kind is not RevisionKind.CONTENT
                        or revision.revision_metadata.representation_profile
                        != GOOGLE_SHEET_ROW_EXTRACTION_PROFILE
                    ):
                        continue
                    associations = await uow.sources.resolve_receipt_associations(
                        revision_id
                    )
                    if len(associations) != 1:
                        raise ValueError("Sheets lifecycle original evidence is ambiguous")
                    pending.append(
                        (
                            source,
                            revision_id,
                            associations[0],
                            is_tombstoned,
                            head.current_revision_id if is_tombstoned else None,
                        )
                    )
                if len(page) < 500:
                    break
                last = page[-1]
                after = (last.created_at, last.source_object_id)
            await uow.rollback()

        results: list[GoogleSheetsLifecycleSource] = []
        seen: set[str] = set()
        for source, revision_id, association, is_tombstoned, tombstone_revision_id in pending:
            chunks = self._original_reader.read_finalized(
                workspace_id=command.workspace_id,
                environment_id=command.environment_id,
                source_revision_id=revision_id,
                receipt=association,
                max_bytes=self._max_original_bytes,
            )
            original = GoogleSheetRowOriginal.model_validate_json(
                await _read_original_bytes(chunks, self._max_original_bytes)
            )
            key = original.key
            if (
                key.connection_id != command.connection_id
                or source.external_object_id
                != google_source_identity(key).external_object_id
            ):
                raise ValueError("Sheets lifecycle original identity does not match its source")
            if source.external_object_id in seen:
                raise ValueError("Sheets lifecycle source identity is ambiguous")
            seen.add(source.external_object_id)
            results.append(
                GoogleSheetsLifecycleSource(
                    key=key,
                    source=source,
                    current_revision_id=revision_id,
                    is_tombstoned=is_tombstoned,
                    tombstone_revision_id=tombstone_revision_id,
                )
            )
        return tuple(results)


def _candidate_columns(candidate: GoogleSheetsAbsenceCandidate) -> dict[str, object]:
    return {
        "candidate_id": candidate.candidate_id,
        "workspace_id": candidate.workspace_id,
        "environment_id": candidate.environment_id,
        "connection_id": candidate.connection_id,
        "sync_run_id": candidate.sync_run_id,
        "scope_identity": candidate.scope_identity,
        "external_object_id": candidate.external_object_id,
        "source_object_id": candidate.source_object_id,
        "expected_current_revision_id": candidate.expected_current_revision_id,
        "record": candidate.model_dump(mode="json"),
        "observed_at": candidate.observed_at,
    }


def _candidate(row: object) -> GoogleSheetsAbsenceCandidate:
    return GoogleSheetsAbsenceCandidate.model_validate_json(
        json.dumps(_record(row))
    )


async def _read_original_bytes(
    chunks: AsyncIterable[bytes], max_bytes: int
) -> bytes:
    output = bytearray()
    async for chunk in chunks:
        if not isinstance(chunk, bytes):
            raise ValueError("Authorized Sheets original returned invalid bytes")
        output.extend(chunk)
        if len(output) > max_bytes:
            raise ValueError("Authorized Sheets original exceeded its read bound")
    return bytes(output)


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


__all__ = [
    "GoogleSheetsOriginalContentReader",
    "PostgreSQLGoogleSheetsLifecycleRepository",
    "PostgreSQLGoogleSheetsLifecycleSourceReader",
]
