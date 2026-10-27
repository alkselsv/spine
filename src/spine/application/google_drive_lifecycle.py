"""Fail-closed Drive removal and reappearance lifecycle decisions.

Provider observations are evidence, not deletion commands.  A tombstone is
created only after a complete, authorized reconciliation proves absence and a
versioned policy for the exact connection and scope authorizes that signal.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import ConfigDict, Field, model_validator

from spine.application.persistence.context import TrustedPersistenceContext
from spine.application.source_lifecycle import (
    SourceLifecycleCommand,
    SourceLifecycleCoordinator,
    SourceLifecycleKind,
    SourceLifecycleResult,
)
from spine.domain.common import DefinitionModel, VersionIdentifier
from spine.domain.sources.google import GoogleDriveLifecycleSignal
from spine.domain.sources import SourceObject, SourceRevision
from spine.domain.sources import RevisionKind
from spine.domain.sources.google import GoogleDriveFileKey, google_source_identity


DriveLifecycleSignal = GoogleDriveLifecycleSignal


class DriveReconciliationState(str, Enum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    AUTHORIZATION_REVOKED = "authorization_revoked"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class DriveLifecycleDecisionState(str, Enum):
    PENDING = "pending"
    TOMBSTONED = "tombstoned"
    RETAINED = "retained"


class DriveReappearanceEvidence(DefinitionModel):
    """Fresh authorized content proving continuity for one Drive source identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_id: UUID
    environment_id: UUID
    connection_id: UUID
    resource_scope_reference: UUID
    service_principal_id: UUID
    authorization_generation: int = Field(gt=0)
    configuration_generation: int = Field(gt=0)
    access_policy_version_id: UUID
    file_id: str = Field(min_length=1, max_length=256)
    source: SourceObject
    revision: SourceRevision
    tombstone_predecessor_revision_id: UUID
    identity_continuity_proven: bool
    observed_at: datetime

    @model_validator(mode="after")
    def validate_reappearance(self) -> "DriveReappearanceEvidence":
        if not self.identity_continuity_proven:
            raise ValueError("Drive reappearance requires proven identity continuity")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("Drive reappearance timestamp must be timezone-aware")
        expected = google_source_identity(
            GoogleDriveFileKey(connection_id=self.connection_id, file_id=self.file_id)
        )
        if (
            self.source.connection_id != self.connection_id
            or self.source.workspace_id != self.workspace_id
            or self.source.environment_id != self.environment_id
            or self.source.external_namespace != expected.external_namespace
            or self.source.external_generation != expected.external_generation
            or self.source.external_object_id != expected.external_object_id
            or self.revision.source_object_id != self.source.source_object_id
            or self.revision.workspace_id != self.workspace_id
            or self.revision.environment_id != self.environment_id
            or self.revision.kind is not RevisionKind.CONTENT
            or self.revision.reappearance_after_tombstone_revision_id
            != self.tombstone_predecessor_revision_id
            or self.access_policy_version_id.int == 0
        ):
            raise ValueError("Drive reappearance does not preserve canonical identity")
        return self


