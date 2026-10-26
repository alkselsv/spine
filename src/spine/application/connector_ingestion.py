"""Canonical ingestion handoff for authorized normalized connector observations."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterable, Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator

from spine.application.admission import AdmissionDecisionService, AdmissionValidationEvidence
from spine.application.connections.google import GoogleReadAdmission, GoogleReadCommand
from spine.application.document_ingestion import (
    PROJECTION_REQUEST_EVENT_TYPE,
    PROJECTION_REQUEST_SCHEMA_VERSION,
    ProjectionRequestPayload,
    build_projection_request_intent,
)
from spine.application.persistence.canonical import (
    CANONICAL_ACCEPTANCE_OUTBOX_EVENT_TYPE,
    CANONICAL_ACCEPTANCE_OUTBOX_SCHEMA_VERSION,
    CanonicalAcceptanceCommand,
    CanonicalAcceptanceResult,
    CanonicalAcceptanceOutboxPayload,
)
from spine.application.persistence.context import (
    PersistenceOperation,
    TrustedPersistenceContext,
)
from spine.application.persistence.errors import OptimisticConflictError
from spine.application.persistence.idempotency import IdempotencyKey
from spine.application.persistence.ingestion import (
    IngestionBindingCommand,
    IngestionRunCommand,
    ProcessingReceiptCommand,
)
from spine.application.persistence.outbox import (
    OpaqueObjectReference,
    OutboxEventRegistry,
)
from spine.application.persistence.repositories import SourceObservationCommand
from spine.application.persistence.unit_of_work import UnitOfWorkFactory
from spine.application.storage import OriginalUploadOutcome
from spine.application.diagnostics import AuditEventRegistry
from spine.domain.audit import (
    AuditObjectReference,
    AuditOutcome,
    CanonicalTransitionAuditPayload,
)
from spine.domain.common import ContextOrigin, VersionIdentifier
from spine.domain.ingestion import (
    IngestionItemState,
    IngestionRun,
    IngestionRunSource,
    IngestionRunSourceBinding,
    IngestionRunState,
    IngestionTargetKind,
    IngestionTransition,
    ProcessingCoverage,
    ProcessingReceipt,
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
from spine.domain.connections.google import OpaqueGoogleReference
from spine.infrastructure.object_storage.contracts import OriginalUploadCommandInput
from spine.connectors.observations import (
    ConnectorCoverage,
    NormalizedConnectorObservation,
)


class ConnectorIngestionCommand(BaseModel):
    """Trusted request identity and current Google read generations."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    workspace_id: UUID
    environment_id: UUID
    command_identity: str = Field(
        min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_.:-]{0,63}$"
    )
    idempotency_key: str = Field(min_length=1, max_length=255)
    connection_id: UUID
    resource_scope_reference: UUID
    authorization_generation: int = Field(ge=1)
    configuration_generation: int = Field(ge=1)
    resource_object_reference: str | None = Field(default=None, min_length=1, max_length=256)
    access_policy_version_id: UUID
    expected_current_revision_id: UUID | None = None

    @model_validator(mode="after")
    def validate_ids(self) -> "ConnectorIngestionCommand":
        for name in (
            "workspace_id",
            "environment_id",
            "connection_id",
            "resource_scope_reference",
            "access_policy_version_id",
        ):
            if getattr(self, name).int == 0:
                raise ValueError(f"{name} must be a non-zero UUID")
        if (
            self.expected_current_revision_id is not None
            and self.expected_current_revision_id.int == 0
        ):
            raise ValueError("expected_current_revision_id must be non-zero")
        return self


class AuthorizedConnectorObservationReader(Protocol):
    """Provider-neutral reader that is already behind connection admission."""

    async def read(
        self, command: ConnectorIngestionCommand, context: TrustedPersistenceContext
    ) -> NormalizedConnectorObservation: ...


class _ConnectorUploadService(Protocol):
    async def upload(
        self,
        context: TrustedPersistenceContext,
        command: OriginalUploadCommandInput,
        chunks: AsyncIterable[bytes],
    ) -> OriginalUploadOutcome: ...


