"""Executable PostgreSQL/Cognee outbox worker composition."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from spine.application.diagnostics import (
    AuditEventRegistry,
    DiagnosticContext,
    DiagnosticEventRegistry,
)
from spine.application.document_ingestion import (
    PROCESSING_REQUEST_EVENT_TYPE,
    PROCESSING_REQUEST_SCHEMA_VERSION,
    PROJECTION_REQUEST_EVENT_TYPE,
    PROJECTION_REQUEST_SCHEMA_VERSION,
    ProjectionRequestPayload,
)
from spine.application.outbox import OutboxConsumerSuccess
from spine.application.persistence.canonical import (
    CANONICAL_ACCEPTANCE_OUTBOX_EVENT_TYPE,
    CANONICAL_ACCEPTANCE_OUTBOX_SCHEMA_VERSION,
)
from spine.application.persistence.boundary import CanonicalBoundaryCaptureCommand
from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
)
from spine.application.persistence.delivery import OutboxDeliveryRouteRegistry
from spine.application.persistence.outbox import OutboxEventRegistry, OutboxIntent
from spine.application.persistence.idempotency import IdempotencyKey
from spine.application.persistence.projection import (
    PROJECTION_ACTIVATED_OUTBOX_EVENT_TYPE,
    PROJECTION_OUTBOX_SCHEMA_VERSION,
    PROJECTION_RECEIPT_OPERATION,
    ProjectionActivationCommand,
    ProjectionActivationCommitted,
    ProjectionActivationKind,
    ProjectionActivationReplay,
    ProjectionCandidateSealCommand,
    ProjectionReconciliationCommand,
    ProjectionRunCommand,
    ProjectionSnapshotCommand,
    ProjectionValidationCommand,
)
from spine.application.physical_projection_validation import (
    PhysicalProjectionEvidence,
    PhysicalProjectionEvidenceCoordinator,
    PhysicalProjectionReconciliationRequest,
)
from spine.application.projection_result_mapping import ProjectionMappingResult
from spine.infrastructure.db.settings import (
    DispatcherDatabaseSettings,
    POSTGRESQL_SEARCH_PATH_OPTIONS,
    RuntimeDatabaseSettings,
)
from spine.infrastructure.diagnostics import JsonLoggingDiagnosticSink
from spine.infrastructure.ingestion_composition import register_ingestion_outbox_events
from spine.infrastructure.outbox.delivery import PostgreSQLOutboxDeliveryRepository
from spine.infrastructure.persistence.contexts import TrustedContextBoundary
from spine.infrastructure.persistence.parsed_artifacts import PostgreSQLParsedArtifactRepository
from spine.infrastructure.persistence.postgresql import PostgreSQLPersistence
from spine.infrastructure.persistence.projection_composition import (
    PROJECTION_REQUEST_CONSUMER_NAME,
    build_postgresql_cognee_projection_request_binding,
)
from spine.memory.cognee.context_projection import CogneeContextProjection
from spine.memory.cognee.indexing import CogneeChunkProjectionStore
from spine.memory.cognee.retrieval import CogneeStructuredRetrieval
from spine.memory.contracts import ProjectionCommand, ProjectionEffect
from spine.domain.projections import (
    OntologyVersion,
    ProjectionCandidateState,
    ProjectionConfigVersion,
    ProjectionOperation,
    ProjectionPartition,
    ProjectionRun,
    ProjectionRunState,
    ProjectionRunTransition,
    ProjectionSnapshot,
    ProjectionSnapshotMember,
    ProjectionSnapshotMemberDisposition,
    ProjectionSnapshotState,
    PublishedActivePublication,
    projection_snapshot_manifest_digest,
)
from spine.domain.sources import RevisionKind
from spine.memory.validation import (
    ArtifactKind,
    PhysicalArtifact,
    PhysicalArtifactLocator,
    PhysicalProjectionExpectation,
    PhysicalProjectionSource,
    validate_physical_projection,
)
from spine.workers import OutboxConsumerBinding, OutboxWorker, create_outbox_worker
from spine.workflows.temporal.bootstrap import (
    build_ask_admission_delivery_binding,
    build_ask_cancellation_delivery_binding,
    register_ask_admission_delivery_route,
)
from spine.workflows.temporal.client import TemporalSdkWorkflowClient


class OutboxWorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        extra="forbid",
        frozen=True,
        env_file=None,
        env_prefix="SPINE_WORKER_",
        case_sensitive=False,
        hide_input_in_errors=True,
    )

    service_principal_id: UUID
    identity: str = Field(default="projection-worker", pattern=r"^[a-z][a-z0-9_.:-]{0,63}$")
    batch_size: int = Field(default=20, ge=1, le=1000)
    poll_interval_seconds: float = Field(default=1.0, gt=0, le=60)
    lease_seconds: float = Field(default=60.0, gt=1, le=3600)
    handler_timeout_seconds: float = Field(default=45.0, gt=0, le=3599)
    maximum_attempts: int = Field(default=5, ge=1, le=100)
    temporal_address: str = Field(default="temporal:7233", min_length=1, max_length=255)
    temporal_namespace: str = Field(default="default", min_length=1, max_length=255)
    temporal_task_queue: str = Field(
        default="spine-r1-qa", min_length=1, max_length=255
    )


class _LazyTemporalWorkflowClient:
    """Connect to Temporal only when an Ask delivery is actually claimed."""

    def __init__(self, settings: OutboxWorkerSettings) -> None:
        self._settings = settings
        self._client: TemporalSdkWorkflowClient | None = None
        self._lock = asyncio.Lock()

    async def _resolved(self) -> TemporalSdkWorkflowClient:
        if self._client is not None:
            return self._client
        async with self._lock:
            if self._client is None:
                from temporalio.client import Client
                from temporalio.common import WorkflowIDReusePolicy

                sdk = await Client.connect(
                    self._settings.temporal_address,
                    namespace=self._settings.temporal_namespace,
                )
                self._client = TemporalSdkWorkflowClient(
                    sdk,
                    task_queue=self._settings.temporal_task_queue,
                    id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                )
        return self._client

    async def start_workflow(self, **kwargs):
        return await (await self._resolved()).start_workflow(**kwargs)

    async def get_workflow(self, **kwargs):
        return await (await self._resolved()).get_workflow(**kwargs)

    async def request_cancellation(self, **kwargs):
        return await (await self._resolved()).request_cancellation(**kwargs)


class _AcknowledgingConsumer:
    """Acknowledge a committed notification whose synchronous effect is complete."""

    async def consume(
        self,
        intent: OutboxIntent,
        diagnostic_context: DiagnosticContext,
    ) -> OutboxConsumerSuccess:
        del intent
        return OutboxConsumerSuccess(diagnostic_context=diagnostic_context)


@dataclass(frozen=True, slots=True)
class _ProjectionRequestPreparer:
    """Materialize the canonical build ledger consumed by a projection request."""

    uow_factory: object
    context_factory: object

    async def prepare(self, intent: OutboxIntent) -> None:
        payload = intent.payload
        if (
            type(payload) is not ProjectionRequestPayload
            or intent.environment_id is None
        ):
            raise ValueError("projection request is invalid")
        boundary_context = self.context_factory(
            intent.workspace_id,
            intent.environment_id,
            "canonical_boundary_capture",
            intent.event_id,
        )
        async with self.uow_factory(boundary_context) as uow:  # type: ignore[operator]
            captured = await uow.boundaries.capture(
                CanonicalBoundaryCaptureCommand.create(
                    projection_kind="qa",
                    scope_version="r1",
                    idempotency_key=IdempotencyKey(
                        f"projection-boundary-{payload.source_revision_id}"
                    ),
                )
            )
            await uow.commit()
        matching_members = tuple(
            value
            for value in captured.members
            if value.source_revision_id == payload.source_revision_id
        )
        if len(matching_members) != 1:
            raise ValueError("projection request requires an exact source boundary")
        source_member = matching_members[0]

        now = datetime.now(timezone.utc)
        def stable(name: str) -> UUID:
            return uuid5(
                NAMESPACE_URL,
                "|".join(
                    (
                        "spine:projection-request-ledger:v1",
                        str(intent.workspace_id),
                        str(intent.environment_id),
                        str(payload.source_revision_id),
                        name,
                    )
                ),
            )

        def stable_configuration(name: str) -> UUID:
            return _projection_configuration_id(
                intent.workspace_id,
                intent.environment_id,
                name,
            )
        ontology_payload = {
            "schema_version": "ontology-definition:r1",
            "entity_types": [{"name": "document"}],
            "relation_types": [],
        }
        ontology = OntologyVersion(
            ontology_version_id=stable_configuration("ontology"),
            workspace_id=intent.workspace_id,
            environment_id=intent.environment_id,
            ontology_name="governed_qa",
            version=1,
            definition_payload=ontology_payload,
            definition_digest=_definition_digest(
                "ontology-definition:r1", ontology_payload
            ),
            publication_eligible=True,
            publication_decision_reference="governed-qa-r1",
            publisher_authority_reference="projection-worker-r1",
            audit_event_reference=str(intent.event_id),
            published_at=now,
        )
        config_payload = {"adapter": "cognee", "profile": "governed-qa-r1"}
        config = ProjectionConfigVersion(
            projection_config_version_id=stable_configuration("qa-config"),
            workspace_id=intent.workspace_id,
            environment_id=intent.environment_id,
            projection_kind="qa",
            version=1,
            ontology_version_id=ontology.ontology_version_id,
            parser_contract_version=payload.parser_version,
            locator_contract_version=payload.locator_contract_version,
            extraction_contract_version="document-extraction:r1",
            embedding_profile_version="configured-embedding:r1",
            adapter_contract_version="context-projection:v1",
            config_payload=config_payload,
            config_digest=_definition_digest(
                "projection-config:r1", config_payload
            ),
            created_at=now,
        )
        command_digest = hashlib.sha256(
            intent.model_dump_json().encode("utf-8")
        ).hexdigest()
        run = ProjectionRun(
            projection_run_id=stable("run"),
            projection_candidate_id=stable("candidate"),
            workspace_id=intent.workspace_id,
            environment_id=intent.environment_id,
            projection_kind="qa",
            operation=ProjectionOperation.BUILD,
            projection_config_version_id=config.projection_config_version_id,
            canonical_boundary_id=captured.boundary.boundary_id,
            command_identity=f"project-{payload.source_revision_id.hex[:24]}",
            command_digest=command_digest,
            idempotency_receipt_id=stable("run-receipt-placeholder"),
            started_at=now,
        )
        setup_context = self.context_factory(
            intent.workspace_id,
            intent.environment_id,
            "projection_run",
            intent.event_id,
        )
        async with self.uow_factory(setup_context) as uow:  # type: ignore[operator]
            registered_ontology = await uow.projections.resolve_ontology_version(
                ontology.ontology_version_id
            )
            if registered_ontology is None:
                legacy_config = await uow.projections.resolve_config_version_number(
                    "context", 1
                )
                if legacy_config is not None:
                    registered_ontology = (
                        await uow.projections.resolve_ontology_version(
                            legacy_config.ontology_version_id
                        )
                    )
            if registered_ontology is not None:
                if (
                    registered_ontology.workspace_id != ontology.workspace_id
                    or registered_ontology.environment_id != ontology.environment_id
                    or registered_ontology.ontology_name != ontology.ontology_name
                    or registered_ontology.version != ontology.version
                    or registered_ontology.definition_digest
                    != ontology.definition_digest
                    or not registered_ontology.publication_eligible
                ):
                    raise ValueError("projection ontology identity conflicts")
                ontology = registered_ontology
                config = config.model_copy(
                    update={"ontology_version_id": ontology.ontology_version_id}
                )
            registered_config = (
                await uow.projections.resolve_config_version_number("qa", 1)
            )
            if registered_config is not None:
                if (
                    registered_config.workspace_id != config.workspace_id
                    or registered_config.environment_id != config.environment_id
                    or registered_config.config_digest != config.config_digest
                    or registered_config.parser_contract_version
                    != config.parser_contract_version
                    or registered_config.locator_contract_version
                    != config.locator_contract_version
                ):
                    raise ValueError("projection configuration identity conflicts")
                config = registered_config
                run = run.model_copy(
                    update={
                        "projection_config_version_id": (
                            config.projection_config_version_id
                        )
                    }
                )
            existing = await uow.projections.list_runs_for_source_revision(
                source_member.source_object_id,
                payload.source_revision_id,
            )
            if existing:
                if len(existing) != 1 or existing[0].projection_run_id != run.projection_run_id:
                    raise ValueError("projection run identity conflicts")
                await uow.commit()
                return
            if registered_config is None:
                if registered_ontology is None:
                    await uow.projections.add_ontology_version(ontology)
                await uow.projections.add_config_version(config)
            await uow.projections.create_run(
                ProjectionRunCommand.create(
                    run=run,
                    idempotency_key=IdempotencyKey(
                        f"projection-run-{payload.source_revision_id}"
                    ),
                )
            )
            await uow.projections.append_transition(
                ProjectionRunTransition(
                    projection_run_transition_id=stable("requested"),
                    projection_run_id=run.projection_run_id,
                    projection_candidate_id=run.projection_candidate_id,
                    workspace_id=run.workspace_id,
                    environment_id=run.environment_id,
                    state=ProjectionRunState.REQUESTED,
                    attempt=1,
                    occurred_at=now,
                )
            )
            await uow.projections.add_partition(
                ProjectionPartition(
                    projection_partition_id=stable("partition"),
                    projection_run_id=run.projection_run_id,
                    projection_candidate_id=run.projection_candidate_id,
                    workspace_id=run.workspace_id,
                    environment_id=run.environment_id,
                    security_domain_reference="workspace-default",
                    security_domain_version="r1",
                    partition_identity="default",
                    expected_command_identity=f"project-{payload.source_revision_id.hex[:24]}",
                    expected_command_digest=command_digest,
                    created_at=now,
                )
            )
            await uow.commit()


@dataclass(frozen=True, slots=True)
class _PreparingProjectionConsumer:
    preparer: _ProjectionRequestPreparer
    consumer: object

    async def consume(
        self,
        intent: OutboxIntent,
        diagnostic_context: DiagnosticContext,
    ) -> OutboxConsumerSuccess:
        await self.preparer.prepare(intent)
        return await self.consumer.consume(intent, diagnostic_context)  # type: ignore[attr-defined,no-any-return]


def _definition_digest(schema: str, payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            {"schema": schema, "payload": payload},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _projection_configuration_id(
    workspace_id: UUID,
    environment_id: UUID,
    name: str,
) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        "|".join(
            (
                "spine:projection-request-configuration:v1",
                str(workspace_id),
                str(environment_id),
                name,
            )
        ),
    )


@dataclass(frozen=True, slots=True)
class _DurableArtifactResolver:
    uow_factory: object
    parsed_artifacts: PostgreSQLParsedArtifactRepository
    context_factory: object
    processing_context_factory: object | None = None

    async def __call__(self, effect: ProjectionEffect):
        if effect.processing_receipt_id is None:
            return None
        context = self.context_factory(effect)  # type: ignore[operator]
        async with self.uow_factory(context) as uow:  # type: ignore[operator]
            receipt = await uow.ingestion.resolve_processing_receipt_by_id(
                effect.processing_receipt_id
            )
            await uow.commit()
        if receipt is None or receipt.output_reference is None:
            return None
        return await self.parsed_artifacts.get(
            workspace_id=effect.workspace_id,
            environment_id=effect.environment_id,
            artifact_id=receipt.output_reference,
        )

    async def document_for(self, command: ProjectionCommand, processing_receipt_id: UUID):
        if self.processing_context_factory is None:
            raise ValueError("processing receipt context is unavailable")
        context = self.processing_context_factory(  # type: ignore[operator]
            command.workspace_id,
            command.environment_id,
            command.projection_run_id,
        )
        async with self.uow_factory(context) as uow:  # type: ignore[operator]
            receipt = await uow.ingestion.resolve_processing_receipt_by_id(
                processing_receipt_id
            )
            await uow.commit()
        if receipt is None or receipt.output_reference is None:
            raise ValueError("canonical parsed artifact receipt is unavailable")
        document = await self.parsed_artifacts.get(
            workspace_id=command.workspace_id,
            environment_id=command.environment_id,
            artifact_id=receipt.output_reference,
        )
        if document is None:
            raise ValueError("canonical parsed artifact is unavailable")
        return document


@dataclass(frozen=True, slots=True)
class _ProjectionPublicationCompletion:
    """Resume validation and publication after the canonical receipt boundary."""

    uow_factory: object
    context_factory: object
    artifact_resolver: _DurableArtifactResolver
    physical_store: CogneeChunkProjectionStore
    clock: object

    async def complete(self, mapping: ProjectionMappingResult) -> None:
        command = mapping.handoff.command
        evidence = await self._evidence(mapping)
        coordinator = PhysicalProjectionEvidenceCoordinator(
            uow_factory=self.uow_factory,  # type: ignore[arg-type]
            context_factory=lambda value: self.context_factory(  # type: ignore[operator]
                command.workspace_id,
                command.environment_id,
                value.digest.operation.value,
                command.projection_run_id,
            ),
        )
        run = await self._run(command)
        if run.candidate_state is not ProjectionCandidateState.SEALED:
            sealed = await coordinator.validate_and_seal(
                evidence,
                ProjectionCandidateSealCommand.create(
                    projection_run_id=command.projection_run_id,
                    projection_candidate_id=command.projection_candidate.candidate_id,
                    idempotency_key=IdempotencyKey(
                        f"projection-seal-{command.projection_run_id}"
                    ),
                ),
            )
            run = sealed.value
        evidence_digest = run.candidate_evidence_digest
        if evidence_digest is None:
            raise ValueError("sealed projection candidate has no evidence digest")

        active = await self._active(command)
        snapshot_id = _publication_id(command, "snapshot")
        predecessor = (
            active.projection_snapshot.projection_snapshot_id
            if isinstance(active, PublishedActivePublication)
            and active.projection_snapshot.projection_snapshot_id != snapshot_id
            else None
        )
        if (
            isinstance(active, PublishedActivePublication)
            and active.projection_snapshot.projection_snapshot_id == snapshot_id
        ):
            snapshot = active.projection_snapshot
            activation = active.projection_activation
        else:
            snapshot = await self._assemble_snapshot(
                mapping,
                evidence_digest=evidence_digest,
                predecessor_snapshot_id=predecessor,
            )
            activation = await self._activate(snapshot, active)

        await coordinator.reconcile(
            evidence,
            PhysicalProjectionReconciliationRequest(
                snapshot=snapshot,
                activation=activation,
                attempt_identity=_publication_attempt_identity(
                    snapshot.projection_snapshot_id
                ),
                checked_at=self.clock(),  # type: ignore[operator]
                idempotency_receipt_id=_publication_id(command, "reconciliation"),
            ),
        )

    async def _evidence(self, mapping: ProjectionMappingResult) -> PhysicalProjectionEvidence:
        command = mapping.handoff.command
        expected: list[PhysicalArtifact] = []
        sources: list[PhysicalProjectionSource] = []
        for source in command.source_revisions:
            if source.processing_receipt_id is None:
                raise ValueError("projection source has no processing receipt")
            document = await self.artifact_resolver.document_for(
                command, source.processing_receipt_id
            )
            sources.append(
                PhysicalProjectionSource(
                    source_object_id=source.source_object_id,
                    source_revision_id=source.source_revision_id,
                    revision_digest=source.revision_digest,
                    revision_number=source.revision_number,
                    status=source.status,
                )
            )
            for segment in document.segments:
                data_id = self.physical_store.data_id_for(
                    namespace_digest=command.physical_namespace.namespace_digest,
                    source_object_id=source.source_object_id,
                    segment_id=segment.segment_id,
                )
                expected.append(
                    PhysicalArtifact(
                        artifact_kind=ArtifactKind.CHUNK,
                        artifact_id=str(segment.segment_id),
                        data_id=data_id,
                        workspace_id=command.workspace_id,
                        environment_id=command.environment_id,
                        namespace_digest=command.physical_namespace.namespace_digest,
                        source_object_id=source.source_object_id,
                        source_revision_id=source.source_revision_id,
                        revision_digest=source.revision_digest,
                        revision_number=source.revision_number,
                        projection_config_id=command.projection_config.config_id,
                        projection_config_version=command.projection_config.version,
                        projection_config_digest=command.projection_config.config_digest,
                        locators=(
                            PhysicalArtifactLocator.from_payload(
                                kind=segment.locator.kind,
                                payload=segment.locator,
                            ),
                        ),
                    )
                )
        expectation = PhysicalProjectionExpectation(
            workspace_id=command.workspace_id,
            environment_id=command.environment_id,
            namespace_digest=command.physical_namespace.namespace_digest,
            projection_config_id=command.projection_config.config_id,
            projection_config_version=command.projection_config.version,
            projection_config_digest=command.projection_config.config_digest,
            sources=tuple(sources),
            expected_artifacts=tuple(expected),
        )
        actual = await self.physical_store.physical_artifacts_for(
            workspace_id=command.workspace_id,
            environment_id=command.environment_id,
            namespace_digest=command.physical_namespace.namespace_digest,
        )
        return PhysicalProjectionEvidence(
            mapping=mapping,
            expectation=expectation,
            result=validate_physical_projection(expectation, actual),
            evaluated_at=self.clock(),  # type: ignore[operator]
        )

    async def _active(self, command: ProjectionCommand):
        context = self.context_factory(  # type: ignore[operator]
            command.workspace_id,
            command.environment_id,
            "projection_activation",
            command.projection_run_id,
        )
        async with self.uow_factory(context) as uow:  # type: ignore[operator]
            active = await uow.projections.resolve_active_publication("qa")
            await uow.commit()
            return active

    async def _run(self, command: ProjectionCommand) -> ProjectionRun:
        context = self.context_factory(  # type: ignore[operator]
            command.workspace_id,
            command.environment_id,
            "projection_validation",
            command.projection_run_id,
        )
        async with self.uow_factory(context) as uow:  # type: ignore[operator]
            run = await uow.projections.resolve_run(command.projection_run_id)
            await uow.commit()
        if (
            run is None
            or run.workspace_id != command.workspace_id
            or run.environment_id != command.environment_id
            or run.projection_candidate_id
            != command.projection_candidate.candidate_id
        ):
            raise ValueError("canonical projection run is unavailable")
        return run

    async def _assemble_snapshot(
        self,
        mapping: ProjectionMappingResult,
        *,
        evidence_digest: str,
        predecessor_snapshot_id: UUID | None,
    ) -> ProjectionSnapshot:
        command = mapping.handoff.command
        context = self.context_factory(  # type: ignore[operator]
            command.workspace_id,
            command.environment_id,
            "projection_snapshot",
            command.projection_run_id,
        )
        async with self.uow_factory(context) as uow:  # type: ignore[operator]
            boundary_members = await uow.boundaries.resolve_members(
                command.canonical_boundary.boundary_id
            )
            receipt = mapping.handoff.receipt_command.receipt
            snapshot_id = _publication_id(command, "snapshot")
            included_sources = {
                source.source_object_id for source in command.source_revisions
            }
            members = tuple(
                ProjectionSnapshotMember(
                    projection_snapshot_member_id=uuid5(
                        NAMESPACE_URL,
                        f"spine:projection-publication-member:v1:{snapshot_id}:{member.source_object_id}",
                    ),
                    projection_snapshot_id=snapshot_id,
                    canonical_boundary_id=command.canonical_boundary.boundary_id,
                    canonical_boundary_member_id=member.source_object_id,
                    workspace_id=command.workspace_id,
                    environment_id=command.environment_id,
                    source_object_id=member.source_object_id,
                    source_revision_id=member.source_revision_id,
                    disposition=(
                        ProjectionSnapshotMemberDisposition.TOMBSTONED
                        if member.revision_kind is RevisionKind.TOMBSTONE
                        else ProjectionSnapshotMemberDisposition.INCLUDED
                        if member.source_object_id in included_sources
                        else ProjectionSnapshotMemberDisposition.EXCLUDED
                    ),
                    exclusion_code=(
                        "outside_request"
                        if member.revision_kind is RevisionKind.CONTENT
                        and member.source_object_id not in included_sources
                        else None
                    ),
                    projection_partition_id=(
                        receipt.projection_partition_id
                        if member.source_object_id in included_sources
                        else None
                    ),
                    projection_receipt_id=(
                        receipt.projection_receipt_id
                        if member.source_object_id in included_sources
                        else None
                    ),
                )
                for member in boundary_members
            )
            snapshot = ProjectionSnapshot(
                projection_snapshot_id=snapshot_id,
                projection_run_id=command.projection_run_id,
                projection_candidate_id=command.projection_candidate.candidate_id,
                workspace_id=command.workspace_id,
                environment_id=command.environment_id,
                snapshot_state=ProjectionSnapshotState.ASSEMBLING,
                projection_kind="qa",
                projection_config_version_id=command.projection_config.config_id,
                canonical_boundary_id=command.canonical_boundary.boundary_id,
                predecessor_snapshot_id=predecessor_snapshot_id,
                candidate_evidence_digest=evidence_digest,
                manifest_digest="0" * 64,
                member_count=len(members),
                created_at=self.clock(),  # type: ignore[operator]
            )
            snapshot = snapshot.model_copy(
                update={
                    "manifest_digest": projection_snapshot_manifest_digest(
                        snapshot, members
                    )
                }
            )
            result = await uow.projections.assemble_snapshot(
                ProjectionSnapshotCommand.create(
                    snapshot=snapshot,
                    members=members,
                    idempotency_key=IdempotencyKey(
                        f"projection-snapshot-{command.projection_run_id}"
                    ),
                )
            )
            await uow.commit()
            return result.value

    async def _activate(self, snapshot: ProjectionSnapshot, active) -> object:
        published = active if isinstance(active, PublishedActivePublication) else None
        command = ProjectionActivationCommand.create(
            projection_kind="qa",
            projection_snapshot_id=snapshot.projection_snapshot_id,
            activation_kind=ProjectionActivationKind.PUBLISH,
            expected_predecessor_snapshot_id=(
                published.projection_snapshot.projection_snapshot_id
                if published is not None
                else None
            ),
            expected_predecessor_activation_generation=(
                published.activation_generation if published is not None else None
            ),
            expected_active_pointer_cas_version=(
                published.active_pointer_cas_version if published is not None else None
            ),
            command_identity=f"publish-{snapshot.projection_run_id.hex[:24]}",
            idempotency_key=IdempotencyKey(
                f"projection-activation-{snapshot.projection_run_id}"
            ),
        )
        context = self.context_factory(  # type: ignore[operator]
            snapshot.workspace_id,
            snapshot.environment_id,
            command.digest.operation.value,
            snapshot.projection_run_id,
        )
        async with self.uow_factory(context) as uow:  # type: ignore[operator]
            result = await uow.projections.activate(command)
            await uow.commit()
        if not isinstance(result, (ProjectionActivationCommitted, ProjectionActivationReplay)):
            raise ValueError("projection activation was rejected")
        return result.activation


def _publication_id(command: ProjectionCommand, name: str) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        f"spine:projection-publication:v1:{command.projection_run_id}:{name}",
    )


def _publication_attempt_identity(snapshot_id: UUID) -> str:
    """Keep reconciliation attempts unique across successive publications."""

    return f"publication:{snapshot_id.hex}"


def _projection_receipt_context(
    boundary: TrustedContextBoundary,
    service_principal_id: UUID,
    command: ProjectionCommand,
):
    """Issue the exact trusted operation required by ProjectionReceiptCommand."""

    return boundary.worker(
        scope=EnvironmentScope(
            workspace_id=command.workspace_id,
            environment_id=command.environment_id,
        ),
        service_principal_id=service_principal_id,
        purpose=PersistencePurpose("projection.delivery.v1"),
        operation=PROJECTION_RECEIPT_OPERATION,
        trace_id=command.projection_run_id,
    )


@dataclass(slots=True)
class ProductionOutboxWorkerRuntime:
    worker: OutboxWorker
    settings: OutboxWorkerSettings
    runtime_engine: object
    dispatcher_engine: object

    async def run_once(self):
        return await self.worker.dispatch_batch(
            limit=self.settings.batch_size,
            worker_identity=self.settings.identity,
        )

    async def run_forever(self) -> None:
        while True:
            report = await self.run_once()
            if report.claimed == 0:
                await asyncio.sleep(self.settings.poll_interval_seconds)

    async def close(self) -> None:
        await self.runtime_engine.dispose()  # type: ignore[attr-defined]
        await self.dispatcher_engine.dispose()  # type: ignore[attr-defined]


def create_production_outbox_worker_runtime(
    *,
    runtime_database: RuntimeDatabaseSettings | None = None,
    dispatcher_database: DispatcherDatabaseSettings | None = None,
    worker_settings: OutboxWorkerSettings | None = None,
) -> ProductionOutboxWorkerRuntime:
    runtime_db = runtime_database or RuntimeDatabaseSettings()
    dispatcher_db = dispatcher_database or DispatcherDatabaseSettings()
    settings = worker_settings or OutboxWorkerSettings()
    connect_args = {
        "connect_timeout": runtime_db.connect_timeout_seconds,
        "options": POSTGRESQL_SEARCH_PATH_OPTIONS,
    }
    runtime_engine = create_async_engine(
        runtime_db.url.get_secret_value(),
        pool_pre_ping=True,
        connect_args=connect_args,
        hide_parameters=True,
    )
    dispatcher_engine = create_async_engine(
        dispatcher_db.url.get_secret_value(),
        pool_pre_ping=True,
        connect_args={
            "connect_timeout": dispatcher_db.connect_timeout_seconds,
            "options": POSTGRESQL_SEARCH_PATH_OPTIONS,
        },
        hide_parameters=True,
    )
    runtime_sessions = async_sessionmaker(runtime_engine, expire_on_commit=False)
    dispatcher_sessions = async_sessionmaker(dispatcher_engine, expire_on_commit=False)
    boundary = TrustedContextBoundary.create()
    audit_events = AuditEventRegistry.with_default_families()
    diagnostic_events = DiagnosticEventRegistry.with_default_families()
    diagnostic_sink = JsonLoggingDiagnosticSink(
        diagnostic_events,
        logger=logging.getLogger("spine.worker.diagnostics"),
    )
    outbox_events = OutboxEventRegistry()
    register_ingestion_outbox_events(outbox_events)
    persistence = PostgreSQLPersistence(
        session_factory=runtime_sessions,
        context_verifier=boundary,
        outbox_events=outbox_events,
        audit_events=audit_events,
    )

    def context_for_scope(
        workspace_id: UUID,
        environment_id: UUID,
        operation: str,
        trace_id: UUID,
    ):
        return boundary.worker(
            scope=EnvironmentScope(
                workspace_id=workspace_id,
                environment_id=environment_id,
            ),
            service_principal_id=settings.service_principal_id,
            purpose=PersistencePurpose("projection.delivery.v1"),
            operation=PersistenceOperation(operation),
            trace_id=trace_id,
        )

    parsed = PostgreSQLParsedArtifactRepository(
        runtime_sessions,
        context_factory=lambda workspace_id, environment_id: context_for_scope(
            workspace_id,
            environment_id,
            "parsed_artifact",
            uuid4(),
        ),
        context_verifier=boundary,
    )
    resolver = _DurableArtifactResolver(
        uow_factory=persistence.uow_factory,
        parsed_artifacts=parsed,
        context_factory=lambda effect: context_for_scope(
            effect.workspace_id,
            effect.environment_id,
            "processing_receipt",
            uuid4(),
        ),
        processing_context_factory=lambda workspace_id, environment_id, trace_id: context_for_scope(
            workspace_id,
            environment_id,
            "processing_receipt",
            trace_id,
        ),
    )
    physical_store = CogneeChunkProjectionStore(artifact_resolver=resolver)
    projection = CogneeContextProjection(
        effect_store=physical_store,
        retrieval=CogneeStructuredRetrieval(),
    )
    completion = _ProjectionPublicationCompletion(
        uow_factory=persistence.uow_factory,
        context_factory=context_for_scope,
        artifact_resolver=resolver,
        physical_store=physical_store,
        clock=lambda: datetime.now(timezone.utc),
    )
    projection_binding = build_postgresql_cognee_projection_request_binding(
        session_factory=runtime_sessions,
        parsed_artifact_context_factory=lambda workspace_id, environment_id: context_for_scope(
            workspace_id,
            environment_id,
            "parsed_artifact",
            uuid4(),
        ),
        context_verifier=boundary,
        uow_factory=persistence.uow_factory,
        request_context_factory=lambda envelope: context_for_scope(
            envelope.workspace_id,
            envelope.environment_id,
            "projection_request",
            envelope.event_id,
        ),
        projection_context_factory=lambda command: _projection_receipt_context(
            boundary,
            settings.service_principal_id,
            command,
        ),
        projection=projection,
        clock=lambda: datetime.now(timezone.utc),
        completion=completion,
    )
    projection_binding = OutboxConsumerBinding(
        consumer_name=projection_binding.consumer_name,
        consumer=_PreparingProjectionConsumer(
            preparer=_ProjectionRequestPreparer(
                uow_factory=persistence.uow_factory,
                context_factory=context_for_scope,
            ),
            consumer=projection_binding.consumer,
        ),
    )
    routes = OutboxDeliveryRouteRegistry(version="issue508.worker.v1")
    routes.register(
        event_type=CANONICAL_ACCEPTANCE_OUTBOX_EVENT_TYPE,
        schema_version=CANONICAL_ACCEPTANCE_OUTBOX_SCHEMA_VERSION,
        consumer_name="canonical_notification",
    )
    routes.register(
        event_type=PROCESSING_REQUEST_EVENT_TYPE,
        schema_version=PROCESSING_REQUEST_SCHEMA_VERSION,
        consumer_name="processing_notification",
    )
    routes.register(
        event_type="access_policy.initial.activated",
        schema_version=1,
        consumer_name="policy_notification",
    )
    routes.register(
        event_type=PROJECTION_REQUEST_EVENT_TYPE,
        schema_version=PROJECTION_REQUEST_SCHEMA_VERSION,
        consumer_name=PROJECTION_REQUEST_CONSUMER_NAME,
    )
    routes.register(
        event_type=PROJECTION_ACTIVATED_OUTBOX_EVENT_TYPE,
        schema_version=PROJECTION_OUTBOX_SCHEMA_VERSION,
        consumer_name="projection_activation_notification",
    )
    register_ask_admission_delivery_route(routes)
    routes.seal()
    repository = PostgreSQLOutboxDeliveryRepository(
        session_factory=dispatcher_sessions,
        outbox_events=outbox_events,
        delivery_routes=routes,
        audit_events=audit_events,
    )
    acknowledging = _AcknowledgingConsumer()
    temporal_client = _LazyTemporalWorkflowClient(settings)
    worker = create_outbox_worker(
        repository=repository,
        consumer_bindings=(
            OutboxConsumerBinding("canonical_notification", acknowledging),
            OutboxConsumerBinding("processing_notification", acknowledging),
            OutboxConsumerBinding("policy_notification", acknowledging),
            OutboxConsumerBinding("projection_activation_notification", acknowledging),
            projection_binding,
            build_ask_admission_delivery_binding(temporal_client),
            build_ask_cancellation_delivery_binding(temporal_client),
        ),
        audit_events=audit_events,
        diagnostic_events=diagnostic_events,
        diagnostic_sink=diagnostic_sink,
        service_principal_id=settings.service_principal_id,
        clock=lambda: datetime.now(timezone.utc),
        audit_event_id_source=uuid4,
        retry_delay=lambda attempt: timedelta(seconds=min(2**attempt, 30)),
        maximum_retry_delay=timedelta(seconds=30),
        lease_duration=timedelta(seconds=settings.lease_seconds),
        handler_timeout=timedelta(seconds=settings.handler_timeout_seconds),
        maximum_attempts=settings.maximum_attempts,
    )
    return ProductionOutboxWorkerRuntime(
        worker=worker,
        settings=settings,
        runtime_engine=runtime_engine,
        dispatcher_engine=dispatcher_engine,
    )


async def _run() -> None:
    runtime = create_production_outbox_worker_runtime()
    try:
        await runtime.run_forever()
    finally:
        await runtime.close()


def main() -> None:
    asyncio.run(_run())


__all__ = [
    "OutboxWorkerSettings",
    "ProductionOutboxWorkerRuntime",
    "create_production_outbox_worker_runtime",
    "main",
]
