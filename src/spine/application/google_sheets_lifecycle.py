"""Fail-closed lifecycle decisions for rows absent from a complete Sheets scan."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import ConfigDict, Field, model_validator

from spine.application.google_sheets_reconciliation import (
    GoogleSheetsReconciliationCommand,
    GoogleSheetsReconciliationResult,
    GoogleSheetsReconciliationScope,
    google_sheets_scope_set_digest,
)
from spine.application.persistence.context import TrustedPersistenceContext
from spine.application.persistence.context import PersistenceOperation
from spine.application.persistence.command_digest import digest_command
from spine.application.persistence.google_sheets_sync import (
    GoogleSheetsSyncRepository,
    GoogleSheetsSyncPage,
    GoogleSheetsSyncRunState,
)
from spine.application.persistence.unit_of_work import UnitOfWork, UnitOfWorkFactory
from spine.application.source_lifecycle import (
    SourceLifecycleCommand,
    SourceLifecycleCoordinator,
    SourceLifecycleKind,
    SourceLifecycleResult,
)
from spine.application.document_lifecycle import (
    ServerDocumentLifecycleComposer,
    TombstoneDocumentCommand,
)
from spine.domain.common import DefinitionModel, VersionIdentifier
from spine.domain.sources import SourceObject
from spine.domain.sources.google import GoogleRegisterRowKey, google_source_identity


class GoogleSheetsRemovalState(str, Enum):
    PENDING = "pending"
    TOMBSTONED = "tombstoned"
    RETAINED = "retained"


class GoogleSheetsAbsenceCandidate(DefinitionModel):
    """Content-free evidence that one current row was absent from a full scan."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_id: UUID
    workspace_id: UUID
    environment_id: UUID
    connection_id: UUID
    sync_run_id: UUID
    scope_identity: str = Field(min_length=1, max_length=1024)
    external_object_id: str = Field(pattern=r"^gsk1:[a-z2-7]{52}$", max_length=64)
    source_object_id: UUID
    expected_current_revision_id: UUID
    observed_at: datetime

    @model_validator(mode="after")
    def validate_candidate(self) -> "GoogleSheetsAbsenceCandidate":
        for name in (
            "candidate_id", "workspace_id", "environment_id", "connection_id",
            "sync_run_id", "source_object_id", "expected_current_revision_id",
        ):
            if getattr(self, name).int == 0:
                raise ValueError(f"{name} must be a non-zero UUID")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("candidate timestamp must be timezone-aware")
        return self


class GoogleSheetsRemovalPolicy(DefinitionModel):
    """Immutable versioned authority for absence-driven row tombstones."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_version_id: UUID
    version: VersionIdentifier
    workspace_id: UUID
    environment_id: UUID
    connection_id: UUID
    resource_scope_reference: UUID
    authorized_scope_identities: frozenset[str] = frozenset()

    @model_validator(mode="after")
    def validate_policy(self) -> "GoogleSheetsRemovalPolicy":
        for name in (
            "policy_version_id", "workspace_id", "environment_id", "connection_id",
            "resource_scope_reference",
        ):
            if getattr(self, name).int == 0:
                raise ValueError(f"{name} must be a non-zero UUID")
        return self


class GoogleSheetsRemovalDecision(DefinitionModel):
    """Versioned policy outcome kept separate from its absence candidate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision_id: UUID
    candidate_id: UUID
    workspace_id: UUID
    environment_id: UUID
    sync_run_id: UUID
    connection_id: UUID
    policy_version_id: UUID
    policy_version: VersionIdentifier
    external_object_id: str = Field(pattern=r"^gsk1:[a-z2-7]{52}$", max_length=64)
    source_object_id: UUID
    expected_current_revision_id: UUID
    policy_authorized: bool
    state: GoogleSheetsRemovalState
    tombstone_revision_id: UUID | None = None
    decided_at: datetime

    @model_validator(mode="after")
    def validate_decision(self) -> "GoogleSheetsRemovalDecision":
        for name in (
            "decision_id", "candidate_id", "workspace_id", "environment_id",
            "sync_run_id", "connection_id", "policy_version_id",
            "source_object_id", "expected_current_revision_id",
        ):
            if getattr(self, name).int == 0:
                raise ValueError(f"{name} must be a non-zero UUID")
        if self.decided_at.tzinfo is None or self.decided_at.utcoffset() is None:
            raise ValueError("decision timestamp must be timezone-aware")
        if (self.state is GoogleSheetsRemovalState.RETAINED) == self.policy_authorized:
            raise ValueError("retained decisions cannot authorize removal")
        if (self.state is GoogleSheetsRemovalState.TOMBSTONED) != (
            self.tombstone_revision_id is not None
        ):
            raise ValueError("tombstoned decisions require their revision")
        if self.tombstone_revision_id is not None and self.tombstone_revision_id.int == 0:
            raise ValueError("tombstone revision must be a non-zero UUID")
        return self


