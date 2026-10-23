"""The single-document v1 ingestion use case.

This module is deliberately an application seam.  It composes the existing
storage, source-observation, admission, canonical-acceptance, parser, artifact
and ingestion-ledger ports without making any of those adapters authoritative
over another one.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import PurePath
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field

from spine.application.admission import AdmissionDecisionService, AdmissionValidationEvidence
from spine.application.ingestion import (
    ContentValidationReader,
    ContentValidator,
    ValidatedContentEvidence,
    ValidationOutcome,
    _assert_evidence_matches_receipt,
)
from spine.application.ingestion_faults import (
    IngestionFaultBoundary,
    IngestionFaultInjected,
    IngestionFaultInjector,
)
from spine.application.persistence.canonical import CanonicalAcceptanceCommand
from spine.application.persistence.context import (
    PersistenceOperation,
    TrustedPersistenceContext,
)
from spine.application.persistence.ingestion import (
    IngestionBindingCommand,
    IngestionRunCommand,
    ProcessingReceiptCommand,
)
from spine.application.persistence.parsed_artifacts import (
    ParsedArtifactCommand,
    ParsedArtifactRepository,
)
from spine.application.persistence.repositories import SourceObservationCommand
from spine.application.persistence.unit_of_work import UnitOfWorkFactory
from spine.application.storage import OriginalUploadOutcome
from spine.domain.artifacts import ParseCoverage, ParsedDocument
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
    ProcessingCoverage,
    ProcessingExclusion,
    ProcessingReceipt,
    SafeDiagnosticDetail,
    WarningStage,
)
from spine.domain.sources import (
    AdmissionDecision,
    AdmissionDisposition,
    AdmissionReference,
    IdentityMode,
    RevisionMetadata,
    SourceObject,
    SourceRevision,
    SourceRevisionProvenance,
    SourceRevisionReceiptAssociation,
    content_revision,
)
from spine.infrastructure.object_storage.contracts import OriginalUploadCommandInput
from spine.application.persistence.outbox import (
    OpaqueObjectReference,
    OutboxEventRegistry,
    OutboxIntent,
)
from spine.ingestion.parsers import DOCX_MEDIA_TYPE, XLSX_MEDIA_TYPE


PROCESSING_REQUEST_EVENT_TYPE = "ingestion.processing.requested"
PROCESSING_REQUEST_SCHEMA_VERSION = 1
PROCESSING_REQUEST_PAYLOAD_SCHEMA = "ingestion-processing-request:v1"
PROJECTION_REQUEST_EVENT_TYPE = "ingestion.projection.requested"
PROJECTION_REQUEST_SCHEMA_VERSION = 1
PROJECTION_REQUEST_PAYLOAD_SCHEMA = "ingestion-projection-request:v1"


class DocumentIngestionStatus(str, Enum):
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    REJECTED = "rejected"
    QUARANTINED = "quarantined"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"


class UnsupportedDocumentFormat(ValueError):
    """The admission path accepted bytes for which no parser is installed."""


class IngestionRequestPayload(BaseModel):
    """Content-free durable handoff for a completed parse."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = Field(default=PROCESSING_REQUEST_PAYLOAD_SCHEMA)
    ingestion_run_source_id: UUID
    binding_id: UUID
    source_object_id: UUID
    source_revision_id: UUID
    processing_receipt_id: UUID
    artifact_id: UUID
    artifact_digest: str = Field(pattern=r"[0-9a-f]{64}")


class ProjectionRequestPayload(BaseModel):
    """Content-free handoff for a future projection worker."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = Field(default=PROJECTION_REQUEST_PAYLOAD_SCHEMA)
    ingestion_run_id: UUID
    ingestion_run_source_id: UUID
    source_revision_id: UUID
    processing_receipt_id: UUID
    processor_kind: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9_.:-]{0,127}$")
    parser_version: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9_.:-]{0,127}$")
    parser_config_reference: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9_.:-]{0,127}$")
    locator_contract_version: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9_.:-]{0,127}$")


def build_projection_request_intent(
    *,
    registry: OutboxEventRegistry,
    run: IngestionRun,
    item: IngestionRunSource,
    receipt: ProcessingReceipt,
    trace_id: UUID,
    causation_id: UUID,
) -> OutboxIntent:
    """Build one stable projection request from committed ingestion identities."""

    if (
        item.ingestion_run_id != run.ingestion_run_id
        or receipt.ingestion_run_source_id != item.ingestion_run_source_id
        or (item.workspace_id, item.environment_id)
        != (run.workspace_id, run.environment_id)
        or (receipt.workspace_id, receipt.environment_id)
        != (run.workspace_id, run.environment_id)
    ):
        raise ValueError("projection request ingestion scope or lineage is invalid")
    event_id = uuid5(
        NAMESPACE_URL,
        "|".join(
            (
                "spine:ingestion-projection-request:v1",
                str(run.workspace_id),
                str(run.environment_id),
                str(item.ingestion_run_source_id),
                receipt.original_sha256,
            )
        ),
    )
    return registry.build_intent(
        event_id=event_id,
        workspace_id=run.workspace_id,
        environment_id=run.environment_id,
        event_type=PROJECTION_REQUEST_EVENT_TYPE,
        schema_version=PROJECTION_REQUEST_SCHEMA_VERSION,
        payload=ProjectionRequestPayload(
            ingestion_run_id=run.ingestion_run_id,
            ingestion_run_source_id=item.ingestion_run_source_id,
            source_revision_id=receipt.source_revision_id,
            processing_receipt_id=receipt.processing_receipt_id,
            processor_kind=receipt.processor_kind,
            parser_version=receipt.parser_version,
            parser_config_reference=receipt.parser_config_reference,
            locator_contract_version=receipt.locator_contract_version,
        ),
        trace_id=trace_id,
        correlation_id=run.ingestion_run_id,
        causation_id=causation_id,
        aggregate=OpaqueObjectReference(
            object_type="ingestion_run_source",
            object_id=item.ingestion_run_source_id,
            schema_version=1,
        ),
        producer_deduplication_id=(
            f"projection-request:{item.ingestion_run_source_id}"
        ),
    )


class DocumentUploadCommand(BaseModel):
    """Trusted semantic command for one upload item.

    ``upload_identity`` is the stable logical source identity.  A new
    idempotency key with changed bytes therefore creates a new immutable
    revision without creating a second SourceObject.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_id: UUID
    environment_id: UUID
    command_identity: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_.:-]{0,63}$")
    idempotency_key: str = Field(min_length=1, max_length=255)
    upload_identity: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_.:-]{0,63}$")
    purpose: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_.:-]{0,63}$")
    declared_media_type: str | None = Field(default=None, min_length=1, max_length=255)
    declared_filename: str | None = Field(default=None, min_length=1, max_length=255)
    expected_sha256: str | None = Field(default=None, pattern=r"[0-9a-f]{64}")
    expected_byte_length: int | None = Field(default=None, ge=0)
    expected_current_revision_id: UUID | None = None
    access_policy_version_id: UUID | None = None
    source_metadata: RevisionMetadata = RevisionMetadata()