class ConnectorCanonicalAcceptanceCommandFactory(Protocol):
    def __call__(
        self,
        *,
        command: ConnectorIngestionCommand,
        source: SourceObject,
        revision: SourceRevision,
        provenance: SourceRevisionProvenance,
        admission_id: UUID,
        context: TrustedPersistenceContext,
        accepted_at: datetime,
    ) -> CanonicalAcceptanceCommand: ...


class ConnectorIngestionStatus(str, Enum):
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"


@dataclass(frozen=True, slots=True)
class GoogleAuthorizedObservationReader:
    """Adapt the trusted Google read-admission port to the connector seam."""

    admission: GoogleReadAdmission[NormalizedConnectorObservation]

    async def read(
        self, command: ConnectorIngestionCommand, context: TrustedPersistenceContext
    ) -> NormalizedConnectorObservation:
        return await self.admission.read(
            GoogleReadCommand(
                connection_id=command.connection_id,
                resource_scope_reference=OpaqueGoogleReference(
                    value=command.resource_scope_reference
                ),
                authorization_generation=command.authorization_generation,
                configuration_generation=command.configuration_generation,
                resource_object_reference=command.resource_object_reference,
            ),
            context,
        )


@dataclass(frozen=True, slots=True)
class ConnectorIngestionResult:
    command: ConnectorIngestionCommand
    observation: NormalizedConnectorObservation
    run: IngestionRun
    item: IngestionRunSource
    source: SourceObject
    revision: SourceRevision
    provenance: SourceRevisionProvenance
    binding: IngestionRunSourceBinding
    admission: AdmissionDecision
    canonical_acceptance: CanonicalAcceptanceResult | None
    processing_receipt: ProcessingReceipt
    replay: bool

    @property
    def status(self) -> ConnectorIngestionStatus:
        return (
            ConnectorIngestionStatus.PARTIAL
            if self.observation.coverage is ConnectorCoverage.PARTIAL
            else ConnectorIngestionStatus.SUCCEEDED
        )