class DriveRemovalPolicy(DefinitionModel):
    """Immutable, explicitly versioned deletion policy scoped to one Drive scope."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_version_id: UUID
    version: VersionIdentifier
    workspace_id: UUID
    environment_id: UUID
    connection_id: UUID
    resource_scope_reference: UUID
    authorized_signals: frozenset[DriveLifecycleSignal] = frozenset()

    @model_validator(mode="after")
    def validate_scope(self) -> "DriveRemovalPolicy":
        for name in (
            "policy_version_id", "workspace_id", "environment_id", "connection_id",
            "resource_scope_reference",
        ):
            if getattr(self, name).int == 0:
                raise ValueError(f"{name} must be a non-zero UUID")
        if any(
            signal in {
                DriveLifecycleSignal.HTTP_401,
                DriveLifecycleSignal.HTTP_403,
                DriveLifecycleSignal.HTTP_404,
                DriveLifecycleSignal.PRESENT,
                DriveLifecycleSignal.AMBIGUOUS,
            }
            for signal in self.authorized_signals
        ):
            raise ValueError("access failures and ambiguous signals cannot authorize deletion")
        return self


class DriveReconciliationEvidence(DefinitionModel):
    """Durable full-scope scan outcome; ``file_ids`` contains observed identities."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reconciliation_id: UUID
    snapshot_run_id: UUID
    change_sync_run_id: UUID
    committed_cursor: str = Field(min_length=1, max_length=4096)
    workspace_id: UUID
    environment_id: UUID
    connection_id: UUID
    resource_scope_reference: UUID
    service_principal_id: UUID
    access_policy_version_id: UUID
    authorization_generation: int = Field(gt=0)
    configuration_generation: int = Field(gt=0)
    state: DriveReconciliationState
    file_ids: frozenset[str] = frozenset()
    signals: tuple[tuple[str, DriveLifecycleSignal], ...] = ()
    started_at: datetime
    completed_at: datetime | None = None

    @model_validator(mode="after")
    def validate_evidence(self) -> "DriveReconciliationEvidence":
        for name in (
            "reconciliation_id", "snapshot_run_id", "change_sync_run_id",
            "workspace_id", "environment_id", "connection_id",
            "resource_scope_reference", "service_principal_id", "access_policy_version_id",
        ):
            if getattr(self, name).int == 0:
                raise ValueError(f"{name} must be a non-zero UUID")
        if self.started_at.tzinfo is None or self.started_at.utcoffset() is None:
            raise ValueError("reconciliation timestamps must be timezone-aware")
        if self.completed_at is not None and (
            self.completed_at.tzinfo is None or self.completed_at.utcoffset() is None
        ):
            raise ValueError("reconciliation timestamps must be timezone-aware")
        if self.completed_at is not None and self.completed_at < self.started_at:
            raise ValueError("reconciliation completion cannot precede its start")
        if self.state is DriveReconciliationState.COMPLETE and self.completed_at is None:
            raise ValueError("complete reconciliation requires a completion timestamp")
        if len({file_id for file_id, _ in self.signals}) != len(self.signals):
            raise ValueError("reconciliation may carry one final signal per file")
        if any(not file_id or len(file_id) > 256 for file_id in self.file_ids):
            raise ValueError("reconciliation file identities must be bounded")
        return self


class DriveLifecycleSource(DefinitionModel):
    """Current canonical identity supplied by the tenant-scoped source query."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    file_id: str = Field(min_length=1, max_length=256)
    source_object_id: UUID
    current_revision_id: UUID
    is_tombstoned: bool


class DriveRemovalDecision(DefinitionModel):
    """Content-free, replayable evidence for one policy decision and lifecycle effect."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision_id: UUID
    workspace_id: UUID
    environment_id: UUID
    reconciliation_id: UUID
    snapshot_run_id: UUID
    change_sync_run_id: UUID
    committed_cursor: str = Field(min_length=1, max_length=4096)
    service_principal_id: UUID
    access_policy_version_id: UUID
    authorization_generation: int = Field(gt=0)
    configuration_generation: int = Field(gt=0)
    connection_id: UUID
    resource_scope_reference: UUID
    policy_version_id: UUID
    policy_version: VersionIdentifier
    file_id: str = Field(min_length=1, max_length=256)
    signal: DriveLifecycleSignal
    source_object_id: UUID
    expected_current_revision_id: UUID
    policy_authorized: bool
    state: DriveLifecycleDecisionState
    tombstone_revision_id: UUID | None = None
    decided_at: datetime


class DriveLifecycleDecisionRepository(Protocol):
    """Tenant-scoped idempotent store for lifecycle decision evidence."""

    async def resolve(
        self, decision_id: UUID, *, workspace_id: UUID, environment_id: UUID
    ) -> DriveRemovalDecision | None: ...

    async def record(self, decision: DriveRemovalDecision) -> DriveRemovalDecision: ...


class InMemoryDriveLifecycleDecisionRepository:
    """Deterministic decision ledger for application and adapter contract tests."""

    def __init__(self) -> None:
        self.records: dict[UUID, DriveRemovalDecision] = {}

    async def resolve(
        self, decision_id: UUID, *, workspace_id: UUID, environment_id: UUID
    ) -> DriveRemovalDecision | None:
        decision = self.records.get(decision_id)
        if decision is None or (decision.workspace_id, decision.environment_id) != (
            workspace_id, environment_id
        ):
            return None
        return decision

    async def record(self, decision: DriveRemovalDecision) -> DriveRemovalDecision:
        existing = self.records.get(decision.decision_id)
        if existing is None:
            self.records[decision.decision_id] = decision
            return decision
        identity_fields = (
            "workspace_id", "environment_id", "reconciliation_id", "snapshot_run_id",
            "change_sync_run_id", "committed_cursor", "service_principal_id",
            "access_policy_version_id", "authorization_generation",
            "configuration_generation", "connection_id", "resource_scope_reference",
            "policy_version_id", "policy_version", "file_id", "signal",
            "source_object_id", "expected_current_revision_id", "policy_authorized",
        )
        if any(getattr(existing, field) != getattr(decision, field) for field in identity_fields):
            raise ValueError("Drive lifecycle decision identity conflicts with its replay")
        if existing.state is DriveLifecycleDecisionState.TOMBSTONED:
            return existing
        if decision.state is DriveLifecycleDecisionState.TOMBSTONED:
            self.records[decision.decision_id] = decision
            return decision
        return existing