class CanonicalAcceptanceCommandFactory(Protocol):
    def __call__(
        self,
        *,
        request: DocumentUploadCommand,
        source: SourceObject,
        revision: SourceRevision,
        provenance: SourceRevisionProvenance,
        admission: AdmissionDecision,
        context: TrustedPersistenceContext,
        accepted_at: datetime,
    ) -> CanonicalAcceptanceCommand: ...


class InitialAccessPolicyEnsurer(Protocol):
    """Ensure the first current policy before canonical content acceptance."""

    async def ensure(
        self,
        *,
        request: DocumentUploadCommand,
        source: SourceObject,
        context: TrustedPersistenceContext,
        activated_at: datetime,
    ) -> UUID: ...


class DocumentParser(Protocol):
    def __call__(
        self, revision: SourceRevision, content: bytes, *, media_type: str
    ) -> ParsedDocument: ...


@dataclass(frozen=True, slots=True)
class AuthorizedOriginalContentReader:
    """Adapt the policy-gated original-read gateway to the ingestion port."""

    gateway: object
    purpose: str = "document.processing.v1"

    def read_finalized(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        source_revision_id: UUID,
        receipt: object,
        max_bytes: int,
    ) -> AsyncIterable[bytes]:
        del receipt
        reader = getattr(self.gateway, "read_original", None)
        if not callable(reader):
            raise TypeError("authorized original-read gateway is unavailable")
        return reader(
            workspace_id=workspace_id,
            environment_id=environment_id,
            source_revision_id=source_revision_id,
            purpose=self.purpose,
            max_bytes=max_bytes,
        )


@dataclass(frozen=True, slots=True)
class DocumentIngestionResult:
    run: IngestionRun
    item: IngestionRunSource
    source: SourceObject
    revision: SourceRevision
    provenance: SourceRevisionProvenance
    binding: IngestionRunSourceBinding
    validation: ValidatedContentEvidence
    admission: AdmissionDecision
    status: DocumentIngestionStatus
    artifact: ParsedDocument | None = None
    processing_receipt: ProcessingReceipt | None = None
    replay: bool = False
    failure_ids: tuple[UUID, ...] = ()


class _UploadService(Protocol):
    async def upload(
        self,
        context: TrustedPersistenceContext,
        command: OriginalUploadCommandInput,
        chunks: AsyncIterable[bytes],
    ) -> OriginalUploadOutcome: ...


