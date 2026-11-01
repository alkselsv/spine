"""Idempotent document processing retry and reprocess commands."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from types import SimpleNamespace
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field

PROCESSING_REQUEST_EVENT_TYPE = "ingestion.processing.requested"
PROCESSING_REQUEST_SCHEMA_VERSION = 1
from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    TrustedPersistenceContext,
)
from spine.application.persistence.errors import ConstraintConflictError
from spine.application.persistence.ingestion import (
    ExistingRevisionBindingCommand,
    IngestionRunCommand,
    ProcessingReceiptCommand,
)
from spine.application.persistence.idempotency import IdempotencyKey
from spine.application.persistence.outbox import (
    OpaqueObjectReference,
    OutboxEventRegistry,
)
from spine.application.persistence.parsed_artifacts import ParsedArtifactCommand
from spine.application.persistence.unit_of_work import UnitOfWorkFactory
from spine.application.processing_disposition import (
    CanonicalCurrentProcessingDispositionAuthority,
)
from spine.application.document_lifecycle import (
    ReplacementProcessingIntent,
    replacement_processing_operation_id,
)
from spine.domain.ingestion import (
    FailureStage,
    IngestionFailure,
    IngestionItemState,
    IngestionRun,
    IngestionRunSource,
    IngestionRunSourceBinding,
    IngestionRunState,
    IngestionTargetKind,
    IngestionTransition,
    IngestionWarning,
    ProcessOnlyOperationLineage,
    ProcessingCoverage,
    ProcessingExclusion,
    ProcessingReceipt,
    SafeDiagnosticDetail,
    WarningStage,
)
from spine.domain.sources import RevisionKind
from spine.domain.sources.original_reference import ObjectReference
from spine.domain.artifacts import ParseCoverage, ParsedDocument


PROCESSING_COMMAND_SCHEMA_VERSION = 1
DEFAULT_PROCESSING_PROFILE = "document-processing:r1"
ALLOWED_PROCESSING_PROFILES = frozenset({DEFAULT_PROCESSING_PROFILE})
class ProcessingCommandStatus(str, Enum):
    ACCEPTED = "accepted"
    READY = "ready"
    PROCESSING = "processing"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNAVAILABLE = "unavailable"


class ProcessingCommandRejected(ConstraintConflictError):
    """The requested immutable processing input is not eligible."""


class _ProcessingCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_id: UUID
    environment_id: UUID
    source_object_id: UUID
    source_revision_id: UUID
    expected_current_revision_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=255)


class RetryProcessingCommand(_ProcessingCommand):
    parent_failure_id: UUID
    parent_processing_receipt_id: UUID | None = None


class ReprocessProcessingCommand(_ProcessingCommand):
    parent_processing_receipt_id: UUID | None = None
    processing_profile_reference: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z][a-z0-9_.:-]{0,127}$",
    )


class ReplacementProcessingCommand(_ProcessingCommand):
    lifecycle_operation_id: UUID
    canonical_acceptance_id: UUID
    processing_profile_reference: str = Field(
        default=DEFAULT_PROCESSING_PROFILE,
        min_length=1,
        max_length=128,
        pattern=r"^[a-z][a-z0-9_.:-]{0,127}$",
    )


@dataclass(frozen=True, slots=True)
class ProcessingCommandResult:
    operation_id: UUID
    run_id: UUID
    item_id: UUID
    source_object_id: UUID
    source_revision_id: UUID
    trigger_kind: str
    status: ProcessingCommandStatus
    replay: bool
    processing_receipt_id: UUID | None
    failure_ids: tuple[UUID, ...]
    next_action: str


class ProcessingCommandService(Protocol):
    async def retry(
        self,
        *,
        context: TrustedPersistenceContext,
        command: RetryProcessingCommand,
    ) -> ProcessingCommandResult: ...

    async def reprocess(
        self,
        *,
        context: TrustedPersistenceContext,
        command: ReprocessProcessingCommand,
    ) -> ProcessingCommandResult: ...

    async def replacement(
        self,
        *,
        context: TrustedPersistenceContext,
        intent: ReplacementProcessingIntent,
    ) -> ProcessingCommandResult: ...

    async def status(
        self,
        *,
        context: TrustedPersistenceContext,
        operation_id: UUID,
    ) -> ProcessingCommandResult | None: ...


class _OriginalContentReader(Protocol):
    def read_finalized(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        source_revision_id: UUID,
        receipt: object,
        max_bytes: int,
    ) -> AsyncIterable[bytes]: ...


@dataclass(frozen=True, slots=True)
class _FinalizedOriginalReceipt:
    """Minimal receipt view required by the finalized-content reader."""

    object_reference: ObjectReference


class _DocumentParser(Protocol):
    def __call__(self, revision: object, content: bytes, *, media_type: str) -> ParsedDocument: ...


@dataclass(frozen=True, slots=True)
class OrchestratedProcessingCommandService:
    """Compose retry/reprocess against canonical source and ingestion ports."""

    uow_factory: UnitOfWorkFactory
    validation_reader: _OriginalContentReader
    parsed_artifacts: object
    parser: _DocumentParser
    context_factory: Callable[[PersistenceOperation, UUID], TrustedPersistenceContext]
    outbox_events: OutboxEventRegistry
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    max_parse_bytes: int = 10_000_000
    service_name: str = "document-processing-command"

    def __post_init__(self) -> None:
        if self.max_parse_bytes <= 0:
            raise ValueError("max_parse_bytes must be positive")
        from spine.application.document_ingestion import IngestionRequestPayload

        self.outbox_events.register(
            event_type=PROCESSING_REQUEST_EVENT_TYPE,
            schema_version=PROCESSING_REQUEST_SCHEMA_VERSION,
            payload_type=IngestionRequestPayload,
        )

    async def retry(
        self,
        *,
        context: TrustedPersistenceContext,
        command: RetryProcessingCommand,
    ) -> ProcessingCommandResult:
        return await self._execute(context=context, command=command, trigger_kind="retry")

    async def reprocess(
        self,
        *,
        context: TrustedPersistenceContext,
        command: ReprocessProcessingCommand,
    ) -> ProcessingCommandResult:
        return await self._execute(context=context, command=command, trigger_kind="reprocess")

    async def replacement(
        self,
        *,
        context: TrustedPersistenceContext,
        intent: ReplacementProcessingIntent,
    ) -> ProcessingCommandResult:
        scope = context.scope
        if (
            not isinstance(scope, EnvironmentScope)
            or intent.lifecycle_operation_id != intent.canonical_acceptance_id
        ):
            raise ProcessingCommandRejected()
        return await self._execute(
            context=context,
            command=ReplacementProcessingCommand(
                workspace_id=scope.workspace_id,
                environment_id=scope.environment_id,
                source_object_id=intent.source_object_id,
                source_revision_id=intent.source_revision_id,
                expected_current_revision_id=intent.source_revision_id,
                idempotency_key=intent.idempotency_key,
                lifecycle_operation_id=intent.lifecycle_operation_id,
                canonical_acceptance_id=intent.canonical_acceptance_id,
                processing_profile_reference=intent.processing_profile_reference,
            ),
            trigger_kind="replacement",
        )

    async def status(
        self,
        *,
        context: TrustedPersistenceContext,
        operation_id: UUID,
    ) -> ProcessingCommandResult | None:
        self._assert_context_scope(context)
        trace_id = self._stable_id(operation_id, "status-trace")
        async with self.uow_factory(
            self.context_factory(PersistenceOperation("document_processing.status"), trace_id)
        ) as uow:
            lineage = await uow.ingestion.resolve_process_only_lineage(operation_id)
            if lineage is None:
                await uow.rollback()
                return None
            result = await self._result_from_lineage(uow, lineage, replay=False)
            await uow.commit()
            return result

    async def _execute(
        self,
        *,
        context: TrustedPersistenceContext,
        command: RetryProcessingCommand | ReprocessProcessingCommand | ReplacementProcessingCommand,
        trigger_kind: str,
    ) -> ProcessingCommandResult:
        self._assert_context_scope(context)
        if (command.workspace_id, command.environment_id) != (
            context.scope.workspace_id,  # type: ignore[union-attr]
            context.scope.environment_id,  # type: ignore[union-attr]
        ):
            raise ProcessingCommandRejected()
        if trigger_kind in {"reprocess", "replacement"}:
            profile = command.processing_profile_reference  # type: ignore[union-attr]
            if profile not in ALLOWED_PROCESSING_PROFILES:
                raise ProcessingCommandRejected()
        else:
            profile = DEFAULT_PROCESSING_PROFILE
        command_digest = _command_digest(command, trigger_kind)
        operation_id = (
            replacement_processing_operation_id(
                workspace_id=command.workspace_id,
                environment_id=command.environment_id,
                source_revision_id=command.source_revision_id,
            )
            if trigger_kind == "replacement"
            else self._stable_id(command.idempotency_key, trigger_kind)
        )
        run_id = self._stable_id(operation_id, "run")
        item_id = self._stable_id(operation_id, "item")
        now = self._now()
        requested_at = (
            await self._replacement_requested_at(
                command=command,
                operation_id=operation_id,
            )
            if trigger_kind == "replacement"
            else now
        )
        command_identity = _bounded_identifier(
            f"document-processing.{trigger_kind}.{operation_id.hex[:12]}"
        )
        run = IngestionRun(
            ingestion_run_id=run_id,
            workspace_id=command.workspace_id,
            environment_id=command.environment_id,
            command_identity=command_identity,
            command_digest="0" * 64,
            trigger_kind=trigger_kind,  # type: ignore[arg-type]
            trigger_reference=_bounded_identifier(f"operation:{operation_id}"),
            requested_scope_schema="ingestion-scope:v1",
            requested_scope_digest=command_digest,
            requested_at=requested_at,
        )
        item = IngestionRunSource(
            ingestion_run_source_id=item_id,
            ingestion_run_id=run_id,
            workspace_id=command.workspace_id,
            environment_id=command.environment_id,
            input_ordinal=1,
            input_identity=_bounded_identifier(f"process-{command.source_revision_id.hex[:16]}"),
            input_digest=command_digest,
            command_identity=_bounded_identifier(f"{command_identity}.item"),
            command_digest=command_digest,
            created_at=requested_at,
        )
        trace_id = self._stable_id(operation_id, "command-trace")
        async with self.uow_factory(
            self.context_factory(PersistenceOperation("ingestion_run"), trace_id)
        ) as uow:
            run_result = await uow.ingestion.create_run(
                IngestionRunCommand.create(
                    run=run,
                    idempotency_key=IdempotencyKey(command.idempotency_key),
                )
            )
            created_run = run_result.value
            if not isinstance(created_run, IngestionRun) or created_run.idempotency_receipt_id is None:
                await uow.rollback()
                raise ProcessingCommandRejected()
            if run_result.replay:
                lineage = await uow.ingestion.resolve_process_only_lineage(operation_id)
                if lineage is None:
                    evidence = await self._resolve_eligibility(uow, command, trigger_kind)
                    existing_item = await uow.ingestion.resolve_item(item_id)
                    if existing_item is None:
                        await uow.ingestion.create_item(item)
                    elif existing_item != item:
                        await uow.rollback()
                        raise ProcessingCommandRejected()
                    else:
                        item = existing_item
                    await uow.commit()
                else:
                    existing_item = await uow.ingestion.resolve_item(
                        lineage.ingestion_run_source_id
                    )
                    existing_binding = (
                        await uow.ingestion.resolve_binding(
                            lineage.ingestion_run_source_id
                        )
                        if existing_item is not None
                        else None
                    )
                    revision = await uow.sources.resolve_revision(
                        lineage.source_revision_id
                    )
                    processing_receipt = (
                        await uow.ingestion.resolve_processing_receipt(
                            lineage.ingestion_run_source_id
                        )
                        if existing_item is not None
                        else None
                    )
                    if (
                        existing_item is None
                        or existing_binding is None
                        or revision is None
                    ):
                        await uow.rollback()
                        raise ProcessingCommandRejected()
                    await uow.commit()
                    if processing_receipt is None:
                        await self._process(
                            context=context,
                            operation_id=operation_id,
                            run=created_run,
                            item=existing_item,
                            binding=existing_binding,
                            revision=revision,
                            profile=profile,
                            command_key=command.idempotency_key,
                        )
                    result = await self.status(context=context, operation_id=operation_id)
                    if result is None:
                        raise ProcessingCommandRejected()
                    return _replayed_processing_result(result)
            else:
                evidence = await self._resolve_eligibility(uow, command, trigger_kind)
                await uow.ingestion.create_item(item)
                await uow.commit()

        parent_binding = evidence["binding"]
        binding = IngestionRunSourceBinding(
            binding_id=self._stable_id(operation_id, "binding"),
            ingestion_run_source_id=item_id,
            source_object_id=command.source_object_id,
            source_revision_id=command.source_revision_id,
            workspace_id=command.workspace_id,
            environment_id=command.environment_id,
            binding_command_identity=_bounded_identifier(f"{command_identity}.binding"),
            binding_command_digest="0" * 64,
            original_reference=parent_binding.original_reference,  # type: ignore[union-attr]
            original_receipt_kind=parent_binding.original_receipt_kind,  # type: ignore[union-attr]
            original_receipt_id=parent_binding.original_receipt_id,  # type: ignore[union-attr]
            original_receipt_digest=parent_binding.original_receipt_digest,  # type: ignore[union-attr]
            bound_at=now,
        )
        async with self.uow_factory(
            self.context_factory(PersistenceOperation("ingestion_existing_binding"), trace_id)
        ) as uow:
            binding_result = await uow.ingestion.bind_existing_revision(
                ExistingRevisionBindingCommand.create(
                    item=item,
                    binding=binding,
                    idempotency_key=IdempotencyKey(_child_key(command.idempotency_key, "binding")),
                )
            )
            binding = binding_result.value  # type: ignore[assignment]
            parent_binding = evidence["binding"]
            await _append_run_transition(
                uow,
                run,
                IngestionRunState.PROCESSING,
                now,
                self.service_name,
            )
            lineage = ProcessOnlyOperationLineage(
                operation_id=operation_id,
                workspace_id=command.workspace_id,
                environment_id=command.environment_id,
                ingestion_run_id=run_id,
                ingestion_run_source_id=item_id,
                trigger_kind=trigger_kind,  # type: ignore[arg-type]
                source_object_id=command.source_object_id,
                source_revision_id=command.source_revision_id,
                original_receipt_id=binding.original_receipt_id,  # type: ignore[arg-type]
                original_receipt_digest=binding.original_receipt_digest,  # type: ignore[arg-type]
                parent_failure_id=(
                    command.parent_failure_id
                    if isinstance(command, RetryProcessingCommand)
                    else None
                ),
                parent_processing_receipt_id=getattr(
                    command, "parent_processing_receipt_id", None
                ),
                pinned_processing_profile_reference=profile,
                command_digest=command_digest,
                idempotency_receipt_id=created_run.idempotency_receipt_id,  # type: ignore[arg-type]
            )
            await uow.ingestion.create_process_only_lineage(lineage)
            await uow.commit()
        await self._process(
            context=context,
            operation_id=operation_id,
            run=created_run,
            item=item,
            binding=binding.model_copy(update={"binding_command_digest": binding.binding_command_digest}),
            revision=evidence["revision"],
            profile=profile,
            command_key=command.idempotency_key,
        )
        result = await self.status(context=context, operation_id=operation_id)
        if result is None:
            raise ProcessingCommandRejected()
        return _replayed_processing_result(result) if run_result.replay else result

    async def _replacement_requested_at(
        self,
        *,
        command: RetryProcessingCommand | ReprocessProcessingCommand | ReplacementProcessingCommand,
        operation_id: UUID,
    ) -> datetime:
        if not isinstance(command, ReplacementProcessingCommand):
            raise ProcessingCommandRejected()
        trace_id = self._stable_id(operation_id, "identity-trace")
        async with self.uow_factory(
            self.context_factory(PersistenceOperation("ingestion_run"), trace_id)
        ) as uow:
            acceptance = await uow.canonical.resolve_acceptance(
                command.canonical_acceptance_id
            )
            if (
                acceptance is None
                or acceptance.source_object_id != command.source_object_id
                or acceptance.source_revision_id != command.source_revision_id
                or acceptance.accepted_at.tzinfo is None
                or acceptance.accepted_at.utcoffset() is None
            ):
                await uow.rollback()
                raise ProcessingCommandRejected()
            await uow.commit()
            return acceptance.accepted_at

    async def _resolve_eligibility(
        self,
        uow: object,
        command: RetryProcessingCommand | ReprocessProcessingCommand | ReplacementProcessingCommand,
        trigger_kind: str,
    ) -> dict[str, object]:
        revision = await uow.sources.resolve_revision(command.source_revision_id)  # type: ignore[attr-defined]
        source = await uow.sources.resolve_source(command.source_object_id)  # type: ignore[attr-defined]
        current = await uow.canonical.resolve_current(command.source_object_id)  # type: ignore[attr-defined]
        if (
            source is None
            or revision is None
            or current is None
            or revision.kind is not RevisionKind.CONTENT
            or revision.source_object_id != command.source_object_id
            or current.current_revision_id != command.source_revision_id
            or command.expected_current_revision_id != command.source_revision_id
        ):
            raise ProcessingCommandRejected()
        processing = await CanonicalCurrentProcessingDispositionAuthority(
            uow.canonical  # type: ignore[attr-defined]
        ).resolve(
            workspace_id=command.workspace_id,
            environment_id=command.environment_id,
            source_object_id=command.source_object_id,
            source_revision_id=command.source_revision_id,
        )
        if not processing.eligible:
            raise ProcessingCommandRejected()
        parent_failure = None
        parent_item = None
        if trigger_kind == "retry":
            assert isinstance(command, RetryProcessingCommand)
            parent_failure = await uow.ingestion.resolve_failure_by_id(command.parent_failure_id)  # type: ignore[attr-defined]
            if parent_failure is None or not parent_failure.retryable:
                raise ProcessingCommandRejected()
            parent_item = await uow.ingestion.resolve_item(parent_failure.ingestion_run_source_id)  # type: ignore[attr-defined]
            if parent_item is None:
                raise ProcessingCommandRejected()
        elif getattr(command, "parent_processing_receipt_id", None) is not None:
            parent_receipt = await uow.ingestion.resolve_processing_receipt_by_id(command.parent_processing_receipt_id)  # type: ignore[attr-defined]
            if parent_receipt is None:
                raise ProcessingCommandRejected()
            parent_item = await uow.ingestion.resolve_item(parent_receipt.ingestion_run_source_id)  # type: ignore[attr-defined]
            if parent_item is None:
                raise ProcessingCommandRejected()
        if parent_item is None and trigger_kind == "replacement":
            acceptance = await uow.canonical.resolve_acceptance(  # type: ignore[attr-defined]
                command.canonical_acceptance_id  # type: ignore[union-attr]
            )
            associations = await uow.sources.resolve_receipt_associations(  # type: ignore[attr-defined]
                command.source_revision_id
            )
            association = associations[0] if associations else None
            if (
                acceptance is None
                or acceptance.canonical_acceptance_id
                != command.canonical_acceptance_id  # type: ignore[union-attr]
                or acceptance.source_revision_id != command.source_revision_id
                or current.acceptance_id != acceptance.canonical_acceptance_id
                or association is None
            ):
                raise ProcessingCommandRejected()
            parent_binding = SimpleNamespace(
                source_object_id=association.source_object_id,
                source_revision_id=association.revision_id,
                original_reference=association.original_reference,
                original_receipt_kind=association.original_receipt_kind,
                original_receipt_id=association.original_receipt_id,
                original_receipt_digest=association.original_sha256,
            )
        elif parent_item is None and trigger_kind == "reprocess":
            bindings = await uow.ingestion.list_bindings_for_source_revision(  # type: ignore[attr-defined]
                command.source_object_id, command.source_revision_id
            )
            parent_binding = bindings[0] if bindings else None
            if parent_binding is not None:
                parent_item = await uow.ingestion.resolve_item(  # type: ignore[attr-defined]
                    parent_binding.ingestion_run_source_id
                )
        else:
            parent_binding = await uow.ingestion.resolve_binding(parent_item.ingestion_run_source_id)  # type: ignore[union-attr,attr-defined]
        if parent_item is not None:
            parent_view = await uow.ingestion.derive_run_state(parent_item.ingestion_run_id)  # type: ignore[attr-defined]
            if parent_view is None or parent_view.state is IngestionRunState.CANCELLED:
                raise ProcessingCommandRejected()
        if (
            parent_binding is None
            or parent_binding.source_object_id != command.source_object_id
            or parent_binding.source_revision_id != command.source_revision_id
            or parent_binding.original_receipt_id is None
            or parent_binding.original_reference is None
            or parent_binding.original_receipt_digest != revision.original_sha256
        ):
            raise ProcessingCommandRejected()
        parent_receipt = await uow.ingestion.resolve_processing_receipt(parent_item.ingestion_run_source_id) if parent_item is not None else None  # type: ignore[union-attr,attr-defined]
        supplied_receipt_id = getattr(command, "parent_processing_receipt_id", None)
        if supplied_receipt_id is not None and (
            parent_receipt is None or parent_receipt.processing_receipt_id != supplied_receipt_id
        ):
            raise ProcessingCommandRejected()
        if trigger_kind == "retry" and parent_receipt is not None:
            assert isinstance(command, RetryProcessingCommand)
            if command.parent_processing_receipt_id != parent_receipt.processing_receipt_id:
                raise ProcessingCommandRejected()
        return {"revision": revision, "binding": parent_binding, "receipt": parent_receipt}

    async def _process(
        self,
        *,
        context: TrustedPersistenceContext,
        operation_id: UUID,
        run: IngestionRun,
        item: IngestionRunSource,
        binding: IngestionRunSourceBinding,
        revision: object,
        profile: str,
        command_key: str,
    ) -> None:
        if binding.original_reference is None:
            raise ProcessingCommandRejected()
        finalized_receipt = _FinalizedOriginalReceipt(
            object_reference=binding.original_reference,
        )
        trace_id = self._stable_id(operation_id, "process-trace")
        async with self.uow_factory(
            self.context_factory(PersistenceOperation("processing_receipt"), trace_id)
        ) as uow:
            processing = await CanonicalCurrentProcessingDispositionAuthority(
                uow.canonical
            ).resolve(
                workspace_id=run.workspace_id,
                environment_id=run.environment_id,
                source_object_id=revision.source_object_id,  # type: ignore[attr-defined]
                source_revision_id=revision.revision_id,  # type: ignore[attr-defined]
            )
            if not processing.eligible:
                await uow.rollback()
                raise ProcessingCommandRejected()
            head = await uow.ingestion.resolve_item_head(item.ingestion_run_source_id)
            if head is None:
                await uow.rollback()
                raise ProcessingCommandRejected()
            if head.state is IngestionItemState.PROCESSING_READY:
                await uow.ingestion.append_transition(
                    _item_transition(item, head, IngestionItemState.PROCESSING, self._now(), self.service_name, trace_id)
                )
            artifact: ParsedDocument | None = None
            failure: IngestionFailure | None = None
            warning: IngestionWarning | None = None
            from spine.application.document_ingestion import IngestionRequestPayload, UnsupportedDocumentFormat

            try:
                content = await _read_bounded(
                    self.validation_reader.read_finalized(
                        workspace_id=run.workspace_id,
                        environment_id=run.environment_id,
                        source_revision_id=revision.revision_id,  # type: ignore[attr-defined]
                        receipt=finalized_receipt,
                        max_bytes=self.max_parse_bytes,
                    ),
                    self.max_parse_bytes,
                )
                artifact = self.parser(
                    revision, content, media_type=revision.media_type  # type: ignore[attr-defined]
                )
            except Exception as error:
                failure = self._failure(
                    item,
                    "parser_failed",
                    not isinstance(error, UnsupportedDocumentFormat),
                )
            if artifact is not None and artifact.coverage is not ParseCoverage.FAILED:
                await self.parsed_artifacts.persist(  # type: ignore[attr-defined]
                    ParsedArtifactCommand.create(
                        artifact, command_identity=_bounded_identifier(f"{run.command_identity}.artifact")
                    )
                )
                if artifact.coverage is ParseCoverage.PARTIAL:
                    warning = self._warning(item)
            if artifact is not None and artifact.coverage is ParseCoverage.FAILED:
                failure = self._failure(item, "parser_failed", True)
            receipt = _build_receipt(
                operation_id=operation_id,
                run=run,
                item=item,
                binding=binding,
                revision=revision,
                artifact=artifact,
                failure=failure,
                warning=warning,
                profile=profile,
                completed_at=self._now(),
            )
            if warning is not None:
                await uow.ingestion.append_warning(warning)
            if failure is not None:
                await uow.ingestion.append_failure(failure)
            stored = await uow.ingestion.record_processing_receipt(
                ProcessingReceiptCommand.create(
                    receipt=receipt,
                    idempotency_key=IdempotencyKey(_child_key(command_key, "processing-receipt")),
                )
            )
            target = (
                IngestionItemState.PROCESSED
                if artifact is not None and receipt.coverage is not ProcessingCoverage.FAILED
                else IngestionItemState.PROCESSING_FAILED
            )
            latest = await uow.ingestion.resolve_item_head(item.ingestion_run_source_id)
            if latest is not None and latest.state is not target:
                await uow.ingestion.append_transition(
                    _item_transition(item, latest, target, self._now(), self.service_name, trace_id)
                )
            if artifact is not None and receipt.coverage is not ProcessingCoverage.FAILED:
                payload = IngestionRequestPayload(
                    ingestion_run_source_id=item.ingestion_run_source_id,
                    binding_id=binding.binding_id,
                    source_object_id=receipt.source_object_id,
                    source_revision_id=receipt.source_revision_id,
                    processing_receipt_id=receipt.processing_receipt_id,
                    artifact_id=artifact.artifact_id,
                    artifact_digest=artifact.artifact_digest,
                )
                await uow.outbox.append(
                    self.outbox_events.build_intent(
                        workspace_id=run.workspace_id,
                        environment_id=run.environment_id,
                        event_type=PROCESSING_REQUEST_EVENT_TYPE,
                        schema_version=PROCESSING_REQUEST_SCHEMA_VERSION,
                        payload=payload,
                        trace_id=trace_id,
                        aggregate=OpaqueObjectReference(
                            object_type="ingestion_run_source",
                            object_id=item.ingestion_run_source_id,
                            schema_version=1,
                        ),
                        producer_deduplication_id=f"process-dispatch:{operation_id}",
                        event_id=self._stable_id(operation_id, "dispatch"),
                    )
                )
            await uow.commit()

    async def _result_from_lineage(self, uow: object, lineage: ProcessOnlyOperationLineage, replay: bool) -> ProcessingCommandResult:
        view = await uow.ingestion.derive_run_state(lineage.ingestion_run_id)  # type: ignore[attr-defined]
        item_head = await uow.ingestion.resolve_item_head(lineage.ingestion_run_source_id)  # type: ignore[attr-defined]
        receipt = await uow.ingestion.resolve_processing_receipt(lineage.ingestion_run_source_id)  # type: ignore[attr-defined]
        failures = await uow.ingestion.resolve_failures(lineage.ingestion_run_source_id)  # type: ignore[attr-defined]
        status = _status(view.state if view else IngestionRunState.FAILED, item_head.state if item_head else None, receipt)
        return ProcessingCommandResult(
            operation_id=lineage.operation_id,
            run_id=lineage.ingestion_run_id,
            item_id=lineage.ingestion_run_source_id,
            source_object_id=lineage.source_object_id,
            source_revision_id=lineage.source_revision_id,
            trigger_kind=lineage.trigger_kind,
            status=status,
            replay=replay,
            processing_receipt_id=receipt.processing_receipt_id if receipt else None,
            failure_ids=tuple(failure.ingestion_failure_id for failure in failures),
            next_action=(
                "poll"
                if status in {
                    ProcessingCommandStatus.ACCEPTED,
                    ProcessingCommandStatus.READY,
                    ProcessingCommandStatus.PROCESSING,
                }
                else "retry"
                if status is ProcessingCommandStatus.FAILED
                and any(failure.retryable for failure in failures)
                else "review"
                if status is ProcessingCommandStatus.FAILED
                else "none"
            ),
        )

    def _warning(self, item: IngestionRunSource) -> IngestionWarning:
        return IngestionWarning(
            ingestion_warning_id=self._stable_id(item.ingestion_run_source_id, "partial"),
            workspace_id=item.workspace_id,
            environment_id=item.environment_id,
            ingestion_run_source_id=item.ingestion_run_source_id,
            attempt=1,
            stage=WarningStage.PARSING,
            code="partial_coverage",
            safe_detail=SafeDiagnosticDetail(schema_version="ingestion-safe-detail:v1", item_count=1),
            occurred_at=self._now(),
        )

    def _failure(self, item: IngestionRunSource, code: str, retryable: bool) -> IngestionFailure:
        return IngestionFailure(
            ingestion_failure_id=self._stable_id(item.ingestion_run_source_id, code),
            workspace_id=item.workspace_id,
            environment_id=item.environment_id,
            ingestion_run_source_id=item.ingestion_run_source_id,
            attempt=1,
            stage=FailureStage.PARSING,
            code=code,  # type: ignore[arg-type]
            retryable=retryable,
            safe_detail=SafeDiagnosticDetail(schema_version="ingestion-safe-detail:v1", retryable=retryable),
            occurred_at=self._now(),
        )

    @staticmethod
    def _assert_context_scope(context: TrustedPersistenceContext) -> None:
        if not isinstance(context.scope, EnvironmentScope):
            raise ProcessingCommandRejected()

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("processing command clock must be timezone-aware")
        return value

    @staticmethod
    def _stable_id(value: object, suffix: str) -> UUID:
        return uuid5(NAMESPACE_URL, f"spine:processing-command:v1:{value}:{suffix}")


async def _append_run_transition(uow: object, run: IngestionRun, state: IngestionRunState, occurred_at: datetime, actor: str) -> None:
    head = await uow.ingestion.resolve_run_head(run.ingestion_run_id)  # type: ignore[attr-defined]
    if head is None or head.state is state:
        return
    await uow.ingestion.append_transition(  # type: ignore[attr-defined]
        IngestionTransition(
            ingestion_transition_id=uuid5(NAMESPACE_URL, f"spine:processing-command:run:{run.ingestion_run_id}:{state.value}"),
            workspace_id=run.workspace_id,
            environment_id=run.environment_id,
            target_kind=IngestionTargetKind.RUN,
            ingestion_run_id=run.ingestion_run_id,
            predecessor_transition_id=head.ingestion_transition_id,
            state=state,
            attempt=head.attempt + 1,
            actor_or_service=actor,
            trace_id=uuid5(NAMESPACE_URL, f"spine:processing-command:trace:{run.ingestion_run_id}"),
            occurred_at=occurred_at,
        )
    )


def _item_transition(item: IngestionRunSource, head: IngestionTransition, state: IngestionItemState, occurred_at: datetime, actor: str, trace_id: UUID) -> IngestionTransition:
    return IngestionTransition(
        ingestion_transition_id=uuid5(NAMESPACE_URL, f"spine:processing-command:item:{item.ingestion_run_source_id}:{state.value}:{head.attempt + 1}"),
        workspace_id=item.workspace_id,
        environment_id=item.environment_id,
        target_kind=IngestionTargetKind.ITEM,
        ingestion_run_id=item.ingestion_run_id,
        ingestion_run_source_id=item.ingestion_run_source_id,
        predecessor_transition_id=head.ingestion_transition_id,
        state=state,
        attempt=head.attempt + 1,
        actor_or_service=actor,
        trace_id=trace_id,
        occurred_at=occurred_at,
    )


def _build_receipt(*, operation_id: UUID, run: IngestionRun, item: IngestionRunSource, binding: IngestionRunSourceBinding, revision: object, artifact: ParsedDocument | None, failure: IngestionFailure | None, warning: IngestionWarning | None, profile: str, completed_at: datetime) -> ProcessingReceipt:
    from spine.application.document_ingestion import _parser_metadata

    status = ProcessingCoverage.COMPLETE if artifact is not None and artifact.coverage is ParseCoverage.COMPLETE else ProcessingCoverage.PARTIAL if artifact is not None else ProcessingCoverage.FAILED
    parser_name, parser_version, parser_config, locator_version = _parser_metadata(revision.media_type, artifact)  # type: ignore[attr-defined]
    exclusion_codes = {
        "invalid_utf8": "unreadable_content",
        "unsupported_structure": "unsupported_format",
        "malformed_structure": "invalid_locator",
        "bounded_input": "policy_excluded",
        "blank_page": "invalid_locator",
        "malformed_document": "invalid_locator",
        "extraction_failed": "unreadable_content",
        "unlocatable_content": "unreadable_content",
        "unsupported_story": "unsupported_format",
        "unsupported_drawing": "unsupported_format",
        "ambiguous_merged_cell": "invalid_locator",
        "formula_without_cached_value": "unsupported_format",
        "hidden_content": "unsupported_format",
        "external_link": "unsupported_format",
    }
    exclusions = () if artifact is None else tuple(
        ProcessingExclusion(
            schema_version="ingestion-exclusion:v1",
            code=exclusion_codes[exclusion.code],
            count=exclusion.count,
        )
        for exclusion in artifact.exclusions
    )
    output_artifact = artifact if artifact is not None and status is not ProcessingCoverage.FAILED else None
    return ProcessingReceipt(
        processing_receipt_id=uuid5(NAMESPACE_URL, f"spine:processing-command:receipt:{operation_id}"),
        workspace_id=run.workspace_id,
        environment_id=run.environment_id,
        ingestion_run_source_id=item.ingestion_run_source_id,
        binding_id=binding.binding_id,
        source_object_id=revision.source_object_id,  # type: ignore[attr-defined]
        source_revision_id=revision.revision_id,  # type: ignore[attr-defined]
        processor_kind="document-ingestion",
        parser_name=parser_name,
        parser_version=parser_version,
        parser_config_reference=parser_config,
        locator_contract_version=locator_version,
        original_sha256=revision.original_sha256 or "0" * 64,  # type: ignore[attr-defined]
        output_kind="parsed_document" if output_artifact is not None else None,
        output_reference=output_artifact.artifact_id if output_artifact is not None else None,
        output_digest=output_artifact.artifact_digest if output_artifact is not None else None,
        coverage=status,
        exclusions=exclusions,
        warning_ids=(warning.ingestion_warning_id,) if warning else (),
        failure_ids=(failure.ingestion_failure_id,) if failure else (),
        command_identity=_bounded_identifier(f"{run.command_identity}.receipt"),
        command_digest="0" * 64,
        completed_at=completed_at,
    )


def _status(run_state: IngestionRunState, item_state: IngestionItemState | None, receipt: ProcessingReceipt | None) -> ProcessingCommandStatus:
    if item_state is IngestionItemState.PROCESSING_READY:
        return ProcessingCommandStatus.READY
    if item_state is IngestionItemState.PROCESSING:
        return ProcessingCommandStatus.PROCESSING
    if item_state is IngestionItemState.PROCESSING_FAILED or run_state is IngestionRunState.FAILED:
        return ProcessingCommandStatus.FAILED
    if receipt is not None and receipt.coverage is ProcessingCoverage.PARTIAL:
        return ProcessingCommandStatus.PARTIAL
    if run_state is IngestionRunState.CANCELLED:
        return ProcessingCommandStatus.CANCELLED
    if item_state in {IngestionItemState.PROCESSED, IngestionItemState.PROJECTION_REQUESTED}:
        return ProcessingCommandStatus.ACCEPTED
    return ProcessingCommandStatus.ACCEPTED


def _command_digest(command: _ProcessingCommand, trigger_kind: str) -> str:
    payload = command.model_dump(mode="json", exclude={"idempotency_key"})
    payload["trigger_kind"] = trigger_kind
    payload["idempotency_key"] = command.idempotency_key
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _replayed_processing_result(result: ProcessingCommandResult) -> ProcessingCommandResult:
    return ProcessingCommandResult(
        operation_id=result.operation_id,
        run_id=result.run_id,
        item_id=result.item_id,
        source_object_id=result.source_object_id,
        source_revision_id=result.source_revision_id,
        trigger_kind=result.trigger_kind,
        status=result.status,
        replay=True,
        processing_receipt_id=result.processing_receipt_id,
        failure_ids=result.failure_ids,
        next_action=result.next_action,
    )


def _bounded_identifier(value: str) -> str:
    if len(value) <= 64:
        return value
    return f"{value[:31]}-{hashlib.sha256(value.encode()).hexdigest()[:32]}"


def _child_key(parent: str, suffix: str) -> str:
    return _bounded_identifier(f"{parent}:{suffix}")


async def _read_bounded(stream: AsyncIterable[bytes], maximum: int) -> bytes:
    parts: list[bytes] = []
    length = 0
    async for part in stream:
        if not isinstance(part, bytes):
            raise ValueError("original read returned a non-byte chunk")
        length += len(part)
        if length > maximum:
            raise ValueError("original content exceeds parser limit")
        parts.append(part)
    return b"".join(parts)


__all__ = [
    "ALLOWED_PROCESSING_PROFILES",
    "DEFAULT_PROCESSING_PROFILE",
    "OrchestratedProcessingCommandService",
    "ProcessingCommandRejected",
    "ProcessingCommandResult",
    "ProcessingCommandService",
    "ProcessingCommandStatus",
    "ReprocessProcessingCommand",
    "ReplacementProcessingCommand",
    "RetryProcessingCommand",
]