class DriveLifecycleSourceReader(Protocol):
    async def list_current_sources(
        self, evidence: DriveReconciliationEvidence, context: TrustedPersistenceContext
    ) -> tuple[DriveLifecycleSource, ...]: ...


class DriveTombstoneComposer(Protocol):
    async def compose(
        self,
        *,
        source: DriveLifecycleSource,
        evidence: DriveReconciliationEvidence,
        policy: DriveRemovalPolicy,
        signal: DriveLifecycleSignal,
        idempotency_key: str,
    ) -> SourceLifecycleCommand: ...


@dataclass(frozen=True, slots=True)
class DriveLifecycleResult:
    reconciliation_id: UUID
    decisions: tuple[DriveRemovalDecision, ...]
    lifecycle_results: tuple[SourceLifecycleResult, ...]
    retained_reason: str | None = None


@dataclass(frozen=True, slots=True)
class GoogleDriveLifecycleCoordinator:
    decisions: DriveLifecycleDecisionRepository
    sources: DriveLifecycleSourceReader
    tombstones: DriveTombstoneComposer
    source_lifecycle: SourceLifecycleCoordinator
    context_factory: Callable[[UUID], TrustedPersistenceContext]
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)

    async def reconcile(
        self,
        evidence: DriveReconciliationEvidence,
        policy: DriveRemovalPolicy,
    ) -> DriveLifecycleResult:
        mismatch = _scope_mismatch(evidence, policy)
        if mismatch is not None:
            return DriveLifecycleResult(evidence.reconciliation_id, (), (), mismatch)
        if evidence.state is not DriveReconciliationState.COMPLETE:
            return DriveLifecycleResult(
                evidence.reconciliation_id, (), (), evidence.state.value
            )

        context = self.context_factory(evidence.reconciliation_id)
        sources = await self.sources.list_current_sources(evidence, context)
        observed = evidence.file_ids
        signals = dict(evidence.signals)
        decisions: list[DriveRemovalDecision] = []
        effects: list[SourceLifecycleResult] = []

        for source in sources:
            signal = signals.get(source.file_id)
            if source.file_id in observed:
                if signal not in {
                    DriveLifecycleSignal.TRASHED,
                    DriveLifecycleSignal.MOVED_OUT_OF_SCOPE,
                }:
                    continue
            elif signal in {
                DriveLifecycleSignal.HTTP_401,
                DriveLifecycleSignal.HTTP_403,
                DriveLifecycleSignal.HTTP_404,
                DriveLifecycleSignal.AMBIGUOUS,
                DriveLifecycleSignal.PRESENT,
            }:
                # A per-file access error is never evidence of absence.
                continue
            elif signal not in {
                DriveLifecycleSignal.REMOVED,
                DriveLifecycleSignal.TRASHED,
                DriveLifecycleSignal.MOVED_OUT_OF_SCOPE,
            }:
                signal = DriveLifecycleSignal.CONFIRMED_ABSENT

            if source.is_tombstoned:
                continue
            if signal not in policy.authorized_signals:
                decisions.append(
                    await self._decision(
                        evidence, policy, source, signal,
                        DriveLifecycleDecisionState.RETAINED, None,
                    )
                )
                continue

            decision_id = _decision_id(evidence, policy, source.file_id)
            existing = await self.decisions.resolve(
                decision_id,
                workspace_id=evidence.workspace_id,
                environment_id=evidence.environment_id,
            )
            if existing is not None and existing.state is DriveLifecycleDecisionState.TOMBSTONED:
                decisions.append(existing)
                continue
            idem = f"drive-removal:{decision_id.hex}"
            command = await self.tombstones.compose(
                source=source,
                evidence=evidence,
                policy=policy,
                signal=signal,
                idempotency_key=idem,
            )
            if command.kind is not SourceLifecycleKind.DELETION:
                raise ValueError("Drive removal composer must produce a deletion lifecycle command")
            pending = await self._decision(
                evidence, policy, source, signal,
                DriveLifecycleDecisionState.PENDING, command.revision.revision_id,
            )
            await self.decisions.record(pending)
            if (
                command.observation_idempotency_key.value != idem
                and not command.observation_idempotency_key.value.startswith(f"{idem}:")
                or command.canonical_acceptance.idempotency_key.value != idem
                or command.source.source_object_id != source.source_object_id
                or command.canonical_acceptance.expected_current_revision_id
                != source.current_revision_id
            ):
                raise ValueError("Drive tombstone command does not preserve scoped identity and replay key")
            result = await self.source_lifecycle.apply(command)
            applied = pending.model_copy(update={
                "state": DriveLifecycleDecisionState.TOMBSTONED,
                "tombstone_revision_id": result.acceptance.revision.revision_id,
            })
            decisions.append(await self.decisions.record(applied))
            effects.append(result)

        return DriveLifecycleResult(evidence.reconciliation_id, tuple(decisions), tuple(effects))

    async def reappear(
        self,
        evidence: DriveReappearanceEvidence,
        command: SourceLifecycleCommand,
    ) -> SourceLifecycleResult:
        """Accept fresh content through SourceLifecycle with tombstone lineage."""

        if (
            command.kind is not SourceLifecycleKind.REAPPEARANCE
            or command.source != evidence.source
            or command.revision != evidence.revision
            or command.revision.reappearance_after_tombstone_revision_id
            != evidence.tombstone_predecessor_revision_id
            or command.canonical_acceptance.access_policy_version_id
            != evidence.access_policy_version_id
            or command.canonical_acceptance.admission_decision_id is None
            or command.canonical_acceptance.authorization_generation
            != evidence.authorization_generation
            or command.canonical_acceptance.configuration_generation
            != evidence.configuration_generation
            or command.canonical_acceptance.connection_id != evidence.connection_id
            or command.canonical_acceptance.resource_scope_reference
            != evidence.resource_scope_reference
            or command.canonical_acceptance.resource_object_reference != evidence.file_id
        ):
            raise ValueError("Drive reappearance command lacks current identity and admission evidence")
        return await self.source_lifecycle.apply(command)

    async def _decision(
        self,
        evidence: DriveReconciliationEvidence,
        policy: DriveRemovalPolicy,
        source: DriveLifecycleSource,
        signal: DriveLifecycleSignal,
        state: DriveLifecycleDecisionState,
        tombstone_revision_id: UUID | None,
    ) -> DriveRemovalDecision:
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Drive lifecycle clock must be timezone-aware")
        decision = DriveRemovalDecision(
            decision_id=_decision_id(evidence, policy, source.file_id),
            workspace_id=evidence.workspace_id,
            environment_id=evidence.environment_id,
            reconciliation_id=evidence.reconciliation_id,
            snapshot_run_id=evidence.snapshot_run_id,
            change_sync_run_id=evidence.change_sync_run_id,
            committed_cursor=evidence.committed_cursor,
            service_principal_id=evidence.service_principal_id,
            access_policy_version_id=evidence.access_policy_version_id,
            authorization_generation=evidence.authorization_generation,
            configuration_generation=evidence.configuration_generation,
            connection_id=evidence.connection_id,
            resource_scope_reference=evidence.resource_scope_reference,
            policy_version_id=policy.policy_version_id,
            policy_version=policy.version,
            file_id=source.file_id,
            signal=signal,
            source_object_id=source.source_object_id,
            expected_current_revision_id=source.current_revision_id,
            policy_authorized=state is not DriveLifecycleDecisionState.RETAINED,
            state=state,
            tombstone_revision_id=tombstone_revision_id,
            decided_at=now,
        )
        return await self.decisions.record(decision)