@dataclass(frozen=True, slots=True)
class DocumentIngestionOrchestrator:
    """Coordinate one upload through the durable v1 application path."""

    uow_factory: UnitOfWorkFactory
    uploader: _UploadService
    validation_reader: ContentValidationReader
    validator: ContentValidator
    parsed_artifacts: ParsedArtifactRepository
    context_factory: Callable[[PersistenceOperation, UUID], TrustedPersistenceContext]
    access_policy_ensurer: InitialAccessPolicyEnsurer
    canonical_command_factory: CanonicalAcceptanceCommandFactory
    outbox_events: OutboxEventRegistry
    parser: DocumentParser
    fault_injector: IngestionFaultInjector | None = None
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    max_validation_bytes: int = 10_000_000
    max_parse_bytes: int = 10_000_000
    service_name: str = "document-ingestion"

    def __post_init__(self) -> None:
        if self.max_validation_bytes <= 0 or self.max_parse_bytes <= 0:
            raise ValueError("ingestion byte limits must be positive")
        self.outbox_events.register(
            event_type=PROCESSING_REQUEST_EVENT_TYPE,
            schema_version=PROCESSING_REQUEST_SCHEMA_VERSION,
            payload_type=IngestionRequestPayload,
        )
        self.outbox_events.register(
            event_type=PROJECTION_REQUEST_EVENT_TYPE,
            schema_version=PROJECTION_REQUEST_SCHEMA_VERSION,
            payload_type=ProjectionRequestPayload,
        )

    async def ingest(
        self,
        *,
        request: DocumentUploadCommand,
        chunks: AsyncIterable[bytes],
    ) -> DocumentIngestionResult:
        """Run one item; every durable phase is safe to repeat after failure."""

        self._validate_request_scope(request)
        command_digest = _request_digest(request)
        run, item, replay = await self._ensure_run(request, command_digest)
        return await self.ingest_item(
            request=request,
            chunks=chunks,
            run=run,
            item=item,
            replay=replay,
        )

    async def ingest_item(
        self,
        *,
        request: DocumentUploadCommand,
        chunks: AsyncIterable[bytes],
        run: IngestionRun,
        item: IngestionRunSource,
        replay: bool = False,
    ) -> DocumentIngestionResult:
        """Process an already-created item inside a caller-owned ingestion run."""

        self._validate_request_scope(request)
        if (
            item.ingestion_run_id != run.ingestion_run_id
            or (item.workspace_id, item.environment_id)
            != (run.workspace_id, run.environment_id)
            or (request.workspace_id, request.environment_id)
            != (run.workspace_id, run.environment_id)
        ):
            raise ValueError("ingestion item scope does not match its run")
        upload = await self.uploader.upload(
            self.context_factory(PersistenceOperation("original.upload"), _id(request, "upload-trace")),
            OriginalUploadCommandInput(
                operation_name="original.upload",
                operation_schema_version=1,
                idempotency_key=request.idempotency_key,
                workspace_id=request.workspace_id,
                environment_id=request.environment_id,
                expected_sha256=request.expected_sha256,
                expected_byte_length=request.expected_byte_length,
                declared_media_type=request.declared_media_type,
                declared_filename=request.declared_filename,
                purpose=request.purpose,
            ),
            chunks,
        )
        source, revision, provenance, binding, observation = self._build_observation(
            request, run, item, upload
        )
        await self._bind_source(request, run, item, binding, observation)
        validation = await self._validate(request, revision, upload)
        admission = await self._record_admission(request, item, revision, provenance, validation)

        terminal_replay = await self._replay_if_terminal(
            request=request, run=run, item=item, source=source, revision=revision,
            provenance=provenance, binding=binding, validation=validation,
            admission=admission, replay=replay,
        )
        if terminal_replay is not None:
            return terminal_replay

        if validation.outcome is not ValidationOutcome.VALID:
            status = (
                DocumentIngestionStatus.QUARANTINED
                if validation.outcome is ValidationOutcome.QUARANTINED
                else DocumentIngestionStatus.REJECTED
            )
            await self._finish_admission_only(
                request=request,
                run=run,
                item=item,
                state=(
                    IngestionItemState.QUARANTINED
                    if status is DocumentIngestionStatus.QUARANTINED
                    else IngestionItemState.REJECTED
                ),
            )
            return DocumentIngestionResult(
                run=run,
                item=item,
                source=source,
                revision=revision,
                provenance=provenance,
                binding=binding,
                validation=validation,
                admission=admission,
                status=status,
                replay=replay,
            )

        policy_context = self.context_factory(
            PersistenceOperation("access_policy_activation"),
            _id(request, "access-policy-trace"),
        )
        policy_id = await self.access_policy_ensurer.ensure(
            request=request,
            source=source,
            context=policy_context,
            activated_at=self._now(),
        )
        governed_request = request.model_copy(
            update={"access_policy_version_id": policy_id}
        )
        try:
            await self._inject_qualification_fault(
                request=request,
                item=item,
                boundary=IngestionFaultBoundary.PRE_ACCEPTANCE,
            )
            await self._accept_canonical(
                governed_request, source, revision, provenance, admission
            )
        except IngestionFaultInjected as error:
            failure_id = await self.record_item_failure(
                request=request,
                item=item,
                retryable=True,
                code=error.reason_code.value,
            )
            return DocumentIngestionResult(
                run=run,
                item=item,
                source=source,
                revision=revision,
                provenance=provenance,
                binding=binding,
                validation=validation,
                admission=admission,
                status=DocumentIngestionStatus.FAILED,
                replay=replay,
                failure_ids=(failure_id,) if failure_id is not None else (),
            )
        await self._advance_to_processing(request, run, item)
        return await self._process(
            request=request,
            run=run,
            item=item,
            source=source,
            revision=revision,
            provenance=provenance,
            binding=binding,
            validation=validation,
            admission=admission,
            upload=upload,
            replay=replay,
        )

    async def record_item_failure(
        self,
        *,
        request: DocumentUploadCommand,
        item: IngestionRunSource,
        retryable: bool = True,
        code: str = "persistence_failed",
    ) -> UUID | None:
        """Persist a safe batch-boundary failure after an item exception."""

        context = self.context_factory(
            PersistenceOperation("ingestion_run"), _id(request, "batch-failure-trace")
        )
        async with self.uow_factory(context) as uow:
            head = await uow.ingestion.resolve_item_head(item.ingestion_run_source_id)
            if head is None:
                await uow.rollback()
                return None
            failure = self._failure(
                item,
                code=code,
                stage=FailureStage.PERSISTING,
                retryable=retryable,
                attempt=head.attempt,
            )
            await uow.ingestion.append_failure(failure)
            if head.state not in {
                IngestionItemState.PROCESSED,
                IngestionItemState.PROJECTION_REQUESTED,
                IngestionItemState.REJECTED,
                IngestionItemState.QUARANTINED,
                IngestionItemState.PROCESSING_FAILED,
            }:
                await self._advance_item_in_uow(
                    uow, item, IngestionItemState.PROCESSING_FAILED, self._now()
                )
            await uow.commit()
            return failure.ingestion_failure_id

    async def _ensure_run(
        self, request: DocumentUploadCommand, command_digest: str
    ) -> tuple[IngestionRun, IngestionRunSource, bool]:
        run_id = _id(request, "run")
        item_id = _id(request, "item")
        now = self._now()
        run = IngestionRun(
            ingestion_run_id=run_id,
            workspace_id=request.workspace_id,
            environment_id=request.environment_id,
            command_identity=request.command_identity,
            command_digest="0" * 64,
            trigger_kind="upload",
            requested_scope_schema="ingestion-scope:v1",
            requested_scope_digest=command_digest,
            requested_at=now,
        )
        item = IngestionRunSource(
            ingestion_run_source_id=item_id,
            ingestion_run_id=run_id,
            workspace_id=request.workspace_id,
            environment_id=request.environment_id,
            input_ordinal=1,
            input_identity=request.upload_identity,
            input_digest=command_digest,
            command_identity=request.command_identity,
            command_digest=command_digest,
            created_at=now,
        )
        context = self.context_factory(PersistenceOperation("ingestion_run"), _id(request, "run-trace"))
        async with self.uow_factory(context) as uow:
            result = await uow.ingestion.create_run(
                IngestionRunCommand.create(run=run, idempotency_key=_key(request, "run"))
            )
            await uow.ingestion.create_item(item)
            run_head = await uow.ingestion.resolve_run_head(run_id)
            if run_head is not None and run_head.state is IngestionRunState.REQUESTED:
                await self._advance_run_in_uow(uow, result.value, IngestionRunState.RECEIVING, now)
            await uow.commit()
        return result.value, item, result.replay  # type: ignore[return-value]

    async def _bind_source(
        self,
        request: DocumentUploadCommand,
        run: IngestionRun,
        item: IngestionRunSource,
        binding: IngestionRunSourceBinding,
        observation: SourceObservationCommand,
    ) -> None:
        context = self.context_factory(PersistenceOperation("ingestion_binding"), _id(request, "binding-trace"))
        async with self.uow_factory(context) as uow:
            head = await uow.ingestion.resolve_item_head(item.ingestion_run_source_id)
            if head is not None and head.state is IngestionItemState.RECEIVED:
                await self._advance_item_in_uow(uow, item, IngestionItemState.STORED, self._now())
            command = IngestionBindingCommand.create(
                item=item,
                binding=binding,
                observation=observation,
                idempotency_key=_key(request, "binding"),
            )
            await uow.ingestion.bind_source(command)
            await uow.commit()

    async def _validate(
        self,
        request: DocumentUploadCommand,
        revision: SourceRevision,
        upload: OriginalUploadOutcome,
    ) -> ValidatedContentEvidence:
        if upload.write_receipt.object_reference.byte_length > self.max_validation_bytes:
            evidence = ValidatedContentEvidence(
                producer="content-validator:r1",
                content_class="unknown",
                outcome=ValidationOutcome.QUARANTINED,
                reason_codes=("validation_limit_exceeded",),
                observed_sha256=upload.write_receipt.observed_content.observed_sha256,
                observed_byte_length=upload.write_receipt.observed_content.observed_byte_length,
            )
        else:
            content = self.validation_reader.read_finalized(
                workspace_id=request.workspace_id,
                environment_id=request.environment_id,
                source_revision_id=revision.revision_id,
                receipt=upload.write_receipt,
                max_bytes=self.max_validation_bytes,
            )
            evidence = await self.validator.validate(
                content,
                declared_filename=request.declared_filename,
                declared_media_type=revision.media_type,
            )
        _assert_evidence_matches_receipt(evidence, upload.write_receipt)
        return evidence

    async def _record_admission(
        self,
        request: DocumentUploadCommand,
        item: IngestionRunSource,
        revision: SourceRevision,
        provenance: SourceRevisionProvenance,
        validation: ValidatedContentEvidence,
    ) -> AdmissionDecision:
        context = self.context_factory(PersistenceOperation("ingestion_admission"), _id(request, "admission-trace"))
        async with self.uow_factory(context) as uow:
            head = await uow.ingestion.resolve_item_head(item.ingestion_run_source_id)
            if head is not None and head.state is IngestionItemState.OBSERVED:
                await self._advance_item_in_uow(uow, item, IngestionItemState.ADMISSION_PENDING, self._now())
            existing = await uow.admissions.resolve_effective(revision.revision_id)
            if existing is not None:
                await uow.commit()
                return existing
            service = AdmissionDecisionService(
                uow.admissions,
                id_factory=lambda: _id(request, "admission-decision"),
            )
            decision = await service.record(
                revision=revision,
                provenance=provenance,
                evidence=AdmissionValidationEvidence(
                    disposition=_disposition(validation.outcome),
                    validator_profile_version=validation.producer,
                    reason_schema_version=validation.reason_schema_version,
                    reason_codes=validation.reason_codes,
                    authority_reference=AdmissionReference(
                        reference_type="content-validator",
                        reference_id=_id(request, "validator-authority"),
                        schema_version=1,
                    ),
                    trace_id=context.trace_id,
                    audit_event_id=_id(request, "admission-audit"),
                    decided_at=self._now(),
                ),
            )
            await uow.commit()
            return decision

    async def _finish_admission_only(
        self,
        *,
        request: DocumentUploadCommand,
        run: IngestionRun,
        item: IngestionRunSource,
        state: IngestionItemState,
    ) -> None:
        context = self.context_factory(PersistenceOperation("ingestion_run"), _id(request, "admission-finish-trace"))
        async with self.uow_factory(context) as uow:
            await self._advance_run_in_uow(uow, run, IngestionRunState.PROCESSING, self._now())
            await self._advance_item_in_uow(uow, item, state, self._now())
            await uow.commit()

    async def _accept_canonical(
        self,
        request: DocumentUploadCommand,
        source: SourceObject,
        revision: SourceRevision,
        provenance: SourceRevisionProvenance,
        admission: AdmissionDecision,
    ) -> None:
        context = self.context_factory(PersistenceOperation("canonical_acceptance"), _id(request, "canonical-trace"))
        command = self.canonical_command_factory(
            request=request,
            source=source,
            revision=revision,
            provenance=provenance,
            admission=admission,
            context=context,
            accepted_at=self._now(),
        )
        async with self.uow_factory(context) as uow:
            await uow.canonical.accept(command)
            await uow.commit()

    async def _advance_to_processing(
        self, request: DocumentUploadCommand, run: IngestionRun, item: IngestionRunSource
    ) -> None:
        context = self.context_factory(PersistenceOperation("ingestion_run"), _id(request, "processing-trace"))
        async with self.uow_factory(context) as uow:
            await self._advance_run_in_uow(uow, run, IngestionRunState.PROCESSING, self._now())
            head = await uow.ingestion.resolve_item_head(item.ingestion_run_source_id)
            if head is not None and head.state is IngestionItemState.ADMISSION_PENDING:
                await self._advance_item_in_uow(uow, item, IngestionItemState.ACCEPTED, self._now())
                await self._advance_item_in_uow(uow, item, IngestionItemState.PROCESSING, self._now())
            elif head is not None and head.state is IngestionItemState.ACCEPTED:
                await self._advance_item_in_uow(uow, item, IngestionItemState.PROCESSING, self._now())
            elif head is not None and head.state is IngestionItemState.PROCESSING_FAILED:
                await self._advance_item_in_uow(uow, item, IngestionItemState.PROCESSING, self._now())
            await uow.commit()

    async def _replay_if_terminal(self, **values: object) -> DocumentIngestionResult | None:
        item = values["item"]
        request = values["request"]
        assert isinstance(item, IngestionRunSource)
        assert isinstance(request, DocumentUploadCommand)
        context = self.context_factory(PersistenceOperation("processing_receipt"), _id(request, "replay-trace"))
        async with self.uow_factory(context) as uow:
            head = await uow.ingestion.resolve_item_head(item.ingestion_run_source_id)
            if head is None or head.state not in {
                IngestionItemState.PROCESSED,
                IngestionItemState.PROJECTION_REQUESTED,
                IngestionItemState.PROCESSING_FAILED,
                IngestionItemState.REJECTED,
                IngestionItemState.QUARANTINED,
            }:
                await uow.rollback()
                return None
            stored_receipt = await uow.ingestion.resolve_processing_receipt(item.ingestion_run_source_id)
            await uow.commit()
        if stored_receipt is None:
            if head.state not in {IngestionItemState.REJECTED, IngestionItemState.QUARANTINED}:
                return None
            status = (
                DocumentIngestionStatus.REJECTED
                if head.state is IngestionItemState.REJECTED
                else DocumentIngestionStatus.QUARANTINED
            )
            return DocumentIngestionResult(
                run=values["run"], item=item, source=values["source"], revision=values["revision"],
                provenance=values["provenance"], binding=values["binding"], validation=values["validation"],
                admission=values["admission"], status=status, replay=True,
            )
        if head.state is IngestionItemState.PROCESSING_FAILED:
            return None
        artifact = None
        if stored_receipt.output_reference is not None:
            artifact = await self.parsed_artifacts.get(
                workspace_id=request.workspace_id,
                environment_id=request.environment_id,
                artifact_id=stored_receipt.output_reference,
            )
        status = (
            DocumentIngestionStatus.PARTIAL
            if stored_receipt.coverage is ProcessingCoverage.PARTIAL
            else DocumentIngestionStatus.SUCCEEDED
        )
        return DocumentIngestionResult(
            run=values["run"], item=item, source=values["source"], revision=values["revision"],
            provenance=values["provenance"], binding=values["binding"], validation=values["validation"],
            admission=values["admission"], status=status, artifact=artifact,
            processing_receipt=stored_receipt, replay=True,
        )

    async def _process(self, **values: object) -> DocumentIngestionResult:
        request = values["request"]
        assert isinstance(request, DocumentUploadCommand)
        revision = values["revision"]
        assert isinstance(revision, SourceRevision)
        item = values["item"]
        assert isinstance(item, IngestionRunSource)
        attempt = await self._item_attempt(request, item)
        status = DocumentIngestionStatus.SUCCEEDED
        artifact: ParsedDocument | None = None
        failure: IngestionFailure | None = None
        warning: IngestionWarning | None = None
        try:
            if revision.media_type not in {
                "text/plain",
                "text/markdown",
                "text/x-markdown",
                "application/pdf",
                DOCX_MEDIA_TYPE,
                XLSX_MEDIA_TYPE,
            }:
                raise UnsupportedDocumentFormat()
            await self._inject_qualification_fault(
                request=request,
                item=item,
                boundary=IngestionFaultBoundary.ORIGINAL_STORAGE,
            )
            stream = self.validation_reader.read_finalized(
                workspace_id=request.workspace_id,
                environment_id=request.environment_id,
                source_revision_id=revision.revision_id,
                receipt=values["upload"].write_receipt,
                max_bytes=self.max_parse_bytes,
            )
            await self._inject_qualification_fault(
                request=request,
                item=item,
                boundary=IngestionFaultBoundary.PARSER,
            )
            artifact = self.parser(
                revision,
                await _read_bounded(stream, self.max_parse_bytes),
                media_type=revision.media_type,
            )
            if artifact.coverage is ParseCoverage.FAILED:
                status = DocumentIngestionStatus.FAILED
            elif artifact.coverage is ParseCoverage.PARTIAL:
                status = DocumentIngestionStatus.PARTIAL
        except UnsupportedDocumentFormat:
            status = DocumentIngestionStatus.UNSUPPORTED
        except IngestionFaultInjected as error:
            status = DocumentIngestionStatus.FAILED
            failure = self._failure(
                item,
                code=error.reason_code.value,
                stage=(
                    FailureStage.STORING
                    if error.reason_code.value == "original_storage_unavailable"
                    else FailureStage.PARSING
                ),
                retryable=True,
                attempt=attempt,
            )
        except Exception:
            status = DocumentIngestionStatus.FAILED

        if artifact is not None and artifact.coverage is not ParseCoverage.FAILED:
            await self.parsed_artifacts.persist(
                ParsedArtifactCommand.create(artifact, command_identity=f"{request.command_identity}.parse")
            )
            if artifact.coverage is ParseCoverage.PARTIAL:
                warning = self._warning(
                    item, code="partial_coverage", stage=WarningStage.PARSING, attempt=attempt
                )
        if failure is None and status in {
            DocumentIngestionStatus.FAILED,
            DocumentIngestionStatus.UNSUPPORTED,
        }:
            failure = self._failure(
                item,
                code="parser_failed",
                stage=FailureStage.PARSING,
                retryable=status is DocumentIngestionStatus.FAILED,
                attempt=attempt,
            )
        receipt = self._processing_receipt(
            request=request,
            item=item,
            revision=revision,
            artifact=artifact,
            status=status,
            warning=warning,
            failure=failure,
        )
        stored_receipt = await self._persist_processing(
            request=request,
            run=values["run"],
            item=item,
            receipt=receipt,
            warning=warning,
            failure=failure,
            artifact=artifact,
        )
        return DocumentIngestionResult(
            run=values["run"], item=item, source=values["source"], revision=revision,
            provenance=values["provenance"], binding=values["binding"], validation=values["validation"],
            admission=values["admission"], status=status, artifact=artifact,
            processing_receipt=stored_receipt, replay=bool(values["replay"]),
            failure_ids=(failure.ingestion_failure_id,) if failure else (),
        )

    async def _inject_qualification_fault(
        self,
        *,
        request: DocumentUploadCommand,
        item: IngestionRunSource,
        boundary: IngestionFaultBoundary,
    ) -> None:
        """Apply one configured fault only until its ledger failure is durable."""

        if self.fault_injector is None:
            return
        context = self.context_factory(
            PersistenceOperation("ingestion_run"), _id(request, "fault-check-trace")
        )
        async with self.uow_factory(context) as uow:
            failures = await uow.ingestion.resolve_failures(
                item.ingestion_run_source_id
            )
            await uow.rollback()
        if failures:
            return
        self.fault_injector.inject_if_bound(
            workspace_id=request.workspace_id,
            environment_id=request.environment_id,
            command_identity=request.command_identity,
            boundary=boundary,
        )

    async def _persist_processing(
        self,
        *,
        request: DocumentUploadCommand,
        run: IngestionRun,
        item: IngestionRunSource,
        receipt: ProcessingReceipt,
        warning: IngestionWarning | None,
        failure: IngestionFailure | None,
        artifact: ParsedDocument | None,
    ) -> ProcessingReceipt:
        context = self.context_factory(PersistenceOperation("processing_receipt"), _id(request, "receipt-trace"))
        async with self.uow_factory(context) as uow:
            if warning is not None:
                await uow.ingestion.append_warning(warning)
            if failure is not None:
                await uow.ingestion.append_failure(failure)
            result = await uow.ingestion.record_processing_receipt(
                ProcessingReceiptCommand.create(
                    receipt=receipt,
                    idempotency_key=_key(request, "processing-receipt"),
                )
            )
            if not isinstance(result.value, ProcessingReceipt):
                raise TypeError("processing receipt repository returned an invalid value")
            stored_receipt = result.value
            target = IngestionItemState.PROCESSED if artifact is not None and receipt.coverage is not ProcessingCoverage.FAILED else IngestionItemState.PROCESSING_FAILED
            await self._advance_item_in_uow(uow, item, target, self._now())
            if artifact is not None and receipt.coverage is not ProcessingCoverage.FAILED:
                payload = IngestionRequestPayload(
                    ingestion_run_source_id=item.ingestion_run_source_id,
                    binding_id=receipt.binding_id,
                    source_object_id=receipt.source_object_id,
                    source_revision_id=receipt.source_revision_id,
                    processing_receipt_id=receipt.processing_receipt_id,
                    artifact_id=artifact.artifact_id,
                    artifact_digest=artifact.artifact_digest,
                )
                intent = self.outbox_events.build_intent(
                    workspace_id=request.workspace_id,
                    environment_id=request.environment_id,
                    event_type=PROCESSING_REQUEST_EVENT_TYPE,
                    schema_version=PROCESSING_REQUEST_SCHEMA_VERSION,
                    payload=payload,
                    trace_id=context.trace_id,
                    aggregate=OpaqueObjectReference(
                        object_type="ingestion_run_source",
                        object_id=item.ingestion_run_source_id,
                        schema_version=1,
                    ),
                    producer_deduplication_id=f"processing-request:{receipt.processing_receipt_id}",
                    event_id=_id(request, "processing-request"),
                )
                await uow.outbox.append(intent)
                current = await uow.canonical.resolve_current(
                    stored_receipt.source_object_id
                )
                if (
                    current is not None
                    and current.current_revision_id
                    == stored_receipt.source_revision_id
                ):
                    await uow.outbox.append(
                        build_projection_request_intent(
                            registry=self.outbox_events,
                            run=run,
                            item=item,
                            receipt=stored_receipt,
                            trace_id=context.trace_id,
                            causation_id=intent.event_id,
                        )
                    )
                    await self._advance_item_in_uow(
                        uow,
                        item,
                        IngestionItemState.PROJECTION_REQUESTED,
                        self._now(),
                    )
            await uow.commit()
            return stored_receipt

    async def _advance_run_in_uow(self, uow: object, run: IngestionRun, state: IngestionRunState, occurred_at: datetime) -> None:
        head = await uow.ingestion.resolve_run_head(run.ingestion_run_id)
        if head is None or head.state is state:
            return
        await uow.ingestion.append_transition(
            IngestionTransition(
                ingestion_transition_id=uuid5(NAMESPACE_URL, f"spine:ingestion-run-transition:v1:{run.ingestion_run_id}:{state.value}:{head.attempt + 1}"),
                workspace_id=run.workspace_id, environment_id=run.environment_id,
                target_kind=IngestionTargetKind.RUN, ingestion_run_id=run.ingestion_run_id,
                predecessor_transition_id=head.ingestion_transition_id, state=state,
                attempt=head.attempt + 1, actor_or_service=self.service_name,
                trace_id=_uow_trace_id(uow),
                occurred_at=occurred_at,
            )
        )

    async def _advance_item_in_uow(self, uow: object, item: IngestionRunSource, state: IngestionItemState, occurred_at: datetime) -> None:
        head = await uow.ingestion.resolve_item_head(item.ingestion_run_source_id)
        if head is None or head.state is state:
            return
        trace_id = _uow_trace_id(uow)
        await uow.ingestion.append_transition(
            IngestionTransition(
                ingestion_transition_id=uuid5(NAMESPACE_URL, f"spine:ingestion-item-transition:v1:{item.ingestion_run_source_id}:{state.value}:{head.attempt + 1}"),
                workspace_id=item.workspace_id, environment_id=item.environment_id,
                target_kind=IngestionTargetKind.ITEM, ingestion_run_id=item.ingestion_run_id,
                ingestion_run_source_id=item.ingestion_run_source_id,
                predecessor_transition_id=head.ingestion_transition_id, state=state,
                attempt=head.attempt + 1, actor_or_service=self.service_name,
                trace_id=trace_id, occurred_at=occurred_at,
            )
        )

    def _build_observation(
        self,
        request: DocumentUploadCommand,
        run: IngestionRun,
        item: IngestionRunSource,
        upload: OriginalUploadOutcome,
    ):
        now = self._now()
        source = SourceObject(
            source_object_id=_source_object_id(request),
            workspace_id=request.workspace_id,
            environment_id=request.environment_id,
            source_kind="document",
            identity_mode=IdentityMode.UPLOAD,
            upload_identity=request.upload_identity,
            created_at=now,
        )
        receipt = upload.write_receipt
        media_type = _media_type(request)
        revision = content_revision(
            revision_id=_id(request, f"revision:{receipt.observed_content.observed_sha256}"),
            source_object_id=source.source_object_id,
            workspace_id=request.workspace_id,
            environment_id=request.environment_id,
            kind="content",
            revision_schema_version="source-revision:v1",
            revision_metadata_schema="revision-metadata:r1-document-v1",
            revision_metadata=request.source_metadata,
            original_reference=receipt.object_reference,
            original_sha256=receipt.observed_content.observed_sha256,
            byte_length=receipt.observed_content.observed_byte_length,
            media_type=media_type,
            observed_at=now,
        )
        provenance = SourceRevisionProvenance(
            provenance_id=_id(request, "provenance"), source_object_id=source.source_object_id,
            revision_id=revision.revision_id, workspace_id=request.workspace_id,
            environment_id=request.environment_id, producer_kind="upload",
            producer_reference="document-upload",
            event_identity=_child_identifier("upload-event", request.idempotency_key),
            event_digest=_sha256(f"{request.command_identity}:{revision.revision_digest}"),
            upload_command_reference=_child_identifier("upload-command", request.idempotency_key),
            observer_service=self.service_name,
            received_at=now, observed_at=now,
        )
        association = SourceRevisionReceiptAssociation.from_verified_upload(
            association_id=_id(request, "receipt-association"),
            source_object_id=source.source_object_id, revision_id=revision.revision_id,
            provenance_id=provenance.provenance_id, workspace_id=request.workspace_id,
            environment_id=request.environment_id,
            original_receipt_id=upload.result.issue8_result_reference.receipt_id,
            command_result=upload.result, write_receipt=receipt, associated_at=now,
        )
        item = IngestionRunSource(
            ingestion_run_source_id=item.ingestion_run_source_id,
            ingestion_run_id=run.ingestion_run_id,
            workspace_id=item.workspace_id,
            environment_id=item.environment_id,
            input_ordinal=item.input_ordinal,
            input_identity=item.input_identity,
            input_digest=item.input_digest,
            command_identity=item.command_identity,
            command_digest=item.command_digest,
            created_at=item.created_at,
        )
        binding = IngestionRunSourceBinding(
            binding_id=_id(request, "binding"), ingestion_run_source_id=item.ingestion_run_source_id,
            source_object_id=source.source_object_id, source_revision_id=revision.revision_id,
            workspace_id=request.workspace_id, environment_id=request.environment_id,
            binding_command_identity=_child_identifier(request.command_identity, "binding"),
            binding_command_digest="0" * 64, original_reference=receipt.object_reference,
            original_receipt_kind="issue6.write_receipt",
            original_receipt_id=upload.result.issue8_result_reference.receipt_id,
            original_receipt_digest=receipt.observed_content.observed_sha256, bound_at=now,
        )
        observation = SourceObservationCommand.create(
            source=source, revision=revision, provenance=provenance,
            idempotency_key=_key(request, "observation"), receipt_association=association,
        )
        return source, revision, provenance, binding, observation

    def _processing_receipt(self, *, request, item, revision, artifact, status, warning, failure):
        coverage = (
            ProcessingCoverage.COMPLETE if status is DocumentIngestionStatus.SUCCEEDED
            else ProcessingCoverage.PARTIAL if status is DocumentIngestionStatus.PARTIAL
            else ProcessingCoverage.FAILED
        )
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
                code=exclusion_codes.get(exclusion.code, "unsupported_format"),
                count=exclusion.count,
            )
            for exclusion in artifact.exclusions
        )
        output_artifact = (
            artifact
            if artifact is not None
            and status
            in {DocumentIngestionStatus.SUCCEEDED, DocumentIngestionStatus.PARTIAL}
            else None
        )
        (
            parser_name,
            parser_version,
            parser_config_reference,
            locator_contract_version,
        ) = _parser_metadata(revision.media_type, artifact)
        return ProcessingReceipt(
            processing_receipt_id=_id(request, "processing-receipt"), workspace_id=request.workspace_id,
            environment_id=request.environment_id, ingestion_run_source_id=item.ingestion_run_source_id,
            binding_id=_id(request, "binding"), source_object_id=revision.source_object_id,
            source_revision_id=revision.revision_id, processor_kind="document-ingestion",
            parser_name=parser_name,
            parser_version=parser_version,
            parser_config_reference=parser_config_reference,
            locator_contract_version=locator_contract_version,
            original_sha256=revision.original_sha256 or "0" * 64,
            output_kind="parsed_document" if output_artifact is not None else None,
            output_reference=output_artifact.artifact_id if output_artifact is not None else None,
            output_digest=output_artifact.artifact_digest if output_artifact is not None else None,
            coverage=coverage, exclusions=exclusions,
            warning_ids=(warning.ingestion_warning_id,) if warning else (),
            failure_ids=(failure.ingestion_failure_id,) if failure else (),
            command_identity=_child_identifier(request.command_identity, "processing"),
            command_digest="0" * 64, completed_at=self._now(),
        )

    async def _item_attempt(self, request: DocumentUploadCommand, item: IngestionRunSource) -> int:
        context = self.context_factory(
            PersistenceOperation("ingestion_run"), _id(request, "attempt-trace")
        )
        async with self.uow_factory(context) as uow:
            head = await uow.ingestion.resolve_item_head(item.ingestion_run_source_id)
            await uow.rollback()
        return head.attempt if head is not None else 1

    def _warning(self, item, *, code, stage, attempt=1):
        return IngestionWarning(
            ingestion_warning_id=uuid5(NAMESPACE_URL, f"spine:ingestion-warning:v1:{item.ingestion_run_source_id}:{code}:{attempt}"),
            workspace_id=item.workspace_id, environment_id=item.environment_id,
            ingestion_run_source_id=item.ingestion_run_source_id, attempt=attempt, stage=stage,
            code=code, safe_detail=SafeDiagnosticDetail(schema_version="ingestion-safe-detail:v1", item_count=1),
            occurred_at=self._now(),
        )

    def _failure(self, item, *, code, stage, retryable, attempt=1):
        return IngestionFailure(
            ingestion_failure_id=uuid5(NAMESPACE_URL, f"spine:ingestion-failure:v1:{item.ingestion_run_source_id}:{code}:{attempt}"),
            workspace_id=item.workspace_id, environment_id=item.environment_id,
            ingestion_run_source_id=item.ingestion_run_source_id, attempt=attempt, stage=stage,
            code=code, retryable=retryable,
            safe_detail=SafeDiagnosticDetail(schema_version="ingestion-safe-detail:v1", retryable=retryable),
            occurred_at=self._now(),
        )

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("ingestion clock must be timezone-aware")
        return value

    @staticmethod
    def _validate_request_scope(request: DocumentUploadCommand) -> None:
        if request.workspace_id.int == 0 or request.environment_id.int == 0:
            raise ValueError("ingestion scope identities must be non-zero")


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