@dataclass(frozen=True, slots=True)
class ConnectorIngestionOrchestrator:
    """Run one authorized normalized observation through canonical ingestion."""

    uow_factory: UnitOfWorkFactory
    observation_reader: AuthorizedConnectorObservationReader
    uploader: _ConnectorUploadService
    read_context_factory: Callable[[PersistenceOperation, UUID], TrustedPersistenceContext]
    context_factory: Callable[[PersistenceOperation, UUID], TrustedPersistenceContext]
    canonical_command_factory: ConnectorCanonicalAcceptanceCommandFactory
    outbox_events: OutboxEventRegistry
    audit_events: AuditEventRegistry
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    service_name: str = "connector-ingestion"

    def __post_init__(self) -> None:
        self.outbox_events.register(
            event_type=PROJECTION_REQUEST_EVENT_TYPE,
            schema_version=PROJECTION_REQUEST_SCHEMA_VERSION,
            payload_type=ProjectionRequestPayload,
        )

    async def ingest(self, command: ConnectorIngestionCommand) -> ConnectorIngestionResult:
        read_context = self.read_context_factory(
            PersistenceOperation("google_connection_read"),
            _id(command, "read"),
        )
        observation = await self.observation_reader.read(command, read_context)
        self._validate_observation(command, observation)

        run, item, replay = await self._ensure_run(command, observation)
        upload = await self._upload(command, observation)
        source, revision, provenance, binding, observation_command = self._build_observation(
            command, observation, run, item, upload
        )
        source = await self._bind_source(command, item, binding, observation_command, source)
        admission = await self._record_admission(command, item, revision, provenance, observation)
        canonical_context = self.context_factory(
            PersistenceOperation("canonical_acceptance"), _id(command, "canonical")
        )
        canonical_command = self.canonical_command_factory(
            command=command,
            source=source,
            revision=revision,
            provenance=provenance,
            admission_id=admission.decision_id,
            context=canonical_context,
            accepted_at=self._now(),
        )
        canonical_result: CanonicalAcceptanceResult | None
        async with self.uow_factory(canonical_context) as uow:
            current = await uow.canonical.resolve_current(source.source_object_id)
            if (
                current is not None
                and command.expected_current_revision_id is not None
                and command.expected_current_revision_id != current.current_revision_id
            ):
                raise OptimisticConflictError("The expected source head is stale.")
            if current is not None and current.current_revision_id == revision.revision_id:
                canonical_result = None
                await uow.rollback()
            else:
                canonical_result = await uow.canonical.accept(canonical_command)
                await uow.commit()

        request_projection = _should_request_projection(canonical_result, revision)
        receipt = await self._record_processing(
            command=command,
            observation=observation,
            run=run,
            item=item,
            source=source,
            revision=revision,
            binding=binding,
            request_projection=request_projection,
        )
        return ConnectorIngestionResult(
            command=command,
            observation=observation,
            run=run,
            item=item,
            source=source,
            revision=revision,
            provenance=provenance,
            binding=binding,
            admission=admission,
            canonical_acceptance=canonical_result,
            processing_receipt=receipt,
            replay=replay,
        )

    async def _ensure_run(
        self,
        command: ConnectorIngestionCommand,
        observation: NormalizedConnectorObservation,
    ) -> tuple[IngestionRun, IngestionRunSource, bool]:
        now = self._now()
        request_digest = _observation_digest(command, observation)
        run = IngestionRun(
            ingestion_run_id=_id(command, "run"),
            workspace_id=command.workspace_id,
            environment_id=command.environment_id,
            command_identity=command.command_identity,
            command_digest="0" * 64,
            trigger_kind="connector",
            trigger_reference=observation.external_identity.external_object_id,
            requested_scope_schema="ingestion-scope:v1",
            requested_scope_digest=request_digest,
            requested_at=now,
        )
        item = IngestionRunSource(
            ingestion_run_source_id=_id(command, "item"),
            ingestion_run_id=run.ingestion_run_id,
            workspace_id=command.workspace_id,
            environment_id=command.environment_id,
            input_ordinal=1,
            input_identity=observation.external_identity.external_object_id,
            input_digest=request_digest,
            command_identity=command.command_identity,
            command_digest=request_digest,
            created_at=now,
        )
        context = self.context_factory(
            PersistenceOperation("ingestion_run"), _id(command, "run-context")
        )
        async with self.uow_factory(context) as uow:
            result = await uow.ingestion.create_run(
                IngestionRunCommand.create(
                    run=run, idempotency_key=_key(command, "run")
                )
            )
            stored_item = (
                await uow.ingestion.resolve_item(item.ingestion_run_source_id)
                if result.replay
                else None
            )
            if stored_item is None:
                stored_item = await uow.ingestion.create_item(item)
            run_head = await uow.ingestion.resolve_run_head(run.ingestion_run_id)
            if run_head is not None and run_head.state is IngestionRunState.REQUESTED:
                await self._append_transition(
                    uow, stored_item, IngestionRunState.RECEIVING, now, is_run=True
                )
            await uow.commit()
        return result.value, stored_item, result.replay  # type: ignore[return-value]

    async def _upload(
        self,
        command: ConnectorIngestionCommand,
        observation: NormalizedConnectorObservation,
    ) -> OriginalUploadOutcome:
        digest = hashlib.sha256(observation.original_bytes).hexdigest()
        context = self.context_factory(
            PersistenceOperation("original.upload"), _id(command, "original")
        )
        upload_command = OriginalUploadCommandInput(
            operation_name="original.upload",
            operation_schema_version=1,
            idempotency_key=_event_key(command, observation, "original").value,
            workspace_id=command.workspace_id,
            environment_id=command.environment_id,
            expected_sha256=digest,
            expected_byte_length=len(observation.original_bytes),
            declared_media_type=observation.media_type,
            declared_filename="connector-observation.bin",
            purpose="connector.ingestion.v1",
        )
        return await self.uploader.upload(context, upload_command, _bytes(observation.original_bytes))

    def _build_observation(
        self,
        command: ConnectorIngestionCommand,
        observation: NormalizedConnectorObservation,
        run: IngestionRun,
        item: IngestionRunSource,
        upload: OriginalUploadOutcome,
    ) -> tuple[
        SourceObject,
        SourceRevision,
        SourceRevisionProvenance,
        IngestionRunSourceBinding,
        SourceObservationCommand,
    ]:
        now = upload.write_receipt.finalized_at
        identity = observation.external_identity
        source = SourceObject(
            source_object_id=_source_object_id(
                command,
                identity.external_namespace,
                identity.external_generation,
                identity.external_object_id,
            ),
            workspace_id=command.workspace_id,
            environment_id=command.environment_id,
            source_kind=observation.source_kind,
            identity_mode=IdentityMode.CONNECTOR,
            connection_id=observation.connection_id,
            external_namespace=identity.external_namespace,
            external_generation=identity.external_generation,
            external_object_id=identity.external_object_id,
            created_at=observation.observed_at,
        )
        revision = content_revision(
            revision_id=_stable_id(
                "revision",
                source.source_object_id,
                upload.write_receipt.observed_content.observed_sha256,
                observation.media_type,
                observation.extraction_profile,
            ),
            source_object_id=source.source_object_id,
            workspace_id=command.workspace_id,
            environment_id=command.environment_id,
            kind="content",
            revision_schema_version="source-revision:v1",
            revision_metadata_schema="revision-metadata:r1-document-v1",
            revision_metadata=RevisionMetadata(
                representation_profile=observation.extraction_profile
            ),
            original_reference=upload.write_receipt.object_reference,
            original_sha256=upload.write_receipt.observed_content.observed_sha256,
            byte_length=upload.write_receipt.observed_content.observed_byte_length,
            media_type=observation.media_type,
            observed_at=observation.observed_at,
        )
        provenance = SourceRevisionProvenance(
            provenance_id=_stable_id(
                "provenance", source.source_object_id, observation.event_identity
            ),
            source_object_id=source.source_object_id,
            revision_id=revision.revision_id,
            workspace_id=command.workspace_id,
            environment_id=command.environment_id,
            producer_kind="connector",
            producer_reference=observation.producer_reference,
            event_identity=observation.event_identity,
            event_digest=observation.event_digest,
            connection_id=observation.connection_id,
            upload_command_reference=_event_reference(command, observation, "original"),
            origin_locator_kind=observation.origin_locator_kind,
            origin_locator_value=observation.origin_locator_value,
            origin_locator_schema=observation.origin_locator_schema,
            origin_locator_digest=observation.origin_locator_digest,
            order_scheme=observation.order_scheme,
            order_token=observation.order_token,
            observer_service=observation.observer_service,
            received_at=observation.received_at,
            observed_at=observation.observed_at,
        )
        association = SourceRevisionReceiptAssociation.from_verified_upload(
            association_id=_stable_id(
                "receipt-association", source.source_object_id, observation.event_identity
            ),
            source_object_id=source.source_object_id,
            revision_id=revision.revision_id,
            provenance_id=provenance.provenance_id,
            workspace_id=command.workspace_id,
            environment_id=command.environment_id,
            original_receipt_id=upload.result.issue8_result_reference.receipt_id,
            command_result=upload.result,
            write_receipt=upload.write_receipt,
            associated_at=now,
        )
        binding = IngestionRunSourceBinding(
            binding_id=_id(command, "binding"),
            ingestion_run_source_id=item.ingestion_run_source_id,
            source_object_id=source.source_object_id,
            source_revision_id=revision.revision_id,
            workspace_id=command.workspace_id,
            environment_id=command.environment_id,
            binding_command_identity=_identifier(command, "binding"),
            binding_command_digest="0" * 64,
            original_reference=upload.write_receipt.object_reference,
            original_receipt_kind="issue6.write_receipt",
            original_receipt_id=upload.result.issue8_result_reference.receipt_id,
            original_receipt_digest=upload.write_receipt.observed_content.observed_sha256,
            bound_at=now,
        )
        observation_command = SourceObservationCommand.create(
            source=source,
            revision=revision,
            provenance=provenance,
            idempotency_key=_event_key(command, observation, "observation"),
            receipt_association=association,
        )
        return source, revision, provenance, binding, observation_command

    async def _bind_source(
        self,
        command: ConnectorIngestionCommand,
        item: IngestionRunSource,
        binding: IngestionRunSourceBinding,
        observation: SourceObservationCommand,
        source: SourceObject,
    ) -> SourceObject:
        context = self.context_factory(
            PersistenceOperation("ingestion_binding"), _id(command, "binding-context")
        )
        async with self.uow_factory(context) as uow:
            existing = await uow.sources.resolve_source(binding.source_object_id)
            if existing is not None:
                source = existing
                observation = replace(observation, source=existing)
            head = await uow.ingestion.resolve_item_head(item.ingestion_run_source_id)
            if head is not None and head.state is IngestionItemState.RECEIVED:
                await self._append_transition(
                    uow, item, IngestionItemState.STORED, self._now()
                )
            await uow.ingestion.bind_source(
                IngestionBindingCommand.create(
                    item=item,
                    binding=binding,
                    observation=observation,
                    idempotency_key=_key(command, "binding"),
                )
            )
            await uow.commit()
        return source

    async def _record_admission(
        self,
        command: ConnectorIngestionCommand,
        item: IngestionRunSource,
        revision: SourceRevision,
        provenance: SourceRevisionProvenance,
        observation: NormalizedConnectorObservation,
    ) -> AdmissionDecision:
        context = self.context_factory(
            PersistenceOperation("ingestion_admission"), _id(command, "admission")
        )
        async with self.uow_factory(context) as uow:
            head = await uow.ingestion.resolve_item_head(item.ingestion_run_source_id)
            if head is not None and head.state is IngestionItemState.OBSERVED:
                await self._append_transition(
                    uow, item, IngestionItemState.ADMISSION_PENDING, self._now()
                )
            existing = await uow.admissions.resolve_effective(revision.revision_id)
            if existing is not None:
                await uow.commit()
                return existing
            decision = await AdmissionDecisionService(
                uow.admissions,
                id_factory=lambda: _id(command, "admission-decision"),
            ).record(
                revision=revision,
                provenance=provenance,
                evidence=AdmissionValidationEvidence(
                    disposition=AdmissionDisposition.ADMITTED,
                    validator_profile_version="admission:r1",
                    reason_schema_version="connector-admission-reason:v1",
                    reason_codes=(
                        "connector_observation_admitted",
                        "partial_coverage"
                        if observation.coverage is ConnectorCoverage.PARTIAL
                        else "complete_coverage",
                    ),
                    authority_reference=AdmissionReference(
                        reference_type="google_read_admission",
                        reference_id=observation.connection_id,
                        schema_version=1,
                    ),
                    trace_id=context.trace_id,
                    audit_event_id=_id(command, "admission-audit"),
                    decided_at=self._now(),
                ),
            )
            await uow.commit()
            return decision

    async def _record_processing(
        self,
        *,
        command: ConnectorIngestionCommand,
        observation: NormalizedConnectorObservation,
        run: IngestionRun,
        item: IngestionRunSource,
        source: SourceObject,
        revision: SourceRevision,
        binding: IngestionRunSourceBinding,
        request_projection: bool,
    ) -> ProcessingReceipt:
        context = self.context_factory(
            PersistenceOperation("processing_receipt"), _id(command, "processing")
        )
        coverage = (
            ProcessingCoverage.PARTIAL
            if observation.coverage is ConnectorCoverage.PARTIAL
            else ProcessingCoverage.COMPLETE
        )
        receipt = ProcessingReceipt(
            processing_receipt_id=_id(command, "processing-receipt"),
            workspace_id=command.workspace_id,
            environment_id=command.environment_id,
            ingestion_run_source_id=item.ingestion_run_source_id,
            binding_id=binding.binding_id,
            source_object_id=source.source_object_id,
            source_revision_id=revision.revision_id,
            processor_kind="connector-ingestion",
            parser_name="connector-normalizer",
            parser_version=observation.extraction_profile,
            parser_config_reference=observation.extraction_profile,
            locator_contract_version=observation.locator_contract_version,
            original_sha256=revision.original_sha256 or ("0" * 64),
            output_kind="normalized_connector_observation",
            output_reference=revision.revision_id,
            output_digest=revision.revision_digest,
            coverage=coverage,
            command_identity=_identifier(command, "processing"),
            command_digest="0" * 64,
            completed_at=self._now(),
        )
        async with self.uow_factory(context) as uow:
            run_head = await uow.ingestion.resolve_run_head(run.ingestion_run_id)
            if run_head is not None and run_head.state is IngestionRunState.RECEIVING:
                await self._append_transition(
                    uow, item, IngestionRunState.PROCESSING, self._now(), is_run=True
                )
            head = await uow.ingestion.resolve_item_head(item.ingestion_run_source_id)
            if head is not None and head.state is IngestionItemState.ADMISSION_PENDING:
                await self._append_transition(
                    uow, item, IngestionItemState.ACCEPTED, self._now()
                )
                await self._append_transition(
                    uow, item, IngestionItemState.PROCESSING, self._now()
                )
            elif head is not None and head.state is IngestionItemState.ACCEPTED:
                await self._append_transition(
                    uow, item, IngestionItemState.PROCESSING, self._now()
                )
            result = await uow.ingestion.record_processing_receipt(
                ProcessingReceiptCommand.create(
                    receipt=receipt,
                    idempotency_key=_key(command, "processing-receipt"),
                )
            )
            stored = result.value
            if not isinstance(stored, ProcessingReceipt):
                raise TypeError("processing receipt repository returned an invalid value")
            if request_projection:
                await uow.outbox.append(
                    build_projection_request_intent(
                        registry=self.outbox_events,
                        run=run,
                        item=item,
                        receipt=stored,
                        trace_id=context.trace_id,
                        causation_id=stored.processing_receipt_id,
                    )
                )
            current = await uow.ingestion.resolve_item_head(item.ingestion_run_source_id)
            if current is not None and current.state is IngestionItemState.PROCESSING:
                await self._append_transition(
                    uow, item, IngestionItemState.PROCESSED, self._now()
                )
                if request_projection:
                    await self._append_transition(
                        uow, item, IngestionItemState.PROJECTION_REQUESTED, self._now()
                    )
            await uow.commit()
            return stored

    async def _append_transition(
        self,
        uow: object,
        item: IngestionRunSource,
        state: IngestionRunState | IngestionItemState,
        occurred_at: datetime,
        *,
        is_run: bool = False,
    ) -> None:
        if is_run:
            head = await uow.ingestion.resolve_run_head(item.ingestion_run_id)
            if head is None or head.state is state:
                return
            await uow.ingestion.append_transition(
                IngestionTransition(
                    ingestion_transition_id=uuid5(
                        NAMESPACE_URL,
                        f"connector-run-transition:{item.ingestion_run_id}:{state.value}:{head.attempt + 1}",
                    ),
                    workspace_id=item.workspace_id,
                    environment_id=item.environment_id,
                    target_kind=IngestionTargetKind.RUN,
                    ingestion_run_id=item.ingestion_run_id,
                    state=state,
                    predecessor_transition_id=head.ingestion_transition_id,
                    attempt=head.attempt + 1,
                    actor_or_service=self.service_name,
                    trace_id=_uow_trace(uow),
                    occurred_at=occurred_at,
                )
            )
            return
        head = await uow.ingestion.resolve_item_head(item.ingestion_run_source_id)
        if head is None or head.state is state:
            return
        await uow.ingestion.append_transition(
            IngestionTransition(
                ingestion_transition_id=uuid5(
                    NAMESPACE_URL,
                    f"connector-item-transition:{item.ingestion_run_source_id}:{state.value}:{head.attempt + 1}",
                ),
                workspace_id=item.workspace_id,
                environment_id=item.environment_id,
                target_kind=IngestionTargetKind.ITEM,
                ingestion_run_id=item.ingestion_run_id,
                ingestion_run_source_id=item.ingestion_run_source_id,
                state=state,
                predecessor_transition_id=head.ingestion_transition_id,
                attempt=head.attempt + 1,
                actor_or_service=self.service_name,
                trace_id=_uow_trace(uow),
                occurred_at=occurred_at,
            )
        )

    @staticmethod
    def _validate_observation(
        command: ConnectorIngestionCommand,
        observation: NormalizedConnectorObservation,
    ) -> None:
        if (
            observation.workspace_id,
            observation.environment_id,
            observation.connection_id,
            observation.resource_scope_reference,
            observation.authorization_generation,
            observation.configuration_generation,
        ) != (
            command.workspace_id,
            command.environment_id,
            command.connection_id,
            command.resource_scope_reference,
            command.authorization_generation,
            command.configuration_generation,
        ):
            raise ValueError("connector observation is outside the trusted read scope")

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("connector ingestion clock must be timezone-aware")
        return value