def _scope_mismatch(
    evidence: DriveReconciliationEvidence, policy: DriveRemovalPolicy
) -> str | None:
    if (
        evidence.workspace_id != policy.workspace_id
        or evidence.environment_id != policy.environment_id
        or evidence.connection_id != policy.connection_id
        or evidence.resource_scope_reference != policy.resource_scope_reference
    ):
        return "policy_scope_mismatch"
    return None


def _decision_id(
    evidence: DriveReconciliationEvidence, policy: DriveRemovalPolicy, file_id: str
) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        "spine:google-drive-removal:v1:"
        f"{evidence.workspace_id}:{evidence.environment_id}:{evidence.reconciliation_id}:"
        f"{policy.policy_version_id}:{file_id}",
    )


__all__ = [
    "DriveLifecycleDecision",
    "DriveLifecycleDecisionRepository",
    "DriveLifecycleDecisionState",
    "DriveLifecycleResult",
    "DriveLifecycleSignal",
    "DriveLifecycleSource",
    "DriveLifecycleSourceReader",
    "InMemoryDriveLifecycleDecisionRepository",
    "DriveReconciliationEvidence",
    "DriveReconciliationState",
    "DriveRemovalDecision",
    "DriveRemovalPolicy",
    "DriveReappearanceEvidence",
    "DriveTombstoneComposer",
    "GoogleDriveLifecycleCoordinator",
]