class GoogleSheetsLifecycleRepository(Protocol):
    async def resolve_decision(
        self,
        uow: UnitOfWork,
        decision_id: UUID,
        *,
        workspace_id: UUID,
        environment_id: UUID,
    ) -> GoogleSheetsRemovalDecision | None: ...

    async def list_pending_decisions_for_source(
        self,
        uow: UnitOfWork,
        source_object_id: UUID,
        *,
        workspace_id: UUID,
        environment_id: UUID,
    ) -> tuple[GoogleSheetsRemovalDecision, ...]: ...

    async def record_candidate(
        self, uow: UnitOfWork, candidate: GoogleSheetsAbsenceCandidate
    ) -> GoogleSheetsAbsenceCandidate: ...

    async def record_decision(
        self, uow: UnitOfWork, decision: GoogleSheetsRemovalDecision
    ) -> GoogleSheetsRemovalDecision: ...


class InMemoryGoogleSheetsLifecycleRepository:
    """Deterministic idempotent lifecycle ledger for tests and local composition."""

    def __init__(self) -> None:
        self.candidates: dict[UUID, GoogleSheetsAbsenceCandidate] = {}
        self.decisions: dict[UUID, GoogleSheetsRemovalDecision] = {}
        self._pending_candidates: dict[int, dict[UUID, GoogleSheetsAbsenceCandidate]] = {}
        self._pending_decisions: dict[int, dict[UUID, GoogleSheetsRemovalDecision]] = {}

    async def resolve_decision(
        self,
        uow: UnitOfWork,
        decision_id: UUID,
        *,
        workspace_id: UUID,
        environment_id: UUID,
    ) -> GoogleSheetsRemovalDecision | None:
        decision = self._pending_decisions.get(id(uow), {}).get(decision_id)
        if decision is None:
            decision = self.decisions.get(decision_id)
        if decision is None or (decision.workspace_id, decision.environment_id) != (
            workspace_id, environment_id
        ):
            return None
        return decision

    async def list_pending_decisions_for_source(
        self,
        uow: UnitOfWork,
        source_object_id: UUID,
        *,
        workspace_id: UUID,
        environment_id: UUID,
    ) -> tuple[GoogleSheetsRemovalDecision, ...]:
        pending = self._pending_decisions.get(id(uow), {})
        values = {**self.decisions, **pending}
        return tuple(
            decision
            for decision in values.values()
            if decision.source_object_id == source_object_id
            and decision.workspace_id == workspace_id
            and decision.environment_id == environment_id
            and decision.state is GoogleSheetsRemovalState.PENDING
        )

    async def record_candidate(
        self, uow: UnitOfWork, candidate: GoogleSheetsAbsenceCandidate
    ) -> GoogleSheetsAbsenceCandidate:
        staged = self._pending_candidates.setdefault(id(uow), {})
        existing = staged.get(candidate.candidate_id) or self.candidates.get(candidate.candidate_id)
        if existing is not None and existing != candidate:
            raise ValueError("Google Sheets absence candidate conflicts with replay")
        staged[candidate.candidate_id] = existing or candidate
        return staged[candidate.candidate_id]

    async def record_decision(
        self, uow: UnitOfWork, decision: GoogleSheetsRemovalDecision
    ) -> GoogleSheetsRemovalDecision:
        staged = self._pending_decisions.setdefault(id(uow), {})
        existing = staged.get(decision.decision_id) or self.decisions.get(decision.decision_id)
        if existing is None:
            staged[decision.decision_id] = decision
            return decision
        immutable = (
            "candidate_id", "workspace_id", "environment_id", "sync_run_id",
            "connection_id", "policy_version_id", "policy_version",
            "external_object_id", "source_object_id", "expected_current_revision_id",
            "policy_authorized",
        )
        if any(getattr(existing, field) != getattr(decision, field) for field in immutable):
            raise ValueError("Google Sheets lifecycle decision conflicts with replay")
        if existing.state is GoogleSheetsRemovalState.TOMBSTONED:
            return existing
        if decision.state is GoogleSheetsRemovalState.TOMBSTONED:
            staged[decision.decision_id] = decision
            return decision
        return existing

    async def commit(self, uow: UnitOfWork) -> None:
        self.candidates.update(self._pending_candidates.pop(id(uow), {}))
        self.decisions.update(self._pending_decisions.pop(id(uow), {}))

    async def rollback(self, uow: UnitOfWork) -> None:
        self._pending_candidates.pop(id(uow), None)
        self._pending_decisions.pop(id(uow), None)


