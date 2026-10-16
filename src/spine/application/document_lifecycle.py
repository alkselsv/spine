"""Server-owned document replacement and logical tombstone commands."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field

from spine.application.persistence.command_digest import CommandDigest, digest_command
from spine.application.persistence.canonical import CanonicalAcceptanceCommand
from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    TrustedPersistenceContext,
)
from spine.application.persistence.unit_of_work import UnitOfWorkFactory
from spine.application.persistence.idempotency import IdempotencyKey, idempotency_conflict
from spine.application.source_lifecycle import (
    SourceLifecycleCommand,
    SourceLifecycleCoordinator,
    SourceLifecycleKind,
)
from spine.domain.ingestion import IngestionRunState
from spine.domain.sources import (
    RevisionKind,
    SourceObject,
    SourceRevision,
    SourceRevisionProvenance,
    SourceRevisionReceiptAssociation,
    tombstone_revision,
)
from spine.domain.sources.models import ALLOWED_TOMBSTONE_REASONS


logger = logging.getLogger(__name__)
DOCUMENT_LIFECYCLE_SERVICE = "document-lifecycle"


class LifecycleOperationKind(str, Enum):
    REPLACEMENT = "replacement"
    TOMBSTONE = "tombstone"


class LifecycleOperationStatus(str, Enum):
    ACCEPTED_PROCESSING_PENDING = "accepted_processing_pending"
    PROCESSING_DISPATCHED = "processing_dispatched"
    COMPLETED = "completed"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    CONFLICT = "conflict"
    FAILED = "failed"


class LifecycleFailureCode(str, Enum):
    STORAGE_UNAVAILABLE = "storage_unavailable"
    CANONICAL_CONFLICT = "canonical_conflict"
    PROCESSING_UNAVAILABLE = "processing_unavailable"
    AUTHORITY_UNAVAILABLE = "authority_unavailable"


class ExistingUploadIntent(BaseModel):
    """Bounded browser intent; never authoritative storage evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    upload_identity: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_.:-]{0,63}$",
    )
    declared_media_type: str = Field(min_length=1, max_length=255)
    declared_filename: str = Field(min_length=1, max_length=255)
    storage_request_reference: str = Field(
        min_length=1,
        max_length=255,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,254}$",
    )


class _DocumentLifecycleCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_id: UUID
    environment_id: UUID
    source_object_id: UUID
    expected_current_revision_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=255)


class ReplaceDocumentCommand(_DocumentLifecycleCommand):
    upload_intent: ExistingUploadIntent


class TombstoneDocumentCommand(_DocumentLifecycleCommand):
    deletion_reason: str = Field(pattern="^(" + "|".join(sorted(ALLOWED_TOMBSTONE_REASONS)) + ")$")


class SourceLifecycleOperationReceipt(BaseModel):
    """Durable, content-free result and polling contract."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_id: UUID
    operation_kind: LifecycleOperationKind
    source_object_id: UUID
    expected_current_revision_id: UUID
    accepted_revision_id: UUID | None = None
    canonical_acceptance_id: UUID | None = None
    processing_run_id: UUID | None = None
    status: LifecycleOperationStatus
    idempotency_receipt_id: UUID
    accepted_at: datetime | None = None
    completed_at: datetime | None = None
    safe_failure_code: LifecycleFailureCode | None = None
    retryable: bool
    replay: bool = False


class ReplacementProcessingIntent(BaseModel):
    """Internal process-only handoff created after canonical acceptance."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    lifecycle_operation_id: UUID
    canonical_acceptance_id: UUID
    source_object_id: UUID
    source_revision_id: UUID
    trigger_kind: str = Field(default="replacement", pattern="^replacement$")
    processing_profile_reference: str = Field(
        default="document-processing:r1",
        min_length=1,
        max_length=128,
        pattern=r"^[a-z][a-z0-9_.:-]{0,127}$",
    )
    idempotency_key: str = Field(min_length=1, max_length=255)


