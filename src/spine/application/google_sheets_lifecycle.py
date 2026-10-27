"""Fail-closed lifecycle decisions for rows absent from a complete Sheets scan."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from enum import Enum
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import ConfigDict, Field, model_validator

from spine.application.google_sheets_reconciliation import (
    GoogleSheetsReconciliationCommand,
    GoogleSheetsReconciliationResult,
    GoogleSheetsReconciliationScope,
)
from spine.application.persistence.context import TrustedPersistenceContext
from spine.application.persistence.google_sheets_sync import (
    GoogleSheetsSyncRepository,
    GoogleSheetsSyncPage,
    GoogleSheetsSyncRunState,
)
from spine.application.source_lifecycle import (
    SourceLifecycleCommand,
    SourceLifecycleCoordinator,
    SourceLifecycleKind,
    SourceLifecycleResult,
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
        self, decision_id: UUID, *, workspace_id: UUID, environment_id: UUID
    ) -> GoogleSheetsRemovalDecision | None: ...

    async def record_candidate(
        self, candidate: GoogleSheetsAbsenceCandidate
    ) -> GoogleSheetsAbsenceCandidate: ...

    async def record_decision(
        self, decision: GoogleSheetsRemovalDecision
    ) -> GoogleSheetsRemovalDecision: ...


class InMemoryGoogleSheetsLifecycleRepository:
    """Deterministic idempotent lifecycle ledger for tests and local composition."""

    def __init__(self) -> None:
        self.candidates: dict[UUID, GoogleSheetsAbsenceCandidate] = {}
        self.decisions: dict[UUID, GoogleSheetsRemovalDecision] = {}

    async def resolve_decision(
        self, decision_id: UUID, *, workspace_id: UUID, environment_id: UUID
    ) -> GoogleSheetsRemovalDecision | None:
        decision = self.decisions.get(decision_id)
        if decision is None or (decision.workspace_id, decision.environment_id) != (
            workspace_id, environment_id
        ):
            return None
        return decision

    async def record_candidate(
        self, candidate: GoogleSheetsAbsenceCandidate
    ) -> GoogleSheetsAbsenceCandidate:
        existing = self.candidates.get(candidate.candidate_id)
        if existing is not None and existing != candidate:
            raise ValueError("Google Sheets absence candidate conflicts with replay")
        self.candidates[candidate.candidate_id] = existing or candidate
        return self.candidates[candidate.candidate_id]

    async def record_decision(
        self, decision: GoogleSheetsRemovalDecision
    ) -> GoogleSheetsRemovalDecision:
        existing = self.decisions.get(decision.decision_id)
        if existing is None:
            self.decisions[decision.decision_id] = decision
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
            self.decisions[decision.decision_id] = decision
            return decision
        return existing


class GoogleSheetsLifecycleSource(DefinitionModel):
    """Current source head plus its canonical Google row key."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: GoogleRegisterRowKey
    source: SourceObject
    current_revision_id: UUID
    is_tombstoned: bool

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
    ) -> SourceLifecycleCommand: ...


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
        if run.scope_set_digest != _scope_set_digest(command):
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

        sources = await self.sources.list_current_sources(
            command, self.context_factory(result.sync_run_id)
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
            scope = _scope_for_key(command.scopes, source.key)
            if scope is None or source.is_tombstoned:
                continue
            identity = google_source_identity(source.key).external_object_id
            if identity in observed_by_scope.get(scope.identity, set()):
                continue
            candidate = await self._candidate(command, result, scope, source, pages)
            candidates.append(candidate)
            authorized = scope.identity in policy.authorized_scope_identities
            state = (
                GoogleSheetsRemovalState.PENDING
                if authorized
                else GoogleSheetsRemovalState.RETAINED
            )
            decision_id = _decision_id(candidate, policy)
            existing = await self.repository.resolve_decision(
                decision_id,
                workspace_id=command.workspace_id,
                environment_id=command.environment_id,
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
            await self.repository.record_candidate(candidate)
            if not authorized:
                decisions.append(await self.repository.record_decision(pending))
                continue
            idem = f"sheets-row-removal:{decision_id.hex}"
            lifecycle_command = await self.tombstones.compose(
                source=source,
                command=command,
                policy=policy,
                idempotency_key=idem,
            )
            if (
                lifecycle_command.kind is not SourceLifecycleKind.DELETION
                or lifecycle_command.source.source_object_id != source.source.source_object_id
                or lifecycle_command.canonical_acceptance.expected_current_revision_id
                != source.current_revision_id
                or lifecycle_command.canonical_acceptance.idempotency_key.value != idem
            ):
                raise ValueError("Sheets tombstone command changed identity or replay key")
            await self.repository.record_decision(pending)
            effect = await self.source_lifecycle.apply(lifecycle_command)
            completed = pending.model_copy(
                update={
                    "state": GoogleSheetsRemovalState.TOMBSTONED,
                    "tombstone_revision_id": effect.acceptance.revision.revision_id,
                }
            )
            decisions.append(await self.repository.record_decision(completed))
            effects.append(effect)
        return GoogleSheetsLifecycleResult(
            result.sync_run_id, tuple(candidates), tuple(decisions), tuple(effects)
        )

    async def _candidate(
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
        return await self.repository.record_candidate(candidate)

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


def _scope_set_digest(command: GoogleSheetsReconciliationCommand) -> str:
    scopes = sorted(
        (scope.model_dump(mode="json") for scope in command.scopes),
        key=lambda scope: (scope["spreadsheet_id"], scope["sheet_id"], scope["a1_range"]),
    )
    payload = json.dumps(scopes, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


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
    "GoogleSheetsLifecycleResult",
    "GoogleSheetsLifecycleSource",
    "GoogleSheetsLifecycleSourceReader",
    "GoogleSheetsRemovalDecision",
    "GoogleSheetsRemovalPolicy",
    "GoogleSheetsRemovalState",
    "InMemoryGoogleSheetsLifecycleRepository",
]