class GoogleSheetsLifecycleSource(DefinitionModel):
    """Current source head plus its canonical Google row key."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: GoogleRegisterRowKey
    source: SourceObject
    current_revision_id: UUID
    is_tombstoned: bool
    tombstone_revision_id: UUID | None = None

    @model_validator(mode="after")
    def validate_source_key(self) -> "GoogleSheetsLifecycleSource":
        identity = google_source_identity(self.key)
        if (
            self.current_revision_id.int == 0
            or self.source.connection_id != self.key.connection_id
            or self.source.external_namespace != identity.external_namespace
            or self.source.external_generation != identity.external_generation
            or self.source.external_object_id != identity.external_object_id
        ):
            raise ValueError("Sheets lifecycle source does not match its canonical row key")
        if self.is_tombstoned != (self.tombstone_revision_id is not None):
            raise ValueError("Sheets tombstone identity is inconsistent")
        return self


class GoogleSheetsLifecycleSourceReader(Protocol):
    async def list_current_sources(
        self,
        command: GoogleSheetsReconciliationCommand,
        context: TrustedPersistenceContext,
    ) -> tuple[GoogleSheetsLifecycleSource, ...]: ...


class GoogleSheetsTombstoneComposer(Protocol):
    async def compose(
        self,
        *,
        source: GoogleSheetsLifecycleSource,
        command: GoogleSheetsReconciliationCommand,
        policy: GoogleSheetsRemovalPolicy,
        idempotency_key: str,
        context: TrustedPersistenceContext,
    ) -> SourceLifecycleCommand: ...


@dataclass(frozen=True, slots=True)
class GoogleSheetsDocumentTombstoneComposer:
    """Reuse the canonical document lifecycle command builder for a row tombstone."""

    composer: ServerDocumentLifecycleComposer
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)

    async def compose(
        self,
        *,
        source: GoogleSheetsLifecycleSource,
        command: GoogleSheetsReconciliationCommand,
        policy: GoogleSheetsRemovalPolicy,
        idempotency_key: str,
        context: TrustedPersistenceContext,
    ) -> SourceLifecycleCommand:
        tombstone = TombstoneDocumentCommand(
            workspace_id=command.workspace_id,
            environment_id=command.environment_id,
            source_object_id=source.source.source_object_id,
            expected_current_revision_id=source.current_revision_id,
            idempotency_key=idempotency_key,
            deletion_reason="source_deleted",
        )
        operation_id = uuid5(
            NAMESPACE_URL,
            f"spine:google-sheets-row-removal:v1:{idempotency_key}",
        )
        digest = digest_command(
            operation=PersistenceOperation("google_sheets_row_tombstone"),
            operation_schema_version=1,
            payload={
                **tombstone.model_dump(mode="json"),
                "policy_version_id": str(policy.policy_version_id),
                "policy_version": policy.version,
            },
        )
        accepted_at = self.clock()
        if accepted_at.tzinfo is None or accepted_at.utcoffset() is None:
            raise ValueError("Google Sheets tombstone clock must be timezone-aware")
        return await self.composer.compose_tombstone(
            context=context,
            command=tombstone,
            operation_id=operation_id,
            command_digest=digest,
            accepted_at=accepted_at,
        )


@dataclass(frozen=True, slots=True)
class GoogleSheetsLifecycleResult:
    sync_run_id: UUID
    candidates: tuple[GoogleSheetsAbsenceCandidate, ...]
    decisions: tuple[GoogleSheetsRemovalDecision, ...]
    lifecycle_results: tuple[SourceLifecycleResult, ...]
    retained_reason: str | None = None


@dataclass(frozen=True, slots=True)
class GoogleSheetsLifecycleCoordinator:
    repository: GoogleSheetsLifecycleRepository
    sync_repository: GoogleSheetsSyncRepository
    sources: GoogleSheetsLifecycleSourceReader
    tombstones: GoogleSheetsTombstoneComposer
    source_lifecycle: SourceLifecycleCoordinator
    context_factory: Callable[[UUID], TrustedPersistenceContext]
    lifecycle_uow_factory: UnitOfWorkFactory
    lifecycle_context_factory: Callable[
        [PersistenceOperation, UUID], TrustedPersistenceContext
    ]
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)

    async def reconcile(
        self,
        command: GoogleSheetsReconciliationCommand,
        result: GoogleSheetsReconciliationResult,
        policy: GoogleSheetsRemovalPolicy,
    ) -> GoogleSheetsLifecycleResult:
        run = await self.sync_repository.resolve_run(result.sync_run_id)
        if run is None or run.state is not GoogleSheetsSyncRunState.SUCCEEDED:
            return GoogleSheetsLifecycleResult(
                result.sync_run_id, (), (), (), "reconciliation_incomplete"
            )
        if any(
            getattr(run, name) != getattr(command, name)
            for name in (
                "workspace_id", "environment_id", "connection_id",
                "resource_scope_reference", "service_principal_id",
                "access_policy_version_id", "authorization_generation",
                "configuration_generation",
            )
        ):
            return GoogleSheetsLifecycleResult(
                result.sync_run_id, (), (), (), "reconciliation_scope_mismatch"
            )
        if run.scope_set_digest != google_sheets_scope_set_digest(command):
            return GoogleSheetsLifecycleResult(
                result.sync_run_id, (), (), (), "reconciliation_scope_mismatch"
            )
        if not _policy_matches(command, policy):
            return GoogleSheetsLifecycleResult(
                result.sync_run_id, (), (), (), "policy_scope_mismatch"
            )

        observed_by_scope: dict[str, set[str]] = {}
        pages = await self.sync_repository.list_pages(result.sync_run_id)
        for scope in command.scopes:
            scoped_pages = tuple(page for page in pages if page.scope_identity == scope.identity)
            if not _complete_page_chain(scoped_pages):
                return GoogleSheetsLifecycleResult(
                    result.sync_run_id, (), (), (), "reconciliation_incomplete"
                )
        for page in pages:
            if page.state.value != "completed":
                return GoogleSheetsLifecycleResult(
                    result.sync_run_id, (), (), (), "reconciliation_incomplete"
                )
            observed_by_scope.setdefault(page.scope_identity, set()).update(
                item.identity for item in page.items
            )
        for scope in command.scopes:
            if scope.identity not in observed_by_scope:
                return GoogleSheetsLifecycleResult(
                    result.sync_run_id, (), (), (), "reconciliation_scope_missing"
                )

        source_context = self.context_factory(result.sync_run_id)
        try:
            sources = await self.sources.list_current_sources(command, source_context)
        except Exception:
            return GoogleSheetsLifecycleResult(
                result.sync_run_id, (), (), (), "source_inventory_unavailable"
            )
        if any(
            source.source.workspace_id != command.workspace_id
            or source.source.environment_id != command.environment_id
            or source.key.connection_id != command.connection_id
            for source in sources
        ):
            return GoogleSheetsLifecycleResult(
                result.sync_run_id, (), (), (), "source_scope_mismatch"
            )
        candidates: list[GoogleSheetsAbsenceCandidate] = []
        decisions: list[GoogleSheetsRemovalDecision] = []
        effects: list[SourceLifecycleResult] = []
        for source in sources:
            identity = google_source_identity(source.key).external_object_id
            if source.is_tombstoned:
                recovered, recovered_effects = await self._recover_pending_for_source(
                    source=source,
                    command=command,
                    policy=policy,
                    context=source_context,
                )
                decisions.extend(recovered)
                effects.extend(recovered_effects)
                continue
            scope = _scope_for_key(command.scopes, source.key)
            if scope is None:
                continue
            if identity in observed_by_scope.get(scope.identity, set()):
                continue
            candidate = self._candidate(command, result, scope, source, pages)
            candidates.append(candidate)
            authorized = scope.identity in policy.authorized_scope_identities
            state = (
                GoogleSheetsRemovalState.PENDING
                if authorized
                else GoogleSheetsRemovalState.RETAINED
            )
            decision_id = _decision_id(candidate, policy)
            existing = await self._resolve_decision(
                decision_id, command.workspace_id, command.environment_id
            )
            if existing is not None and existing.state is GoogleSheetsRemovalState.TOMBSTONED:
                decisions.append(existing)
                continue
            pending = GoogleSheetsRemovalDecision(
                decision_id=decision_id,
                candidate_id=candidate.candidate_id,
                workspace_id=command.workspace_id,
                environment_id=command.environment_id,
                sync_run_id=result.sync_run_id,
                connection_id=command.connection_id,
                policy_version_id=policy.policy_version_id,
                policy_version=policy.version,
                external_object_id=identity,
                source_object_id=source.source.source_object_id,
                expected_current_revision_id=source.current_revision_id,
                policy_authorized=authorized,
                state=state,
                decided_at=self._now(),
            )
            if not authorized:
                decisions.append(await self._persist_pending(candidate, pending))
                continue
            await self._persist_pending(candidate, pending)
            idem = f"sheets-row-removal:{decision_id.hex}"
            lifecycle_command = await self.tombstones.compose(
                source=source,
                command=command,
                policy=policy,
                idempotency_key=idem,
                context=source_context,
            )
            if (
                lifecycle_command.kind is not SourceLifecycleKind.DELETION
                or lifecycle_command.source.source_object_id != source.source.source_object_id
                or lifecycle_command.canonical_acceptance.expected_current_revision_id
                != source.current_revision_id
                or lifecycle_command.canonical_acceptance.idempotency_key.value != idem
            ):
                raise ValueError("Sheets tombstone command changed identity or replay key")
            effect = await self.source_lifecycle.apply(lifecycle_command)
            completed = pending.model_copy(
                update={
                    "state": GoogleSheetsRemovalState.TOMBSTONED,
                    "tombstone_revision_id": effect.acceptance.revision.revision_id,
                }
            )
            decisions.append(await self._persist_decision(completed))
            effects.append(effect)
        return GoogleSheetsLifecycleResult(
            result.sync_run_id, tuple(candidates), tuple(decisions), tuple(effects)
        )

    async def _recover_pending_for_source(
        self,
        *,
        source: GoogleSheetsLifecycleSource,
        command: GoogleSheetsReconciliationCommand,
        policy: GoogleSheetsRemovalPolicy,
        context: TrustedPersistenceContext,
    ) -> tuple[tuple[GoogleSheetsRemovalDecision, ...], tuple[SourceLifecycleResult, ...]]:
        pending = await self._list_pending_decisions(
            source.source.source_object_id,
            command.workspace_id,
            command.environment_id,
        )
        decisions: list[GoogleSheetsRemovalDecision] = []
        effects: list[SourceLifecycleResult] = []
        for decision in pending:
            if (
                not decision.policy_authorized
                or decision.connection_id != command.connection_id
                or decision.external_object_id
                != google_source_identity(source.key).external_object_id
                or decision.expected_current_revision_id != source.current_revision_id
            ):
                continue
            prior_policy = policy.model_copy(
                update={
                    "policy_version_id": decision.policy_version_id,
                    "version": decision.policy_version,
                }
            )
            idempotency_key = f"sheets-row-removal:{decision.decision_id.hex}"
            lifecycle_command = await self.tombstones.compose(
                source=source,
                command=command,
                policy=prior_policy,
                idempotency_key=idempotency_key,
                context=context,
            )
            if (
                lifecycle_command.kind is not SourceLifecycleKind.DELETION
                or lifecycle_command.source.source_object_id
                != source.source.source_object_id
                or lifecycle_command.canonical_acceptance.expected_current_revision_id
                != decision.expected_current_revision_id
                or lifecycle_command.revision.revision_id != source.tombstone_revision_id
                or lifecycle_command.canonical_acceptance.idempotency_key.value
                != idempotency_key
            ):
                continue
            effect = await self.source_lifecycle.apply(lifecycle_command)
            completed = decision.model_copy(
                update={
                    "state": GoogleSheetsRemovalState.TOMBSTONED,
                    "tombstone_revision_id": effect.acceptance.revision.revision_id,
                }
            )
            decisions.append(await self._persist_decision(completed))
            effects.append(effect)
        return tuple(decisions), tuple(effects)

    async def _list_pending_decisions(
        self, source_object_id: UUID, workspace_id: UUID, environment_id: UUID
    ) -> tuple[GoogleSheetsRemovalDecision, ...]:
        context = self.lifecycle_context_factory(
            PersistenceOperation("google_sheets_removal_decision_read"), source_object_id
        )
        async with self.lifecycle_uow_factory(context) as uow:
            decisions = await self.repository.list_pending_decisions_for_source(
                uow,
                source_object_id,
                workspace_id=workspace_id,
                environment_id=environment_id,
            )
            await uow.rollback()
            return decisions

    def _candidate(
        self,
        command: GoogleSheetsReconciliationCommand,
        result: GoogleSheetsReconciliationResult,
        scope: GoogleSheetsReconciliationScope,
        source: GoogleSheetsLifecycleSource,
        pages: tuple[GoogleSheetsSyncPage, ...],
    ) -> GoogleSheetsAbsenceCandidate:
        completed_at = max(page.completed_at for page in pages if page.completed_at is not None)
        candidate_id = uuid5(
            NAMESPACE_URL,
            f"spine:google-sheets-absence:v1:{result.sync_run_id}:{scope.identity}:{source.source.source_object_id}",
        )
        candidate = GoogleSheetsAbsenceCandidate(
            candidate_id=candidate_id,
            workspace_id=command.workspace_id,
            environment_id=command.environment_id,
            connection_id=command.connection_id,
            sync_run_id=result.sync_run_id,
            scope_identity=scope.identity,
            external_object_id=google_source_identity(source.key).external_object_id,
            source_object_id=source.source.source_object_id,
            expected_current_revision_id=source.current_revision_id,
            observed_at=completed_at,
        )
        return candidate

    async def _resolve_decision(
        self, decision_id: UUID, workspace_id: UUID, environment_id: UUID
    ) -> GoogleSheetsRemovalDecision | None:
        context = self.lifecycle_context_factory(
            PersistenceOperation("google_sheets_removal_decision_read"), decision_id
        )
        async with self.lifecycle_uow_factory(context) as uow:
            result = await self.repository.resolve_decision(
                uow,
                decision_id,
                workspace_id=workspace_id,
                environment_id=environment_id,
            )
            await uow.rollback()
            return result

    async def _persist_pending(
        self,
        candidate: GoogleSheetsAbsenceCandidate,
        decision: GoogleSheetsRemovalDecision,
    ) -> GoogleSheetsRemovalDecision:
        context = self.lifecycle_context_factory(
            PersistenceOperation("google_sheets_removal_decision"), decision.decision_id
        )
        async with self.lifecycle_uow_factory(context) as uow:
            await self.repository.record_candidate(uow, candidate)
            result = await self.repository.record_decision(uow, decision)
            await uow.commit()
            return result

    async def _persist_decision(
        self, decision: GoogleSheetsRemovalDecision
    ) -> GoogleSheetsRemovalDecision:
        context = self.lifecycle_context_factory(
            PersistenceOperation("google_sheets_removal_decision"), decision.decision_id
        )
        async with self.lifecycle_uow_factory(context) as uow:
            result = await self.repository.record_decision(uow, decision)
            await uow.commit()
            return result

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Google Sheets lifecycle clock must be timezone-aware")
        return value


def _policy_matches(
    command: GoogleSheetsReconciliationCommand, policy: GoogleSheetsRemovalPolicy
) -> bool:
    return (
        command.workspace_id == policy.workspace_id
        and command.environment_id == policy.environment_id
        and command.connection_id == policy.connection_id
        and command.resource_scope_reference == policy.resource_scope_reference
        and policy.authorized_scope_identities <= {scope.identity for scope in command.scopes}
    )


def _scope_for_key(
    scopes: tuple[GoogleSheetsReconciliationScope, ...], key: GoogleRegisterRowKey
) -> GoogleSheetsReconciliationScope | None:
    matches = tuple(
        scope for scope in scopes
        if scope.spreadsheet_id == key.spreadsheet_id and scope.sheet_id == key.sheet_id
    )
    return matches[0] if len(matches) == 1 else None


def _decision_id(
    candidate: GoogleSheetsAbsenceCandidate, policy: GoogleSheetsRemovalPolicy
) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        f"spine:google-sheets-removal:v1:{candidate.candidate_id}:{policy.policy_version_id}",
    )


def _complete_page_chain(pages: tuple[GoogleSheetsSyncPage, ...]) -> bool:
    if not pages or any(
        page.state.value != "completed" or page.completed_at is None for page in pages
    ):
        return False
    by_token = {page.page_token: page for page in pages}
    if len(by_token) != len(pages) or None not in by_token:
        return False
    visited: set[str | None] = set()
    token: str | None = None
    while token is not None or not visited:
        if token in visited or token not in by_token:
            return False
        visited.add(token)
        next_token = by_token[token].next_page_token
        if next_token is None:
            return len(visited) == len(pages)
        token = next_token
    return False


__all__ = [
    "GoogleSheetsAbsenceCandidate",
    "GoogleSheetsLifecycleCoordinator",
    "GoogleSheetsDocumentTombstoneComposer",
    "GoogleSheetsLifecycleResult",
    "GoogleSheetsLifecycleSource",
    "GoogleSheetsLifecycleSourceReader",
    "GoogleSheetsRemovalDecision",
    "GoogleSheetsRemovalPolicy",
    "GoogleSheetsRemovalState",
    "InMemoryGoogleSheetsLifecycleRepository",
]