class LifecycleCommandComposer(Protocol):
    async def compose_replacement(
        self,
        *,
        context: TrustedPersistenceContext,
        command: ReplaceDocumentCommand,
        operation_id: UUID,
        command_digest: CommandDigest,
        accepted_at: datetime,
    ) -> SourceLifecycleCommand: ...

    async def compose_tombstone(
        self,
        *,
        context: TrustedPersistenceContext,
        command: TombstoneDocumentCommand,
        operation_id: UUID,
        command_digest: CommandDigest,
        accepted_at: datetime,
    ) -> SourceLifecycleCommand: ...


class ReplacementProcessingService(Protocol):
    async def replacement(
        self,
        *,
        context: TrustedPersistenceContext,
        intent: ReplacementProcessingIntent,
    ) -> object: ...


class ReplacementLifecycleEvidence(BaseModel):
    """Server-produced replacement evidence from storage and admission authority."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: SourceObject
    revision: SourceRevision
    provenance: SourceRevisionProvenance
    receipt_association: SourceRevisionReceiptAssociation
    admission_decision_id: UUID
    access_policy_version_id: UUID


class ReplacementLifecycleEvidenceProvider(Protocol):
    async def prepare(
        self,
        *,
        context: TrustedPersistenceContext,
        command: ReplaceDocumentCommand,
        operation_id: UUID,
        command_digest: CommandDigest,
        observed_at: datetime,
    ) -> ReplacementLifecycleEvidence: ...


class LifecycleCanonicalCommandFactory(Protocol):
    def __call__(
        self,
        *,
        context: TrustedPersistenceContext,
        source: SourceObject,
        revision: SourceRevision,
        provenance: SourceRevisionProvenance,
        expected_current_revision_id: UUID,
        idempotency_key: IdempotencyKey,
        canonical_acceptance_id: UUID,
        admission_decision_id: UUID | None,
        access_policy_version_id: UUID | None,
        accepted_at: datetime,
    ) -> CanonicalAcceptanceCommand: ...


@dataclass(frozen=True, slots=True)
class ServerDocumentLifecycleComposer:
    """Build lifecycle commands only from trusted server-side evidence."""

    uow_factory: UnitOfWorkFactory
    replacement_evidence: ReplacementLifecycleEvidenceProvider
    canonical_command_factory: LifecycleCanonicalCommandFactory
    service_name: str = DOCUMENT_LIFECYCLE_SERVICE

    async def compose_replacement(
        self,
        *,
        context: TrustedPersistenceContext,
        command: ReplaceDocumentCommand,
        operation_id: UUID,
        command_digest: CommandDigest,
        accepted_at: datetime,
    ) -> SourceLifecycleCommand:
        evidence = await self.replacement_evidence.prepare(
            context=context,
            command=command,
            operation_id=operation_id,
            command_digest=command_digest,
            observed_at=accepted_at,
        )
        if (
            evidence.source.source_object_id != command.source_object_id
            or (evidence.source.workspace_id, evidence.source.environment_id)
            != (command.workspace_id, command.environment_id)
            or evidence.revision.kind is not RevisionKind.CONTENT
            or evidence.revision.reappearance_after_tombstone_revision_id is not None
            or evidence.provenance.event_digest != command_digest.value
            or evidence.provenance.producer_kind != "lifecycle"
            or evidence.provenance.producer_reference != self.service_name
            or evidence.provenance.observer_service != self.service_name
        ):
            raise ValueError("Replacement authority evidence is invalid.")
        canonical = self.canonical_command_factory(
            context=context,
            source=evidence.source,
            revision=evidence.revision,
            provenance=evidence.provenance,
            expected_current_revision_id=command.expected_current_revision_id,
            idempotency_key=IdempotencyKey(command.idempotency_key),
            canonical_acceptance_id=operation_id,
            admission_decision_id=evidence.admission_decision_id,
            access_policy_version_id=evidence.access_policy_version_id,
            accepted_at=accepted_at,
        )
        return SourceLifecycleCommand(
            kind=SourceLifecycleKind.REPLACEMENT,
            source=evidence.source,
            revision=evidence.revision,
            provenance=evidence.provenance,
            observation_idempotency_key=IdempotencyKey(
                _child_idempotency_key(command.idempotency_key, "observation")
            ),
            canonical_acceptance=canonical,
            receipt_association=evidence.receipt_association,
        )

    async def compose_tombstone(
        self,
        *,
        context: TrustedPersistenceContext,
        command: TombstoneDocumentCommand,
        operation_id: UUID,
        command_digest: CommandDigest,
        accepted_at: datetime,
    ) -> SourceLifecycleCommand:
        async with self.uow_factory(context) as uow:
            source = await uow.sources.resolve_source(command.source_object_id)
            current = await uow.canonical.resolve_current(command.source_object_id)
            if (
                source is None
                or current is None
                or current.current_revision_id != command.expected_current_revision_id
            ):
                await uow.rollback()
                from spine.application.persistence.errors import OptimisticConflictError

                raise OptimisticConflictError(
                    "Document lifecycle expected revision is stale or unavailable."
                )
            await uow.commit()
        revision_id = uuid5(NAMESPACE_URL, f"spine:document-lifecycle:tombstone:{operation_id}")
        revision = tombstone_revision(
            revision_id=revision_id,
            source_object_id=source.source_object_id,
            workspace_id=source.workspace_id,
            environment_id=source.environment_id,
            kind=RevisionKind.TOMBSTONE,
            revision_schema_version="source-revision:v1",
            revision_metadata_schema="revision-metadata:r1-document-v1",
            deletion_reason=command.deletion_reason,
            deletion_provenance="document-lifecycle",
            observed_at=accepted_at,
        )
        provenance = SourceRevisionProvenance(
            provenance_id=uuid5(
                NAMESPACE_URL, f"spine:document-lifecycle:provenance:{operation_id}"
            ),
            source_object_id=source.source_object_id,
            revision_id=revision.revision_id,
            workspace_id=source.workspace_id,
            environment_id=source.environment_id,
            producer_kind="lifecycle",
            producer_reference="document-lifecycle",
            event_identity=_bounded_identifier(f"tombstone-{operation_id.hex}"),
            event_digest=command_digest.value,
            observer_service=self.service_name,
            received_at=accepted_at,
            observed_at=accepted_at,
        )
        canonical = self.canonical_command_factory(
            context=context,
            source=source,
            revision=revision,
            provenance=provenance,
            expected_current_revision_id=command.expected_current_revision_id,
            idempotency_key=IdempotencyKey(command.idempotency_key),
            canonical_acceptance_id=operation_id,
            admission_decision_id=None,
            access_policy_version_id=None,
            accepted_at=accepted_at,
        )
        return SourceLifecycleCommand(
            kind=SourceLifecycleKind.DELETION,
            source=source,
            revision=revision,
            provenance=provenance,
            observation_idempotency_key=IdempotencyKey(
                _child_idempotency_key(command.idempotency_key, "observation")
            ),
            canonical_acceptance=canonical,
        )


@dataclass(frozen=True, slots=True)
class CanonicalDocumentLifecycleCommandService:
    """Compose approved lifecycle evidence with canonical CAS and processing."""

    composer: LifecycleCommandComposer
    coordinator: SourceLifecycleCoordinator
    processing: ReplacementProcessingService
    uow_factory: UnitOfWorkFactory
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)

    async def replace(
        self,
        *,
        context: TrustedPersistenceContext,
        command: ReplaceDocumentCommand,
    ) -> SourceLifecycleOperationReceipt:
        self._assert_scope(context, command)
        operation_id = self._operation_id(command, LifecycleOperationKind.REPLACEMENT)
        digest = self.command_digest(command)
        existing = await self._resolve_existing_operation(
            context=context,
            command=command,
            operation_id=operation_id,
            kind=LifecycleOperationKind.REPLACEMENT,
            revision_kind=RevisionKind.CONTENT,
            command_digest=digest,
        )
        if existing is not None:
            return await self._dispatch_replacement(
                context=context,
                command=command,
                acceptance=existing,
                replay=True,
            )
        lifecycle = await self.composer.compose_replacement(
            context=context,
            command=command,
            operation_id=operation_id,
            command_digest=digest,
            accepted_at=self._now(),
        )
        result = await self.coordinator.apply(lifecycle)
        acceptance = self._acceptance(result, operation_id, RevisionKind.CONTENT)
        return await self._dispatch_replacement(
            context=context,
            command=command,
            acceptance=acceptance,
            replay=bool(result.replay),
        )

    async def _dispatch_replacement(
        self,
        *,
        context: TrustedPersistenceContext,
        command: ReplaceDocumentCommand,
        acceptance: object,
        replay: bool,
    ) -> SourceLifecycleOperationReceipt:
        operation_id = acceptance.canonical_acceptance_id
        intent = ReplacementProcessingIntent(
            lifecycle_operation_id=operation_id,
            canonical_acceptance_id=acceptance.canonical_acceptance_id,
            source_object_id=acceptance.source_object_id,
            source_revision_id=acceptance.source_revision_id,
            idempotency_key=f"replacement-processing:{acceptance.source_revision_id}",
        )
        try:
            processing = await self.processing.replacement(context=context, intent=intent)
            processing_run_id = getattr(processing, "run_id", None)
            if not isinstance(processing_run_id, UUID):
                raise ValueError("Replacement processing receipt is unavailable.")
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            logger.warning(
                "Replacement processing requires reconciliation",
                extra={"lifecycle_operation_id": str(operation_id)},
            )
            return self._receipt(
                command=command,
                acceptance=acceptance,
                kind=LifecycleOperationKind.REPLACEMENT,
                status=LifecycleOperationStatus.RECONCILIATION_REQUIRED,
                replay=replay,
                failure=LifecycleFailureCode.PROCESSING_UNAVAILABLE,
                retryable=True,
            )
        return self._receipt(
            command=command,
            acceptance=acceptance,
            kind=LifecycleOperationKind.REPLACEMENT,
            status=LifecycleOperationStatus.PROCESSING_DISPATCHED,
            replay=replay,
            processing_run_id=processing_run_id,
        )

    async def tombstone(
        self,
        *,
        context: TrustedPersistenceContext,
        command: TombstoneDocumentCommand,
    ) -> SourceLifecycleOperationReceipt:
        self._assert_scope(context, command)
        operation_id = self._operation_id(command, LifecycleOperationKind.TOMBSTONE)
        digest = self.command_digest(command)
        existing = await self._resolve_existing_operation(
            context=context,
            command=command,
            operation_id=operation_id,
            kind=LifecycleOperationKind.TOMBSTONE,
            revision_kind=RevisionKind.TOMBSTONE,
            command_digest=digest,
        )
        if existing is not None:
            return self._receipt(
                command=command,
                acceptance=existing,
                kind=LifecycleOperationKind.TOMBSTONE,
                status=LifecycleOperationStatus.COMPLETED,
                replay=True,
                completed_at=existing.accepted_at,
            )
        lifecycle = await self.composer.compose_tombstone(
            context=context,
            command=command,
            operation_id=operation_id,
            command_digest=digest,
            accepted_at=self._now(),
        )
        result = await self.coordinator.apply(lifecycle)
        acceptance = self._acceptance(result, operation_id, RevisionKind.TOMBSTONE)
        return self._receipt(
            command=command,
            acceptance=acceptance,
            kind=LifecycleOperationKind.TOMBSTONE,
            status=LifecycleOperationStatus.COMPLETED,
            replay=bool(result.replay),
            completed_at=acceptance.accepted_at,
        )

    async def _resolve_existing_operation(
        self,
        *,
        context: TrustedPersistenceContext,
        command: ReplaceDocumentCommand | TombstoneDocumentCommand,
        operation_id: UUID,
        kind: LifecycleOperationKind,
        revision_kind: RevisionKind,
        command_digest: CommandDigest,
    ) -> object | None:
        """Resolve durable command replay before repeating storage or preflight work."""

        async with self.uow_factory(context) as uow:
            acceptance = await uow.canonical.resolve_acceptance(operation_id)
            if acceptance is None:
                await uow.rollback()
                return None
            revision = await uow.sources.resolve_revision(acceptance.source_revision_id)
            provenances = (
                await uow.sources.list_provenance(acceptance.source_revision_id)
                if revision is not None
                else ()
            )
            provenance = next(
                (
                    value
                    for value in provenances
                    if value.provenance_id == acceptance.provenance_id
                ),
                None,
            )
            if (
                provenance is None
                or provenance.event_digest != command_digest.value
                or provenance.producer_kind != "lifecycle"
                or provenance.producer_reference != DOCUMENT_LIFECYCLE_SERVICE
                or provenance.observer_service != DOCUMENT_LIFECYCLE_SERVICE
            ):
                await uow.rollback()
                raise idempotency_conflict()
            if (
                acceptance.canonical_acceptance_id != operation_id
                or acceptance.source_object_id != command.source_object_id
                or acceptance.predecessor_revision_id
                != command.expected_current_revision_id
                or revision is None
                or revision.kind is not revision_kind
                or (
                    kind is LifecycleOperationKind.REPLACEMENT
                    and acceptance.admission_decision_id is None
                )
                or (
                    kind is LifecycleOperationKind.TOMBSTONE
                    and acceptance.admission_decision_id is not None
                )
            ):
                await uow.rollback()
                raise ValueError("Stored document lifecycle operation is invalid.")
            await uow.commit()
            return acceptance

    async def status(
        self,
        *,
        context: TrustedPersistenceContext,
        operation_id: UUID,
    ) -> SourceLifecycleOperationReceipt | None:
        if not isinstance(context.scope, EnvironmentScope):
            return None
        async with self.uow_factory(context) as uow:
            acceptance = await uow.canonical.resolve_acceptance(operation_id)
            if acceptance is None or acceptance.predecessor_revision_id is None:
                await uow.rollback()
                return None
            revision = await uow.sources.resolve_revision(acceptance.source_revision_id)
            if revision is None:
                await uow.rollback()
                return None
            provenances = await uow.sources.list_provenance(
                acceptance.source_revision_id
            )
            lifecycle_provenance = next(
                (
                    value
                    for value in provenances
                    if value.provenance_id == acceptance.provenance_id
                    and value.producer_kind == "lifecycle"
                    and value.producer_reference == DOCUMENT_LIFECYCLE_SERVICE
                    and value.observer_service == DOCUMENT_LIFECYCLE_SERVICE
                ),
                None,
            )
            if lifecycle_provenance is None:
                await uow.rollback()
                return None
            processing_run_id = None
            completed_at = None
            failure = None
            retryable = False
            if revision.kind is RevisionKind.TOMBSTONE:
                kind = LifecycleOperationKind.TOMBSTONE
                status = LifecycleOperationStatus.COMPLETED
                completed_at = acceptance.accepted_at
            else:
                kind = LifecycleOperationKind.REPLACEMENT
                process_id = replacement_processing_operation_id(
                    workspace_id=acceptance.workspace_id,
                    environment_id=acceptance.environment_id,
                    source_revision_id=acceptance.source_revision_id,
                )
                lineage = await uow.ingestion.resolve_process_only_lineage(process_id)
                if lineage is None:
                    status = LifecycleOperationStatus.RECONCILIATION_REQUIRED
                    failure = LifecycleFailureCode.PROCESSING_UNAVAILABLE
                    retryable = True
                else:
                    processing_run_id = lineage.ingestion_run_id
                    run = await uow.ingestion.derive_run_state(lineage.ingestion_run_id)
                    if run is not None and run.state in {
                        IngestionRunState.SUCCEEDED,
                        IngestionRunState.PARTIALLY_SUCCEEDED,
                    }:
                        status = LifecycleOperationStatus.COMPLETED
                    elif run is not None and run.state in {
                        IngestionRunState.FAILED,
                        IngestionRunState.CANCELLED,
                    }:
                        status = LifecycleOperationStatus.FAILED
                        failure = LifecycleFailureCode.PROCESSING_UNAVAILABLE
                        retryable = run.state is IngestionRunState.FAILED
                    else:
                        status = LifecycleOperationStatus.PROCESSING_DISPATCHED
            await uow.commit()
        if (
            kind is LifecycleOperationKind.REPLACEMENT
            and status is LifecycleOperationStatus.RECONCILIATION_REQUIRED
        ):
            intent = ReplacementProcessingIntent(
                lifecycle_operation_id=operation_id,
                canonical_acceptance_id=acceptance.canonical_acceptance_id,
                source_object_id=acceptance.source_object_id,
                source_revision_id=acceptance.source_revision_id,
                idempotency_key=(
                    f"replacement-processing:{acceptance.source_revision_id}"
                ),
            )
            try:
                processing = await self.processing.replacement(
                    context=context,
                    intent=intent,
                )
                repaired_run_id = getattr(processing, "run_id", None)
                if not isinstance(repaired_run_id, UUID):
                    raise ValueError("Replacement processing receipt is unavailable.")
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception:
                logger.warning(
                    "Replacement processing reconciliation remains pending",
                    extra={"lifecycle_operation_id": str(operation_id)},
                )
            else:
                processing_run_id = repaired_run_id
                status = LifecycleOperationStatus.PROCESSING_DISPATCHED
                failure = None
                retryable = False
        return SourceLifecycleOperationReceipt(
            operation_id=operation_id,
            operation_kind=kind,
            source_object_id=acceptance.source_object_id,
            expected_current_revision_id=acceptance.predecessor_revision_id,
            accepted_revision_id=acceptance.source_revision_id,
            canonical_acceptance_id=acceptance.canonical_acceptance_id,
            processing_run_id=processing_run_id,
            status=status,
            idempotency_receipt_id=acceptance.idempotency_receipt_id,
            accepted_at=acceptance.accepted_at,
            completed_at=completed_at,
            safe_failure_code=failure,
            retryable=retryable,
            replay=False,
        )

    @staticmethod
    def command_digest(
        command: ReplaceDocumentCommand | TombstoneDocumentCommand,
    ) -> CommandDigest:
        kind = (
            LifecycleOperationKind.REPLACEMENT
            if isinstance(command, ReplaceDocumentCommand)
            else LifecycleOperationKind.TOMBSTONE
        )
        return digest_command(
            operation=PersistenceOperation("document_lifecycle"),
            operation_schema_version=1,
            payload={
                "operation_kind": kind.value,
                "command": command.model_dump(mode="json", exclude={"idempotency_key"}),
            },
        )

    @staticmethod
    def _operation_id(
        command: ReplaceDocumentCommand | TombstoneDocumentCommand,
        kind: LifecycleOperationKind,
    ) -> UUID:
        return uuid5(
            NAMESPACE_URL,
            "spine:document-lifecycle:v1:"
            f"{command.workspace_id}:{command.environment_id}:{kind.value}:"
            f"{command.idempotency_key}",
        )

    @staticmethod
    def _assert_scope(
        context: TrustedPersistenceContext,
        command: ReplaceDocumentCommand | TombstoneDocumentCommand,
    ) -> None:
        scope = context.scope
        if not isinstance(scope, EnvironmentScope) or (
            scope.workspace_id,
            scope.environment_id,
        ) != (command.workspace_id, command.environment_id):
            raise ValueError("Document lifecycle scope is unavailable.")

    @staticmethod
    def _acceptance(result: object, operation_id: UUID, kind: RevisionKind) -> object:
        canonical = getattr(result, "acceptance", None)
        acceptance = getattr(canonical, "acceptance", None)
        revision = getattr(canonical, "revision", None)
        if (
            acceptance is None
            or acceptance.canonical_acceptance_id != operation_id
            or revision is None
            or revision.kind is not kind
        ):
            raise ValueError("Canonical lifecycle result is invalid.")
        return acceptance

    @staticmethod
    def _receipt(
        *,
        command: ReplaceDocumentCommand | TombstoneDocumentCommand,
        acceptance: object,
        kind: LifecycleOperationKind,
        status: LifecycleOperationStatus,
        replay: bool,
        processing_run_id: UUID | None = None,
        completed_at: datetime | None = None,
        failure: LifecycleFailureCode | None = None,
        retryable: bool = False,
    ) -> SourceLifecycleOperationReceipt:
        return SourceLifecycleOperationReceipt(
            operation_id=acceptance.canonical_acceptance_id,
            operation_kind=kind,
            source_object_id=acceptance.source_object_id,
            expected_current_revision_id=command.expected_current_revision_id,
            accepted_revision_id=acceptance.source_revision_id,
            canonical_acceptance_id=acceptance.canonical_acceptance_id,
            processing_run_id=processing_run_id,
            status=status,
            idempotency_receipt_id=acceptance.idempotency_receipt_id,
            accepted_at=acceptance.accepted_at,
            completed_at=completed_at,
            safe_failure_code=failure,
            retryable=retryable,
            replay=replay,
        )

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Document lifecycle clock must be timezone-aware.")
        return value


def replacement_processing_operation_id(
    *,
    workspace_id: UUID,
    environment_id: UUID,
    source_revision_id: UUID,
) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        "spine:replacement-processing:v1:"
        f"{workspace_id}:{environment_id}:{source_revision_id}",
    )


def _child_idempotency_key(parent: str, suffix: str) -> str:
    value = f"{parent}:{suffix}"
    if len(value) <= 255:
        return value
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return f"{value[:220]}:{digest[:32]}"


def _bounded_identifier(value: str) -> str:
    if len(value) <= 64:
        return value
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return f"{value[:31]}-{digest[:32]}"


class DocumentLifecycleCommandService(Protocol):
    async def replace(
        self,
        *,
        context: TrustedPersistenceContext,
        command: ReplaceDocumentCommand,
    ) -> SourceLifecycleOperationReceipt: ...

    async def tombstone(
        self,
        *,
        context: TrustedPersistenceContext,
        command: TombstoneDocumentCommand,
    ) -> SourceLifecycleOperationReceipt: ...

    async def status(
        self,
        *,
        context: TrustedPersistenceContext,
        operation_id: UUID,
    ) -> SourceLifecycleOperationReceipt | None: ...


__all__ = [
    "CanonicalDocumentLifecycleCommandService",
    "DocumentLifecycleCommandService",
    "ExistingUploadIntent",
    "LifecycleFailureCode",
    "LifecycleOperationKind",
    "LifecycleOperationStatus",
    "LifecycleCommandComposer",
    "LifecycleCanonicalCommandFactory",
    "ReplacementProcessingIntent",
    "ReplacementProcessingService",
    "ReplacementLifecycleEvidence",
    "ReplacementLifecycleEvidenceProvider",
    "ServerDocumentLifecycleComposer",
    "ReplaceDocumentCommand",
    "SourceLifecycleOperationReceipt",
    "TombstoneDocumentCommand",
    "replacement_processing_operation_id",
]