def _request_digest(request: DocumentUploadCommand) -> str:
    payload = request.model_dump(mode="json", exclude={"idempotency_key"})
    return _sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _key(request: DocumentUploadCommand, suffix: str):
    from spine.application.persistence.idempotency import IdempotencyKey

    value = f"{request.idempotency_key}:{suffix}"
    return IdempotencyKey(value if len(value) <= 255 else f"{value[:220]}:{_sha256(value)[:32]}")


def _child_identifier(parent: str, suffix: str) -> str:
    value = f"{parent}.{suffix}"
    return value if len(value) <= 64 else f"{value[:31]}-{_sha256(value)[:32]}"


def _id(request: DocumentUploadCommand, suffix: str) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        f"spine:document-ingestion:v1:{request.workspace_id}:{request.environment_id}:{request.upload_identity}:{request.idempotency_key}:{suffix}",
    )


def _source_object_id(request: DocumentUploadCommand) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        f"spine:document-source:v1:{request.workspace_id}:{request.environment_id}:{request.upload_identity}",
    )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _media_type(request: DocumentUploadCommand) -> str:
    if request.declared_media_type:
        return request.declared_media_type.strip().lower()
    suffix = PurePath((request.declared_filename or "").replace("\\", "/")).suffix.lower()
    return {
        ".txt": "text/plain",
        ".md": "text/markdown",
        ".pdf": "application/pdf",
        ".docx": DOCX_MEDIA_TYPE,
        ".xlsx": XLSX_MEDIA_TYPE,
    }.get(suffix, "application/octet-stream")