def build_connector_canonical_acceptance_command(
    *,
    outbox_events: OutboxEventRegistry,
    audit_events: AuditEventRegistry,
    command: ConnectorIngestionCommand,
    source: SourceObject,
    revision: SourceRevision,
    provenance: SourceRevisionProvenance,
    admission_id: UUID,
    context: TrustedPersistenceContext,
    accepted_at: datetime,
) -> CanonicalAcceptanceCommand:
    """Build the normal canonical audit/outbox evidence for connector input."""

    outbox_events.register(
        event_type=CANONICAL_ACCEPTANCE_OUTBOX_EVENT_TYPE,
        schema_version=CANONICAL_ACCEPTANCE_OUTBOX_SCHEMA_VERSION,
        payload_type=CanonicalAcceptanceOutboxPayload,
    )
    acceptance_id = _id(command, f"acceptance:{revision.revision_id}")
    audit_id = _id(command, f"canonical-audit:{revision.revision_id}")
    event_id = _id(command, f"canonical-outbox:{revision.revision_id}")
    audit = audit_events.build_event(
        audit_event_id=audit_id,
        workspace_id=command.workspace_id,
        environment_id=command.environment_id,
        event_type="canonical.transition",
        schema_version=1,
        payload=CanonicalTransitionAuditPayload(transition="source.accepted"),
        origin=ContextOrigin.WORKER,
        acting_subject_id=None,
        service_principal_id=context.service_principal_id,
        trace_id=context.trace_id,
        occurred_at=accepted_at,
        outcome=AuditOutcome.COMMITTED,
        reason="source_accepted",
        target=AuditObjectReference(
            object_type="source_revision", object_id=revision.revision_id, schema_version=1
        ),
        producer_deduplication_id=f"connector-ingestion:{revision.revision_id}",
    )
    outbox = outbox_events.build_intent(
        event_id=event_id,
        workspace_id=command.workspace_id,
        environment_id=command.environment_id,
        event_type=CANONICAL_ACCEPTANCE_OUTBOX_EVENT_TYPE,
        schema_version=CANONICAL_ACCEPTANCE_OUTBOX_SCHEMA_VERSION,
        payload=CanonicalAcceptanceOutboxPayload(
            canonical_acceptance_id=acceptance_id,
            source_object_id=source.source_object_id,
            source_revision_id=revision.revision_id,
            accepted_generation=None,
            audit_event_id=audit_id,
            transition="source.accepted",
        ),
        trace_id=context.trace_id,
        aggregate=OpaqueObjectReference(
            object_type="source_object", object_id=source.source_object_id, schema_version=1
        ),
        producer_deduplication_id=f"connector-ingestion:{revision.revision_id}",
    )
    return CanonicalAcceptanceCommand.create(
        source=source,
        revision=revision,
        provenance=provenance,
        admission_decision_id=admission_id,
        access_policy_version_id=command.access_policy_version_id,
        expected_current_revision_id=command.expected_current_revision_id,
        idempotency_key=_key(command, "canonical"),
        canonical_acceptance_id=acceptance_id,
        audit_event_id=audit_id,
        outbox_event_id=event_id,
        accepted_at=accepted_at,
        audit_event=audit,
        outbox_intent=outbox,
        operation=context.operation,
    )