def _parser_metadata(
    media_type: str,
    artifact: ParsedDocument | None,
) -> tuple[str, str, str, str]:
    """Return stable parser identity even when parsing ends in a failure."""

    if artifact is not None:
        return (
            artifact.parser_name,
            artifact.parser_version,
            artifact.parser_config_reference,
            artifact.locator_contract_version,
        )
    if media_type == "text/plain":
        return (
            "text",
            "text-markdown:v1",
            "text-markdown-default:v1",
            "physical-line-codepoint:v1",
        )
    if media_type in {"text/markdown", "text/x-markdown"}:
        return (
            "markdown",
            "text-markdown:v1",
            "text-markdown-default:v1",
            "physical-line-codepoint:v1",
        )
    return "unsupported", "unsupported:v1", "unsupported:v1", "unsupported:v1"


def _disposition(outcome: ValidationOutcome) -> AdmissionDisposition:
    return {
        ValidationOutcome.VALID: AdmissionDisposition.ADMITTED,
        ValidationOutcome.QUARANTINED: AdmissionDisposition.QUARANTINED,
        ValidationOutcome.REJECTED: AdmissionDisposition.REJECTED,
    }[outcome]


def _uow_trace_id(uow: object) -> UUID:
    factory = getattr(uow, "_trace_id_snapshot", None)
    if callable(factory):
        value = factory()
        if isinstance(value, UUID):
            return value
    return uuid5(NAMESPACE_URL, "spine:document-ingestion:trace")


__all__ = [
    "CanonicalAcceptanceCommandFactory",
    "InitialAccessPolicyEnsurer",
    "DocumentIngestionOrchestrator",
    "DocumentIngestionResult",
    "DocumentIngestionStatus",
    "DocumentParser",
    "DocumentUploadCommand",
    "AuthorizedOriginalContentReader",
    "IngestionRequestPayload",
    "PROCESSING_REQUEST_EVENT_TYPE",
    "PROCESSING_REQUEST_SCHEMA_VERSION",
    "PROJECTION_REQUEST_EVENT_TYPE",
    "PROJECTION_REQUEST_SCHEMA_VERSION",
    "ProjectionRequestPayload",
    "UnsupportedDocumentFormat",
    "build_projection_request_intent",
]