async def _bytes(value: bytes):
    yield value


def _observation_digest(
    command: ConnectorIngestionCommand, observation: NormalizedConnectorObservation
) -> str:
    payload = {
        "command": command.model_dump(mode="json"),
        "observation": observation.model_dump(mode="json"),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _key(command: ConnectorIngestionCommand, suffix: str) -> IdempotencyKey:
    value = f"connector:{command.idempotency_key}:{suffix}"
    return IdempotencyKey(value if len(value) <= 255 else f"{value[:220]}:{_sha256(value)[:32]}")


def _event_key(
    command: ConnectorIngestionCommand,
    observation: NormalizedConnectorObservation,
    suffix: str,
) -> IdempotencyKey:
    value = "|".join(
        (
            "connector-event:v1",
            str(command.workspace_id),
            str(command.environment_id),
            str(observation.connection_id),
            observation.producer_reference,
            observation.event_identity,
            suffix,
        )
    )
    return IdempotencyKey(
        value if len(value) <= 255 else f"{value[:220]}:{_sha256(value)[:32]}"
    )


def _event_reference(
    command: ConnectorIngestionCommand,
    observation: NormalizedConnectorObservation,
    suffix: str,
) -> str:
    return f"connector-event:{_sha256(_event_key(command, observation, suffix).value)[:48]}"


def _should_request_projection(result: object, revision: SourceRevision) -> bool:
    """Project only a new canonical revision, not an operational observation."""

    if result is None:
        return False
    if not hasattr(result, "advanced"):
        return True
    if not result.advanced or getattr(result, "acceptance", None) is None:
        return False
    return result.acceptance.predecessor_revision_id != revision.revision_id


def _identifier(command: ConnectorIngestionCommand, suffix: str) -> str:
    value = f"{command.command_identity}.{suffix}"
    return value if len(value) <= 64 else f"{value[:31]}-{_sha256(value)[:32]}"


def _id(command: ConnectorIngestionCommand, suffix: str) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        f"spine:connector-ingestion:v1:{command.workspace_id}:{command.environment_id}:{command.connection_id}:{command.idempotency_key}:{suffix}",
    )


def _source_object_id(
    command: ConnectorIngestionCommand,
    external_namespace: str,
    external_generation: str,
    external_object_id: str,
) -> UUID:
    return _stable_id(
        "source",
        command.workspace_id,
        command.environment_id,
        command.connection_id,
        external_namespace,
        external_generation,
        external_object_id,
    )


def _stable_id(prefix: str, *parts: object) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        ":".join((f"spine:connector-ingestion:{prefix}:v1", *(str(part) for part in parts))),
    )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _uow_trace(uow: object) -> UUID:
    factory = getattr(uow, "_trace_id_snapshot", None)
    value = factory() if callable(factory) else None
    return value if isinstance(value, UUID) else uuid5(NAMESPACE_URL, "spine:connector-ingestion:trace")


__all__ = [
    "AuthorizedConnectorObservationReader",
    "ConnectorCanonicalAcceptanceCommandFactory",
    "ConnectorIngestionCommand",
    "ConnectorIngestionOrchestrator",
    "ConnectorIngestionResult",
    "ConnectorIngestionStatus",
    "GoogleAuthorizedObservationReader",
    "build_connector_canonical_acceptance_command",
]
