from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
from types import SimpleNamespace
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from spine.application.connector_ingestion import (
    ConnectorIngestionCommand,
    ConnectorIngestionOrchestrator,
    ConnectorIngestionStatus,
    ProviderAuthorizedObservationReader,
    build_connector_canonical_acceptance_command,
)
from spine.application.connections.google import GoogleReadCommand
from spine.application.connections.provider import ProviderReadAdmission
from spine.application.lifecycle import LifecycleAuthorityError, LifecycleEligibilityQuery
from spine.domain.sources.lifecycle import (
    CurrentLifecycleEligibility,
    LifecycleEligibilityStatus,
)
from spine.application.google_sheets_reconciliation import (
    ConnectorGoogleSheetsCanonicalIngestion,
    GoogleSheetsAdmittedPageReader,
    GoogleSheetsReconciliationCommand,
    GoogleSheetsReconciliationCoordinator,
    GoogleSheetsReconciliationScope,
)
from spine.application.yandex_disk_reconciliation import (
    AuthorizedYandexDiskScanner,
    InMemoryYandexDiskReconciliationRepository,
    InMemoryYandexDiskRemovalPolicyAdmission,
    YandexDiskLifecycleSource,
    YandexDiskLifecycleState,
    YandexDiskReconciliationCommand,
    YandexDiskReconciliationCoordinator,
    YandexDiskReconciliationState,
    YandexDiskRemovalPolicy,
)
from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
    WorkspaceScope,
)
from spine.application.persistence.canonical import CanonicalAcceptanceCommand
from spine.application.persistence.idempotency import IdempotencyKey
from spine.application.source_lifecycle import (
    SourceLifecycleCommand,
    SourceLifecycleCoordinator,
    SourceLifecycleKind,
)
from spine.application.persistence.errors import OptimisticConflictError
from spine.application.persistence.outbox import OutboxEventRegistry
from spine.application.persistence.ingestion import IngestionMutationResult
from spine.application.persistence.idempotency import OpaqueResultReference
from spine.application.persistence.google_sheets_sync import (
    GoogleSheetsSyncRunState,
    InMemoryGoogleSheetsSyncRepository,
)
from spine.application.diagnostics import AuditEventRegistry
from spine.connectors import ConnectorCoverage, NormalizedConnectorObservation
from spine.connectors.google_observation_compatibility import (
    google_v1_observation_to_v2,
)
from spine.connectors.observations_v2 import (
    ExternalIdentityAssurance,
    LifecycleObservationRepresentation,
    ProviderLifecycleState,
)
from spine.connectors.google_drive import (
    DriveFileContent,
    DriveFileMetadata,
    GoogleDriveProviderAdapter,
)
from spine.connectors.google_sheets import (
    GoogleSheetsCellData,
    GoogleSheetsProviderAdapter,
    GoogleSheetsRangePage,
    GoogleSheetsReadProfile,
    GoogleSheetsReconciliationProviderAdapter,
    GoogleSheetsRowData,
    GoogleSheetsSnapshot,
)
from spine.connectors.yandex_disk import (
    YandexDiskFileContent,
    YandexDiskProviderAdapter,
    YandexDiskReadProfile,
    YandexDiskResource,
    YandexDiskResourcePage,
    YandexDiskTransientError,
    yandex_disk_capabilities,
)
from spine.connectors.yandex_disk_composition import YandexDiskSourceIdentityResolver
from spine.connectors.bitrix24_disk import (
    Bitrix24DiskFile,
    Bitrix24DiskFileContent,
    Bitrix24DiskFilePage,
    Bitrix24DiskProviderAdapter,
    Bitrix24DiskReadProfile,
    bitrix24_disk_capabilities,
)
from spine.connectors.files.mounted_filesystem import (
    MountedFilesystemObservationReader,
    MountedFilesystemRoot,
)
from spine.domain.ingestion import (
    IngestionItemState,
    IngestionRunState,
    IngestionTargetKind,
    IngestionTransition,
    ProcessingReceipt,
)
from spine.domain.sources.google import (
    GoogleDriveFileKey,
    GoogleRegisterRowKey,
    GoogleSheetCellValue,
    GoogleSheetNumberFormat,
    google_source_identity,
)
from spine.domain.sources import ExternalFileRevisionMetadata, RevisionKind
from spine.domain.connections.google import OpaqueGoogleReference
from spine.domain.connections.provider import (
    OpaqueProviderReference,
    ProviderCapability,
    ProviderCapabilityKind,
    ProviderCapabilityStatus,
    ProviderConnection,
    ProviderConnectionStatus,
    ProviderKind,
)
from spine.domain.connections.bitrix24_disk_scope import (
    Bitrix24DiskMembershipEvidence,
    Bitrix24DiskStorageClass,
)
from spine.domain.common import EnvironmentKind
from spine.domain.workspaces import Environment, Workspace
from spine.infrastructure.persistence.contexts import (
    TrustedContextBoundary,
    create_initial_workspace_bootstrap_authority,
)
from spine.infrastructure.persistence.in_memory import InMemoryPersistence
from spine.infrastructure.object_storage.contracts import (
    IntegrityResult,
    IntegrityStatus,
    ObjectReference,
    ObservedContentIdentity,
    OriginalUploadCommandResult,
    PreWriteRequestDigest,
    WriteReceipt,
)
from spine.application.storage import OriginalUploadOutcome
from tests.yandex_disk_contract import (
    YANDEX_SOURCE_OBJECT_ID,
    YANDEX_SOURCE_REVISION_ID,
)
from tests.application.test_source_lifecycle import (
    _Canonical as _LifecycleCanonical,
    _Sources as _LifecycleSources,
    _Uow as _LifecycleUow,
    _provenance as _lifecycle_provenance,
    _source as _lifecycle_source,
    _tombstone as _lifecycle_tombstone,
)


NOW = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
WORKSPACE = UUID("10000000-0000-4000-8000-000000000001")
ENVIRONMENT = UUID("20000000-0000-4000-8000-000000000001")
CONNECTION = UUID("30000000-0000-4000-8000-000000000001")
SCOPE = UUID("40000000-0000-4000-8000-000000000001")
POLICY = UUID("50000000-0000-4000-8000-000000000001")


def _id(value: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"connector-ingestion-test:{value}")


def _command(key: str = "observation-1") -> ConnectorIngestionCommand:
    return ConnectorIngestionCommand(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        command_identity="connector.observation.1",
        idempotency_key=key,
        connection_id=CONNECTION,
        resource_scope_reference=SCOPE,
        authorization_generation=7,
        configuration_generation=11,
        access_policy_version_id=POLICY,
    )


def _observation(*, content: bytes = b"normalized row\n") -> NormalizedConnectorObservation:
    identity = google_source_identity(
        GoogleRegisterRowKey(
            connection_id=CONNECTION,
            spreadsheet_id="spreadsheet-1",
            sheet_id=17,
            immutable_business_id="estimate-42",
        )
    )
    return NormalizedConnectorObservation(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        connection_id=CONNECTION,
        resource_scope_reference=SCOPE,
        authorization_generation=7,
        configuration_generation=11,
        source_kind="record",
        external_identity=identity,
        original_bytes=content,
        media_type="application/json",
        extraction_profile="google-sheets-row-extraction:v1",
        locator_contract_version="locator:google-sheet-cell:v1",
        coverage=ConnectorCoverage.COMPLETE,
        producer_reference="google-sheets-reader",
        event_identity="sheet-event-42",
        event_digest=hashlib.sha256(b"sheet-event-42").hexdigest(),
        observer_service="connector-worker",
        received_at=NOW,
        observed_at=NOW,
    )


def _drive_observation(*, content: bytes = b"reappeared drive file") -> NormalizedConnectorObservation:
    identity = google_source_identity(
        GoogleDriveFileKey(connection_id=CONNECTION, file_id="drive-file-1")
    )
    digest = hashlib.sha256(content).hexdigest()
    return NormalizedConnectorObservation(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        connection_id=CONNECTION,
        resource_scope_reference=SCOPE,
        authorization_generation=7,
        configuration_generation=11,
        source_kind="document",
        external_identity=identity,
        original_bytes=content,
        media_type="application/pdf",
        extraction_profile="google-drive-download:v1",
        locator_contract_version="locator:google-drive-file:v1",
        coverage=ConnectorCoverage.COMPLETE,
        producer_reference="google-drive-reader",
        event_identity="drive-event-after-tombstone",
        event_digest=digest,
        origin_locator_kind="google-drive-file",
        origin_locator_value="drive-file-1",
        origin_locator_schema="locator:google-drive-file:v1",
        origin_locator_digest=hashlib.sha256(b"drive-file-1").hexdigest(),
        observer_service="connector-worker",
        received_at=NOW,
        observed_at=NOW,
    )


class _Reader:
    def __init__(self, observation: NormalizedConnectorObservation) -> None:
        self.observation = observation
        self.calls = 0
        self.error: Exception | None = None

    async def read(self, command, context):
        del command, context
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.observation


class _SequenceReader:
    def __init__(self, *observations: NormalizedConnectorObservation) -> None:
        self.observations = list(observations)

    async def read(self, command, context):
        del command, context
        return self.observations.pop(0)


class _Uploader:
    def __init__(self) -> None:
        self.calls = 0
        self.fail_next = False

    async def upload(self, context, command, chunks):
        del context
        self.calls += 1
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("storage unavailable")
        content = b"".join([part async for part in chunks])
        digest = __import__("hashlib").sha256(content).hexdigest()
        reference = ObjectReference(
            schema_version=1,
            object_id=_id(f"object:{digest}"),
            storage_generation=_id(f"generation:{digest}"),
            digest_algorithm="sha256",
            digest_hex=digest,
            byte_length=len(content),
        )
        receipt = WriteReceipt(
            schema_version=1,
            upload_id=_id(f"upload:{command.idempotency_key}"),
            object_reference=reference,
            observed_content=ObservedContentIdentity(
                observed_sha256=digest, observed_byte_length=len(content)
            ),
            verification=IntegrityResult(
                status=IntegrityStatus.VERIFIED,
                algorithm="sha256",
                observed_digest=digest,
                observed_length=len(content),
            ),
            storage_status="finalized_verified",
            finalized_at=NOW,
        )
        result = OriginalUploadCommandResult(
            request_digest=PreWriteRequestDigest(
                algorithm="sha256",
                algorithm_version="spine.command-digest.v1",
                digest_hex="a" * 64,
            ),
            upload_id=receipt.upload_id,
            object_id=reference.object_id,
            issue8_result_reference={
                "receipt_id": _id(f"issue8-receipt:{command.idempotency_key}")
            },
        )
        return OriginalUploadOutcome(result=result, write_receipt=receipt, replay=False)


class _Ingestion:
    def __init__(self) -> None:
        self.run = None
        self.item = None
        self.transitions: list[IngestionTransition] = []
        self.binding = None
        self.observation = None
        self.source = None
        self.receipt: ProcessingReceipt | None = None
        self.projection_events = []
        self.revisions = {}

    def _head(self, target_kind, *, item_id=None):
        values = [
            value
            for value in self.transitions
            if value.target_kind is target_kind
            and (
                target_kind is IngestionTargetKind.RUN
                and value.ingestion_run_id == self.run.ingestion_run_id
                or target_kind is IngestionTargetKind.ITEM
                and value.ingestion_run_source_id == item_id
            )
        ]
        return values[-1] if values else None

    async def create_run(self, command):
        replay = self.run is not None
        self.run = self.run or command.run
        if not self.transitions:
            self.transitions.append(
                IngestionTransition(
                    ingestion_transition_id=_id("run-root"),
                    workspace_id=WORKSPACE,
                    environment_id=ENVIRONMENT,
                    target_kind=IngestionTargetKind.RUN,
                    ingestion_run_id=self.run.ingestion_run_id,
                    state=IngestionRunState.REQUESTED,
                    attempt=1,
                    actor_or_service="fake",
                    trace_id=_id("trace"),
                    occurred_at=NOW,
                )
            )
        return SimpleNamespace(value=self.run, replay=replay)

    async def create_item(self, item):
        self.item = self.item or item
        if not any(t.ingestion_run_source_id == item.ingestion_run_source_id for t in self.transitions):
            self.transitions.append(
                IngestionTransition(
                    ingestion_transition_id=_id("item-root"),
                    workspace_id=WORKSPACE,
                    environment_id=ENVIRONMENT,
                    target_kind=IngestionTargetKind.ITEM,
                    ingestion_run_id=item.ingestion_run_id,
                    ingestion_run_source_id=item.ingestion_run_source_id,
                    state=IngestionItemState.RECEIVED,
                    attempt=1,
                    actor_or_service="fake",
                    trace_id=_id("trace"),
                    occurred_at=NOW,
                )
            )
        return self.item

    async def resolve_item(self, item_id):
        return self.item if self.item and self.item.ingestion_run_source_id == item_id else None

    async def resolve_run_head(self, run_id):
        del run_id
        return self._head(IngestionTargetKind.RUN)

    async def resolve_item_head(self, item_id):
        return self._head(IngestionTargetKind.ITEM, item_id=item_id)

    async def append_transition(self, transition):
        self.transitions.append(transition)
        return transition

    async def bind_source(self, command):
        self.source = command.observation.source
        self.binding = command.binding
        self.observation = command.observation
        self.revisions[command.observation.revision.revision_id] = command.observation.revision
        head = await self.resolve_item_head(command.item.ingestion_run_source_id)
        self.transitions.append(
            IngestionTransition(
                ingestion_transition_id=_id("observed"),
                workspace_id=WORKSPACE,
                environment_id=ENVIRONMENT,
                target_kind=IngestionTargetKind.ITEM,
                ingestion_run_id=command.item.ingestion_run_id,
                ingestion_run_source_id=command.item.ingestion_run_source_id,
                state=IngestionItemState.OBSERVED,
                predecessor_transition_id=head.ingestion_transition_id,
                attempt=head.attempt + 1,
                actor_or_service="fake",
                trace_id=_id("trace"),
                occurred_at=NOW,
            )
        )
        return IngestionMutationResult(
            value=self.binding,
            replay=False,
            claim=None,
            result_reference=OpaqueResultReference(
                result_type="ingestion_binding", result_id=self.binding.binding_id, schema_version=1
            ),
        )

    async def resolve_revision(self, revision_id):
        return self.revisions.get(revision_id)

    async def resolve_source(self, source_object_id):
        return self.source if self.source and self.source.source_object_id == source_object_id else None

    async def record_processing_receipt(self, command):
        self.receipt = self.receipt or command.receipt
        return SimpleNamespace(value=self.receipt, replay=self.receipt != command.receipt)


class _Admissions:
    def __init__(self):
        self.decision = None

    async def resolve_effective(self, revision_id):
        del revision_id
        return self.decision

    async def append_decision(self, decision):
        self.decision = decision
        return decision


class _Uow:
    def __init__(self, ingestion, admissions):
        self.ingestion = ingestion
        self.sources = ingestion
        self.admissions = admissions
        self.canonical = SimpleNamespace(
            accept=self._accept, resolve_current=self._resolve_current
        )
        self.outbox = SimpleNamespace(append=self._append)
        self.accepted = 0
        self.current_revision_id = None
        self.current_revision_kind = None
        self.last_accepted_revision = None
        self.events = []
        self.trace_id = _id("uow-trace")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        del args

    async def commit(self):
        return None

    async def rollback(self):
        return None

    async def _accept(self, command):
        self.accepted += 1
        predecessor = self.last_accepted_revision
        self.last_accepted_revision = command.revision.revision_id
        self.current_revision_id = command.revision.revision_id
        self.current_revision_kind = command.revision.kind
        self.sources.revisions[command.revision.revision_id] = command.revision
        return SimpleNamespace(
            advanced=True,
            replay=False,
            acceptance=SimpleNamespace(predecessor_revision_id=predecessor),
        )

    async def _resolve_current(self, source_object_id):
        del source_object_id
        if self.current_revision_id is None:
            return None
        return SimpleNamespace(
            current_revision_id=self.current_revision_id,
            current_revision_kind=self.current_revision_kind,
        )

    async def _append(self, intent):
        self.events.append(intent)
        return intent.event_id

    def _trace_id_snapshot(self):
        return self.trace_id


def _orchestrator(observation=None, *, reader=None, lifecycle_reader=None):
    reader = reader or _Reader(observation)
    uploader = _Uploader()
    ingestion = _Ingestion()
    admissions = _Admissions()
    uow = _Uow(ingestion, admissions)

    def context_factory(operation: PersistenceOperation, trace_id: UUID):
        return SimpleNamespace(operation=operation, trace_id=trace_id, service_principal_id=_id("service"))

    return (
        ConnectorIngestionOrchestrator(
            uow_factory=lambda context: uow,
            observation_reader=reader,
            uploader=uploader,
            read_context_factory=context_factory,
            context_factory=context_factory,
            canonical_command_factory=lambda **kwargs: SimpleNamespace(
                revision=kwargs["revision"],
                lifecycle_query=kwargs["command"].lifecycle_query,
            ),
            outbox_events=OutboxEventRegistry(),
            audit_events=AuditEventRegistry.with_default_families(),
            read_operation=PersistenceOperation("google_connection_read"),
            admission_reference_type="google_read_admission",
            lifecycle_reader=lifecycle_reader,
            clock=lambda: NOW,
        ),
        reader,
        uploader,
        ingestion,
        uow,
    )


@pytest.mark.asyncio
async def test_scope_withdrawal_blocks_connector_before_provider_read():
    class _WithdrawnLifecycleReader:
        async def current_eligibility(self, query, context):
            del query, context
            raise LifecycleAuthorityError()

    observation = _observation()
    lifecycle_query = LifecycleEligibilityQuery(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        source_object_id=_id("withdrawn-source"),
        connection_id=CONNECTION,
        connection_version_id=_id("connection-version"),
        scope_reference=SCOPE,
        scope_version_id=_id("scope-version"),
        scope_digest="a" * 64,
        provider_object_reference="estimate-42",
        source_external_namespace=observation.external_identity.external_namespace,
        source_external_generation=observation.external_identity.external_generation,
        source_external_object_id=observation.external_identity.external_object_id,
    )
    command = _command().model_copy(
        update={
            "resource_object_reference": "estimate-42",
            "lifecycle_query": lifecycle_query,
        }
    )
    orchestrator, reader, uploader, _, _ = _orchestrator(
        observation, lifecycle_reader=_WithdrawnLifecycleReader()
    )

    with pytest.raises(LifecycleAuthorityError):
        await orchestrator.ingest(command)

    assert reader.calls == 0
    assert uploader.calls == 0


class _HandoffSheetsService:
    def __init__(self, snapshots=None) -> None:
        self.snapshots = snapshots
        self.read_count = 0

    async def get_provider_version(
        self, connection, resource_scope_reference, spreadsheet_id
    ):
        del connection, resource_scope_reference, spreadsheet_id
        return "1"

    async def get_spreadsheet(
        self, connection, resource_scope_reference, spreadsheet_id, sheet_id
    ):
        del connection, resource_scope_reference, spreadsheet_id, sheet_id
        if self.snapshots is not None:
            snapshot = self.snapshots[min(self.read_count, len(self.snapshots) - 1)]
            self.read_count += 1
            return snapshot
        return _handoff_snapshot(row_number=2)


class _SheetsHandoffReader:
    def __init__(self, snapshots=None) -> None:
        self.provider = GoogleSheetsProviderAdapter(
            service=_HandoffSheetsService(snapshots),
            profile=GoogleSheetsReadProfile(
                spreadsheet_id="spreadsheet-1",
                sheet_id=17,
                business_id_column="estimate_id",
                columns=(
                    {"column_key": "estimate_id", "coordinate": "A"},
                    {"column_key": "total", "coordinate": "B"},
                ),
            ),
            clock=lambda: NOW,
        )
        self.connection = SimpleNamespace(
            connection_id=CONNECTION,
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
        )

    async def read(self, command, context):
        del context
        observation = await self.provider.read(
            self.connection,
            GoogleReadCommand(
                connection_id=command.connection_id,
                resource_scope_reference=OpaqueGoogleReference(
                    value=command.resource_scope_reference
                ),
                authorization_generation=command.authorization_generation,
                configuration_generation=command.configuration_generation,
            ),
        )
        return observation.to_normalized_observation()


def _handoff_snapshot(
    *, row_number: int, sheet_title: str = "Estimates", formula: str = "=SUM(C2:D2)"
) -> GoogleSheetsSnapshot:
    return GoogleSheetsSnapshot(
        spreadsheet_id="spreadsheet-1",
        sheet_id=17,
        sheet_title=sheet_title,
        locale="ru_RU",
        time_zone="Europe/Moscow",
        rows=(
            GoogleSheetsRowData(
                row_number=row_number,
                a1_range=f"A{row_number}:B{row_number}",
                cells=(
                    GoogleSheetsCellData(
                        column_key="estimate_id",
                        coordinate=f"A{row_number}",
                        entered=GoogleSheetCellValue(
                            kind="string", value="estimate-42"
                        ),
                        effective=GoogleSheetCellValue(
                            kind="string", value="estimate-42"
                        ),
                        formatted="estimate-42",
                        number_format=GoogleSheetNumberFormat(type="TEXT"),
                    ),
                    GoogleSheetsCellData(
                        column_key="total",
                        coordinate=f"B{row_number}",
                        entered=GoogleSheetCellValue(kind="formula", value=formula),
                        effective=GoogleSheetCellValue(
                            kind="number", value="1250.5"
                        ),
                        formatted="1250.50",
                        number_format=GoogleSheetNumberFormat(
                            type="NUMBER", pattern="#,##0.00"
                        ),
                    ),
                ),
            ),
        ),
    )
class _DriveHandoffService:
    def __init__(self) -> None:
        self.metadata = [
            DriveFileMetadata(
                file_id="file-42",
                name="estimate.pdf",
                mime_type="application/pdf",
                version="7",
            ),
            DriveFileMetadata(
                file_id="file-42",
                name="estimate.pdf",
                mime_type="application/pdf",
                version="7",
            ),
            DriveFileMetadata(
                file_id="file-42",
                name="renamed.pdf",
                mime_type="application/pdf",
                version="8",
                parent_ids=("folder-2",),
            ),
            DriveFileMetadata(
                file_id="file-42",
                name="renamed.pdf",
                mime_type="application/pdf",
                version="8",
                parent_ids=("folder-2",),
            ),
            DriveFileMetadata(
                file_id="file-42",
                name="renamed.pdf",
                mime_type="application/pdf",
                version="9",
                parent_ids=("folder-2",),
            ),
            DriveFileMetadata(
                file_id="file-42",
                name="renamed.pdf",
                mime_type="application/pdf",
                version="9",
                parent_ids=("folder-2",),
            ),
        ]
        self.content = [
            DriveFileContent(content=b"v7-bytes", media_type="application/pdf"),
            DriveFileContent(content=b"v7-bytes", media_type="application/pdf"),
            DriveFileContent(content=b"v9-bytes", media_type="application/pdf"),
        ]

    async def get_metadata(self, connection, resource_scope_reference):
        del connection, resource_scope_reference
        return self.metadata.pop(0)

    async def download(self, connection, resource_scope_reference, file_id, version):
        del connection, resource_scope_reference
        assert file_id == "file-42"
        assert version in {"7", "8", "9"}
        return self.content.pop(0)

    async def export(
        self,
        connection,
        resource_scope_reference,
        file_id,
        version,
        export_mime_type,
    ):
        del connection, resource_scope_reference, file_id, version, export_mime_type
        raise AssertionError("export is not used by this handoff")


class _DriveHandoffReader:
    def __init__(self) -> None:
        self.provider = GoogleDriveProviderAdapter(
            service=_DriveHandoffService(),
            clock=lambda: NOW,
        )
        self.connection = SimpleNamespace(
            connection_id=CONNECTION,
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
        )

    async def read(self, command, context):
        del context
        observation = await self.provider.read(
            self.connection,
            GoogleReadCommand(
                connection_id=command.connection_id,
                resource_scope_reference=OpaqueGoogleReference(
                    value=command.resource_scope_reference
                ),
                authorization_generation=command.authorization_generation,
                configuration_generation=command.configuration_generation,
            ),
        )
        return observation.to_normalized_observation()


@pytest.mark.asyncio
async def test_authorized_normalized_observation_reaches_canonical_and_projection_handoffs():
    orchestrator, reader, uploader, ingestion, uow = _orchestrator(_observation())

    result = await orchestrator.ingest(_command())

    assert reader.calls == 1
    assert uploader.calls == 1
    assert result.status is ConnectorIngestionStatus.SUCCEEDED
    assert result.source.identity_mode.value == "connector"
    assert result.source.connection_id == CONNECTION
    assert result.provenance.producer_kind == "connector"
    assert result.processing_receipt.output_kind == "normalized_connector_observation"
    assert result.processing_receipt.output_digest == result.revision.revision_digest
    assert ingestion.observation is not None
    assert ingestion.receipt is result.processing_receipt
    assert len(uow.events) == 1
    assert uow.events[0].event_type == "ingestion.projection.requested"


@pytest.mark.asyncio
async def test_pre_read_normalized_observation_uses_canonical_and_projection_handoffs():
    orchestrator, reader, uploader, ingestion, uow = _orchestrator()

    result = await orchestrator.ingest_observation(_command(), _observation())

    assert reader.calls == 0
    assert uploader.calls == 1
    assert result.status is ConnectorIngestionStatus.SUCCEEDED
    assert result.source.external_object_id == (
        result.observation.external_identity.external_object_id
    )
    assert result.processing_receipt.output_digest == result.revision.revision_digest
    assert ingestion.observation is not None
    assert len(uow.events) == 1
    assert uow.events[0].event_type == "ingestion.projection.requested"


@pytest.mark.asyncio
async def test_lifecycle_only_v2_is_rejected_before_content_ingestion_effects():
    orchestrator, _, uploader, ingestion, uow = _orchestrator()
    observation = google_v1_observation_to_v2(
        _drive_observation(), connection_version_id=_id("google-connection-version")
    ).model_copy(
        update={
            "representation": LifecycleObservationRepresentation(
                state=ProviderLifecycleState.CONFIRMED_DELETED,
                evidence_reference="google-drive-reconciliation:v1",
                evidence_digest="0" * 64,
            )
        }
    )

    with pytest.raises(
        ValueError, match="Lifecycle-only observations cannot enter content ingestion"
    ):
        await orchestrator.ingest_observation(_command(), observation)

    assert ingestion.run is None
    assert ingestion.item is None
    assert ingestion.transitions == []
    assert uploader.calls == 0
    assert uow.accepted == 0
    assert uow.events == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("factory", "expected_assurance"),
    (
        (_observation, ExternalIdentityAssurance.SCOPE_STABLE),
        (_drive_observation, ExternalIdentityAssurance.PROVIDER_STABLE),
    ),
)
async def test_google_v1_and_provider_v2_produce_equivalent_canonical_outcomes(
    factory,
    expected_assurance: ExternalIdentityAssurance,
):
    legacy = factory()
    provider_v2 = google_v1_observation_to_v2(
        legacy, connection_version_id=_id("google-connection-version")
    )
    legacy_orchestrator, _, _, _, legacy_uow = _orchestrator()
    v2_orchestrator, _, _, _, v2_uow = _orchestrator()

    legacy_result = await legacy_orchestrator.ingest_observation(_command(), legacy)
    v2_result = await v2_orchestrator.ingest_observation(_command(), provider_v2)

    assert v2_result.source == legacy_result.source
    assert v2_result.revision == legacy_result.revision
    assert v2_result.provenance == legacy_result.provenance
    assert v2_result.processing_receipt == legacy_result.processing_receipt
    assert v2_uow.events == legacy_uow.events
    assert provider_v2.external_identity.external_object_id == (
        legacy.external_identity.external_object_id
    )
    assert provider_v2.origin_locator_value == legacy.origin_locator_value
    assert provider_v2.external_identity.assurance is expected_assurance


@pytest.mark.asyncio
async def test_yandex_file_replay_keeps_one_immutable_revision_and_projection_effect():
    content = b"%PDF governed Yandex proposal evidence"
    metadata = YandexDiskResource(
        name="brief.pdf",
        path="disk:/approved/brief.pdf",
        type="file",
        mime_type="application/pdf",
        size=len(content),
        md5=hashlib.md5(content).hexdigest(),  # noqa: S324 - provider evidence
        sha256=hashlib.sha256(content).hexdigest(),
        revision="1700000000000000",
        modified=NOW,
        resource_id="1573541:synthetic",
    )

    class Service:
        async def list_resources(self, connection, scope, *, offset, limit):
            del connection, scope
            assert (offset, limit) == (0, 100)
            return YandexDiskResourcePage(
                _embedded={
                    "path": "disk:/approved",
                    "items": [metadata],
                    "limit": 100,
                    "offset": 0,
                    "total": 1,
                }
            )

        async def download(self, connection, scope, *, path, revision, max_bytes):
            del connection, scope
            assert (path, revision) == (metadata.path, metadata.revision)
            assert max_bytes == 32 * 1024 * 1024
            return YandexDiskFileContent(
                content=content, media_type="application/pdf"
            )

        async def get_resource(self, connection, scope, *, path):
            del connection, scope
            assert path == metadata.path
            return metadata

    qualification = OpaqueProviderReference(value=_id("yandex-qualification"))
    connection = ProviderConnection(
        connection_version_id=_id("yandex-connection-version"),
        connection_id=CONNECTION,
        approval_id=_id("yandex-approval"),
        version=1,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        service_principal_id=_id("service"),
        provider_kind=ProviderKind.YANDEX_DISK,
        credential_reference=OpaqueProviderReference(value=_id("yandex-client")),
        grant_reference=OpaqueProviderReference(value=_id("yandex-oauth-grant")),
        approved_resource_scope_references=(OpaqueProviderReference(value=SCOPE),),
        authorization_generation=7,
        configuration_generation=11,
        status=ProviderConnectionStatus.ACTIVE,
        capabilities=yandex_disk_capabilities(
            qualification_reference=qualification
        ),
    )
    provider = YandexDiskProviderAdapter(
        service=Service(),
        profile=YandexDiskReadProfile(),
        clock=lambda: NOW,
        sleep=lambda _: _async_none(),
    )
    orchestrator, _, uploader, ingestion, uow = _orchestrator()

    class Repository:
        @asynccontextmanager
        async def admit_read(self, command, context):
            assert context.operation == PersistenceOperation("provider_connection_read")
            assert command.resource_object_reference == metadata.path
            yield connection

    admitted = replace(
        orchestrator,
        observation_reader=ProviderAuthorizedObservationReader(
            admission=ProviderReadAdmission(
                repository=Repository(),
                provider=provider,
            ),
            connection_version_id=connection.connection_version_id,
            required_capabilities=(
                ProviderCapabilityKind.ENUMERATION,
                ProviderCapabilityKind.METADATA_READ,
                ProviderCapabilityKind.BINARY_READ,
            ),
        ),
        read_operation=PersistenceOperation("provider_connection_read"),
        admission_reference_type="provider_read_admission",
    )
    command = _command("yandex-replay").model_copy(
        update={"resource_object_reference": metadata.path}
    )
    first = await admitted.ingest(command)
    replay = await admitted.ingest(command)
    observation = first.observation

    assert first.revision == replay.revision
    assert first.source.source_object_id == YANDEX_SOURCE_OBJECT_ID
    assert first.revision.revision_id == YANDEX_SOURCE_REVISION_ID
    assert first.revision.original_sha256 == hashlib.sha256(content).hexdigest()
    assert first.revision.byte_length == len(content)
    assert first.revision.media_type == "application/pdf"
    external = first.revision.revision_metadata.external_file_revision
    assert external is not None
    assert external.provider_kind == "yandex_disk"
    assert external.provider_revision == metadata.revision
    assert external.provider_resource_id == metadata.resource_id
    assert external.resource_name == metadata.name
    assert external.resource_path == metadata.path
    assert external.identity_assurance == "path_scoped"
    assert first.provenance.origin_locator_schema == "locator:yandex-disk-path:v1"
    assert replay.replay is True
    assert uploader.calls == 2
    assert uow.accepted == 1
    assert len(uow.events) == 1
    assert uow.events[0].event_type == "ingestion.projection.requested"
    assert ingestion.observation.revision == first.revision
    assert not hasattr(first.source, "proposal_eligible")
    assert not hasattr(first.source, "pricing_basis")

    changed_evidence = observation.representation.revision_evidence.model_copy(
        update={
            "provider_revision": "1700000000000001",
            "modified_at": NOW + timedelta(seconds=1),
        }
    )
    changed_observation = observation.model_copy(
        update={
            "event_identity": "yandex-disk:revision-two",
            "event_digest": "b" * 64,
            "provider_version_before": "1700000000000001",
            "provider_version_after": "1700000000000001",
            "representation": observation.representation.model_copy(
                update={"revision_evidence": changed_evidence}
            ),
        }
    )
    successor = await orchestrator.ingest_observation(
        _command("yandex-revision-two").model_copy(
            update={
                "command_identity": "connector.yandex-revision-two",
                "expected_current_revision_id": first.revision.revision_id,
            }
        ),
        changed_observation,
    )

    assert successor.source == first.source
    assert successor.revision.original_sha256 == first.revision.original_sha256
    assert successor.revision.revision_id != first.revision.revision_id
    assert successor.revision.revision_metadata.external_file_revision == (
        ExternalFileRevisionMetadata(
            provider_kind="yandex_disk",
            provider_revision="1700000000000001",
            provider_resource_id=metadata.resource_id,
            resource_name=metadata.name,
            resource_path=metadata.path,
            content_md5=metadata.md5,
            content_sha256=metadata.sha256,
            byte_length=metadata.size,
            modified_at=NOW + timedelta(seconds=1),
            identity_assurance="path_scoped",
            assurance_evidence_reference="yandex-disk-path:v1",
        )
    )


@pytest.mark.asyncio
async def test_bitrix24_disk_file_replay_reaches_shared_canonical_revision_path():
    content = b"%PDF governed Bitrix24 proposal evidence"
    metadata = Bitrix24DiskFile(
        ID="9043",
        NAME="brief.pdf",
        TYPE="file",
        STORAGE_ID="1357",
        PARENT_ID="8907",
        GLOBAL_CONTENT_VERSION="42",
        FILE_ID="32718",
        SIZE=len(content),
        CREATE_TIME=NOW,
        UPDATE_TIME=NOW,
        DOWNLOAD_URL="https://example.test/download?auth=SIGNED-SECRET",
    )

    class Service:
        async def authorize_read(
            self, connection, scope, *, file_id, acting_principal_id
        ):
            del connection, scope, acting_principal_id
            assert file_id == metadata.id

        async def list_children(
            self, connection, scope, *, start, page_size, acting_principal_id
        ):
            del connection, scope, acting_principal_id
            assert (start, page_size) == (0, 50)
            return Bitrix24DiskFilePage(result=(metadata,), total=1)

        async def get_file(self, connection, scope, *, file_id, acting_principal_id):
            del connection, scope, acting_principal_id
            assert file_id == metadata.id
            return metadata

        async def download(
            self,
            connection,
            scope,
            *,
            file_id,
            download_url,
            max_bytes,
            acting_principal_id,
        ):
            del connection, scope, acting_principal_id
            assert file_id == metadata.id
            assert download_url == metadata.download_url
            assert max_bytes == 32 * 1024 * 1024
            return Bitrix24DiskFileContent(content=content, media_type="application/pdf")

    qualification = OpaqueProviderReference(value=_id("bitrix24-qualification"))
    connection = ProviderConnection(
        connection_version_id=_id("bitrix24-connection-version"),
        connection_id=CONNECTION,
        approval_id=_id("bitrix24-approval"),
        version=1,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        service_principal_id=_id("service"),
        provider_kind=ProviderKind.BITRIX24,
        credential_reference=OpaqueProviderReference(value=_id("bitrix24-client")),
        grant_reference=OpaqueProviderReference(value=_id("bitrix24-grant")),
        approved_resource_scope_references=(OpaqueProviderReference(value=SCOPE),),
        authorization_generation=7,
        configuration_generation=11,
        status=ProviderConnectionStatus.ACTIVE,
        capabilities=bitrix24_disk_capabilities(
            qualification_reference=qualification
        ),
    )
    provider = Bitrix24DiskProviderAdapter(
        service=Service(),
        profile=Bitrix24DiskReadProfile(),
        clock=lambda: NOW,
        sleep=lambda _: _async_none(),
    )
    membership = Bitrix24DiskMembershipEvidence(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        connection_id=CONNECTION,
        connection_version_id=connection.connection_version_id,
        resource_scope_reference=OpaqueProviderReference(value=SCOPE),
        root_policy_id=_id("bitrix24-root-policy"),
        root_policy_version="root-v1",
        root_policy_digest="a" * 64,
        content_policy_id=_id("bitrix24-content-policy"),
        content_policy_version="content-v1",
        content_policy_digest="b" * 64,
        authorization_generation=7,
        configuration_generation=11,
        grant_binding_id=_id("bitrix24-grant-binding"),
        grant_token_epoch=1,
        grant_security_fence=0,
        storage_id="1357",
        storage_class=Bitrix24DiskStorageClass.COMMON,
        storage_root_object_id="1000",
        approved_root_folder_id="8907",
        object_id=metadata.id,
        parent_folder_ids=("8907",),
        provider_revision="42",
    )

    class S1PinnedDiskReader:
        """Supply only the actor resolved by the offline S1 authority fake."""

        async def read(self, admitted_connection, admitted_command):
            return await provider.read(
                admitted_connection,
                replace(
                    admitted_command,
                    acting_subject_id=_id("bitrix24-acting-user"),
                    membership_evidence=membership,
                ),
            )
    orchestrator, _, uploader, ingestion, uow = _orchestrator()

    class Repository:
        @asynccontextmanager
        async def admit_read(self, command, context):
            assert context.operation == PersistenceOperation("provider_connection_read")
            assert command.resource_object_reference == metadata.id
            yield connection

    admitted = replace(
        orchestrator,
        observation_reader=ProviderAuthorizedObservationReader(
            admission=ProviderReadAdmission(
                repository=Repository(),
                provider=S1PinnedDiskReader(),
            ),
            connection_version_id=connection.connection_version_id,
            required_capabilities=(
                ProviderCapabilityKind.ENUMERATION,
                ProviderCapabilityKind.METADATA_READ,
                ProviderCapabilityKind.BINARY_READ,
                ProviderCapabilityKind.STABLE_IDENTITY,
            ),
        ),
        read_operation=PersistenceOperation("provider_connection_read"),
        admission_reference_type="provider_read_admission",
    )
    command = _command("bitrix24-replay").model_copy(
        update={"resource_object_reference": metadata.id}
    )
    first = await admitted.ingest(command)
    replay = await admitted.ingest(command)

    assert first.revision == replay.revision
    assert first.revision.original_sha256 == hashlib.sha256(content).hexdigest()
    external = first.revision.revision_metadata.external_file_revision
    assert external is not None
    assert external.provider_kind == "bitrix24"
    assert external.provider_revision == "42"
    assert external.provider_resource_id == "9043"
    assert external.resource_name == "brief.pdf"
    assert external.provider_payload_digest is not None
    assert external.provider_payload is not None
    assert "DOWNLOAD_URL" not in external.provider_payload
    assert replay.replay is True
    assert uploader.calls == 2
    assert uow.accepted == 1
    assert len(uow.events) == 1
    assert uow.events[0].event_type == "ingestion.projection.requested"
    assert ingestion.observation.revision == first.revision
    assert ingestion.observation.provenance.bitrix24_disk_membership == membership
    assert "SIGNED-SECRET" not in first.revision.model_dump_json()


@pytest.mark.asyncio
async def test_yandex_reconciliation_recovers_through_canonical_ingestion_and_outbox_seam():
    content = b"%PDF recovered governed Yandex evidence"
    metadata = YandexDiskResource(
        name="recovered.pdf",
        path="disk:/approved/recovered.pdf",
        type="file",
        mime_type="application/pdf",
        size=len(content),
        md5=hashlib.md5(content).hexdigest(),  # noqa: S324 - provider evidence
        sha256=hashlib.sha256(content).hexdigest(),
        revision="1700000000000670",
        modified=NOW,
        resource_id="1573541:issue670",
    )

    class Service:
        fail_scan_once = True

        async def list_resources(self, connection, scope, *, offset, limit):
            del connection, scope
            assert (offset, limit) == (0, 100)
            if self.fail_scan_once:
                self.fail_scan_once = False
                raise YandexDiskTransientError()
            return YandexDiskResourcePage(
                _embedded={
                    "path": "disk:/approved",
                    "items": [metadata],
                    "limit": 100,
                    "offset": 0,
                    "total": 1,
                }
            )

        async def download(self, connection, scope, *, path, revision, max_bytes):
            del connection, scope, max_bytes
            assert (path, revision) == (metadata.path, metadata.revision)
            return YandexDiskFileContent(content=content, media_type="application/pdf")

        async def get_resource(self, connection, scope, *, path):
            del connection, scope
            assert path == metadata.path
            return metadata

    connection_version = _id("issue670-connection-version")
    connection = ProviderConnection(
        connection_version_id=connection_version,
        connection_id=CONNECTION,
        approval_id=_id("issue670-approval"),
        version=1,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        service_principal_id=_id("service"),
        provider_kind=ProviderKind.YANDEX_DISK,
        credential_reference=OpaqueProviderReference(value=_id("issue670-client")),
        grant_reference=OpaqueProviderReference(value=_id("issue670-grant")),
        approved_resource_scope_references=(OpaqueProviderReference(value=SCOPE),),
        authorization_generation=7,
        configuration_generation=11,
        status=ProviderConnectionStatus.ACTIVE,
        capabilities=yandex_disk_capabilities(
            qualification_reference=OpaqueProviderReference(
                value=_id("issue670-qualification")
            )
        ),
    )
    provider = YandexDiskProviderAdapter(
        service=Service(),
        profile=YandexDiskReadProfile(max_attempts=1),
        clock=lambda: NOW,
        sleep=lambda _: _async_none(),
    )

    class Repository:
        @asynccontextmanager
        async def admit_read(self, read_command, context):
            del context
            assert read_command.workspace_id == WORKSPACE
            assert read_command.environment_id == ENVIRONMENT
            yield connection

    class LifecycleReader:
        async def current_eligibility(self, query, context):
            del context
            return CurrentLifecycleEligibility(
                workspace_id=query.workspace_id,
                environment_id=query.environment_id,
                source_object_id=query.source_object_id,
                connection_id=query.connection_id,
                connection_version_id=query.connection_version_id,
                scope_reference=query.scope_reference,
                scope_version_id=query.scope_version_id,
                scope_digest=query.scope_digest,
                current_decision_id=_id("issue670-eligible"),
                generation=1,
                status=LifecycleEligibilityStatus.ELIGIBLE,
                legal_hold_active=False,
            )

    lifecycle_reader = LifecycleReader()

    orchestrator, _, _, _, uow = _orchestrator()
    admitted_ingestion = replace(
        orchestrator,
        observation_reader=ProviderAuthorizedObservationReader(
            admission=ProviderReadAdmission(
                repository=Repository(),
                provider=provider,
                lifecycle_reader=lifecycle_reader,
                source_identity_resolvers=(YandexDiskSourceIdentityResolver(),),
            ),
            connection_version_id=connection_version,
            required_capabilities=(
                ProviderCapabilityKind.ENUMERATION,
                ProviderCapabilityKind.METADATA_READ,
                ProviderCapabilityKind.BINARY_READ,
            ),
        ),
        read_operation=PersistenceOperation("provider_connection_read"),
        admission_reference_type="provider_read_admission",
        lifecycle_reader=lifecycle_reader,
    )

    old_source = _lifecycle_source(identity="issue670-old-path")
    tombstone = _lifecycle_tombstone(old_source)
    old_revision_id = _id("issue670-old-current-revision")
    provenance = _lifecycle_provenance(
        old_source, tombstone.revision_id, "issue670-rename-removal"
    )
    lifecycle_sources = _LifecycleSources(
        old_source,
        {tombstone.revision_id: tombstone},
        [],
    )

    class LifecycleCanonical:
        def __init__(self) -> None:
            self.commands = []

        async def accept(self, lifecycle_command):
            self.commands.append(lifecycle_command)
            return SimpleNamespace(
                advanced=True,
                replay=False,
                revision=lifecycle_command.revision,
            )

    lifecycle_canonical = LifecycleCanonical()
    lifecycle_uows = []
    source_lifecycle = SourceLifecycleCoordinator(
        uow_factory=lambda context: lifecycle_uows.append(
            _LifecycleUow(lifecycle_sources, lifecycle_canonical)
        )
        or lifecycle_uows[-1],
        context_factory=lambda lifecycle_command: SimpleNamespace(
            operation=PersistenceOperation("canonical_acceptance")
        ),
    )

    class Tombstones:
        async def compose(self, *, idempotency_key, **kwargs):
            del kwargs
            canonical = CanonicalAcceptanceCommand.create(
                source=old_source,
                revision=tombstone,
                provenance=provenance,
                expected_current_revision_id=old_revision_id,
                idempotency_key=IdempotencyKey(idempotency_key),
                canonical_acceptance_id=_id("issue670-tombstone-acceptance"),
                admission_decision_id=None,
                access_policy_version_id=None,
                audit_event_id=_id("issue670-tombstone-audit"),
                outbox_event_id=_id("issue670-tombstone-outbox"),
                accepted_at=NOW,
            )
            return SourceLifecycleCommand(
                kind=SourceLifecycleKind.DELETION,
                source=old_source,
                revision=tombstone,
                provenance=provenance,
                observation_idempotency_key=IdempotencyKey(idempotency_key),
                canonical_acceptance=canonical,
            )

    class Sources:
        async def list_sources(self, command, context):
            del command, context
            return (
                YandexDiskLifecycleSource(
                    provider_object_reference="disk:/approved/old-name.pdf",
                    source_object_id=old_source.source_object_id,
                    current_revision_id=old_revision_id,
                    current_revision_kind=RevisionKind.CONTENT,
                    current_provider_revision="old-provider-revision",
                ),
            )

    removal_policy = YandexDiskRemovalPolicy(
        policy_version_id=_id("issue670-removal-policy"),
        approval_id=_id("issue670-removal-policy-approval"),
        approved_at=NOW,
        version="yandex-removal-policy:v1",
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        connection_id=CONNECTION,
        resource_scope_reference=SCOPE,
        authorize_complete_scope_absence=True,
    )
    reconciliation_repository = InMemoryYandexDiskReconciliationRepository()
    coordinator = YandexDiskReconciliationCoordinator(
        repository=reconciliation_repository,
        scanner=AuthorizedYandexDiskScanner(
            repository=Repository(),
            provider=provider,
            lifecycle_reader=lifecycle_reader,
        ),
        ingestion=admitted_ingestion,
        sources=Sources(),
        tombstones=Tombstones(),
        source_lifecycle=source_lifecycle,
        removal_policies=InMemoryYandexDiskRemovalPolicyAdmission((removal_policy,)),
        context_factory=lambda operation, identity: SimpleNamespace(
            operation=operation, trace_id=identity, service_principal_id=_id("service")
        ),
        clock=lambda: NOW,
    )
    reconciliation_command = YandexDiskReconciliationCommand(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        connection_id=CONNECTION,
        connection_version_id=connection_version,
        resource_scope_reference=SCOPE,
        service_principal_id=_id("service"),
        authorization_generation=7,
        configuration_generation=11,
        access_policy_version_id=POLICY,
        command_identity="issue670.public-seam",
        idempotency_key="issue670-public-seam",
        lifecycle_query=LifecycleEligibilityQuery(
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            source_object_id=_id("issue670-root-source"),
            connection_id=CONNECTION,
            connection_version_id=connection_version,
            scope_reference=SCOPE,
            scope_version_id=connection.approval_id,
            scope_digest="a" * 64,
            provider_object_reference=str(SCOPE),
            source_external_namespace="yandex-disk",
            source_external_generation="yandex-disk-path:v1",
            source_external_object_id="ydx:" + "a" * 60,
        ),
    )
    interrupted = await coordinator.run(reconciliation_command, removal_policy)
    recovered = await coordinator.run(reconciliation_command, removal_policy)

    assert interrupted.state is YandexDiskReconciliationState.INTERRUPTED
    assert recovered.state is YandexDiskReconciliationState.COMPLETE
    assert [event.state for event in recovered.lifecycle_evidence] == [
        YandexDiskLifecycleState.TEMPORARILY_UNAVAILABLE,
        YandexDiskLifecycleState.OBSERVED,
        YandexDiskLifecycleState.CONFIRMED_REMOVED,
    ]
    accepted = recovered.pages[0].items[0]
    assert accepted.provider_object_reference == metadata.path
    assert uow.sources.revisions[accepted.source_revision_id].original_sha256 == hashlib.sha256(content).hexdigest()
    assert uow.accepted == 1
    assert len(uow.events) == 1
    assert uow.events[0].event_type == "ingestion.projection.requested"
    assert lifecycle_sources.recorded[0].revision == tombstone
    assert lifecycle_canonical.commands[0].outbox_event_id == _id(
        "issue670-tombstone-outbox"
    )
    assert lifecycle_uows[0].committed is True

async def _async_none() -> None:
    return None


@pytest.mark.asyncio
async def test_mounted_filesystem_observation_reaches_canonical_path_with_stable_replay(
    tmp_path,
):
    root = tmp_path / "mounted"
    (root / "guides").mkdir(parents=True)
    (root / "guides" / "safety.md").write_text("Read-only source content.\n")
    connection_version_id = _id("mounted-filesystem-connection-version")
    scope_reference = OpaqueProviderReference(value=SCOPE)
    connection = ProviderConnection(
        connection_version_id=connection_version_id,
        connection_id=CONNECTION,
        approval_id=_id("mounted-filesystem-approval"),
        version=1,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        service_principal_id=_id("service"),
        provider_kind=ProviderKind.MOUNTED_FILESYSTEM,
        credential_reference=OpaqueProviderReference(
            value=_id("mounted-filesystem-credential-reference")
        ),
        grant_reference=OpaqueProviderReference(
            value=_id("mounted-filesystem-grant-reference")
        ),
        approved_resource_scope_references=(scope_reference,),
        authorization_generation=7,
        configuration_generation=11,
        status=ProviderConnectionStatus.ACTIVE,
    )
    qualification = OpaqueProviderReference(
        value=_id("mounted-filesystem-read-qualification")
    )
    connection = connection.model_copy(
        update={
            "capabilities": (
                ProviderCapability(
                    kind=ProviderCapabilityKind.BINARY_READ,
                    status=ProviderCapabilityStatus.SUPPORTED,
                    qualification_reference=qualification,
                ),
            )
        }
    )
    provider = MountedFilesystemObservationReader(
        roots={
            connection_version_id: MountedFilesystemRoot(
                resource_scope_reference=scope_reference,
                path=root,
            )
        },
        clock=lambda: NOW,
    )

    class Repository:
        @asynccontextmanager
        async def admit_read(self, read_command, context):
            del read_command, context
            yield connection

    observation_reader = ProviderAuthorizedObservationReader(
        admission=ProviderReadAdmission(repository=Repository(), provider=provider),
        connection_version_id=connection_version_id,
        required_capabilities=(ProviderCapabilityKind.BINARY_READ,),
    )
    orchestrator, _, uploader, ingestion, uow = _orchestrator(
        reader=observation_reader
    )
    command = _command("mounted-filesystem-ingestion").model_copy(
        update={"resource_object_reference": "guides/safety.md"}
    )

    first = await orchestrator.ingest(command)
    replay = await orchestrator.ingest(command)

    assert first.source.external_generation == (
        f"mounted-path:v2:{connection_version_id.hex}"
    )
    assert first.source.external_object_id == (
        first.observation.external_identity.external_object_id
    )
    assert replay.source == first.source
    assert replay.revision == first.revision
    assert replay.processing_receipt.processing_receipt_id == (
        first.processing_receipt.processing_receipt_id
    )
    assert first.revision.original_sha256 == hashlib.sha256(
        b"Read-only source content.\n"
    ).hexdigest()
    revision_evidence = first.revision.revision_metadata.external_file_revision
    assert revision_evidence is not None
    assert revision_evidence.provider_kind == "mounted_filesystem"
    assert revision_evidence.resource_path == "guides/safety.md"
    assert revision_evidence.identity_assurance == "path_scoped"
    assert uow.accepted == 1
    assert uploader.calls == 2
    assert len(uow.events) == 1
    assert ingestion.receipt == replay.processing_receipt


@pytest.mark.asyncio
async def test_google_sheets_reader_reaches_shared_canonical_and_projection_handoff():
    reader = _SheetsHandoffReader()
    orchestrator, _, uploader, ingestion, uow = _orchestrator(reader=reader)

    result = await orchestrator.ingest(_command())

    assert result.status is ConnectorIngestionStatus.SUCCEEDED
    assert result.source.external_object_id == (
        result.observation.external_identity.external_object_id
    )
    assert result.revision.original_sha256 == hashlib.sha256(
        result.observation.original_bytes
    ).hexdigest()
    assert result.provenance.revision_id == result.revision.revision_id
    assert uow.accepted == 1
    assert uploader.calls == 1
    assert ingestion.observation is not None
    assert len(uow.events) == 1
    assert uow.events[0].event_type == "ingestion.projection.requested"


@pytest.mark.asyncio
async def test_sheets_location_only_observation_skips_revision_projection_intent():
    reader = _SheetsHandoffReader(
        snapshots=(
            _handoff_snapshot(row_number=2),
            _handoff_snapshot(row_number=5, sheet_title="Renamed estimates"),
            _handoff_snapshot(row_number=5, formula="=SUM(C2:E2)"),
        )
    )
    orchestrator, _, _, _, uow = _orchestrator(reader=reader)

    first = await orchestrator.ingest(
        _command("projection-count-1").model_copy(
            update={"command_identity": "connector.projection-count-1"}
        )
    )
    moved = await orchestrator.ingest(
        _command("projection-count-2").model_copy(
            update={
                "command_identity": "connector.projection-count-2",
                "expected_current_revision_id": first.revision.revision_id,
            }
        )
    )
    edited = await orchestrator.ingest(
        _command("projection-count-3").model_copy(
            update={
                "command_identity": "connector.projection-count-3",
                "expected_current_revision_id": moved.revision.revision_id,
            }
        )
    )

    assert first.revision.revision_id == moved.revision.revision_id
    assert edited.revision.revision_id != moved.revision.revision_id
    assert uow.accepted == 2
    assert len(uow.events) == 2
    assert all(
        event.event_type == "ingestion.projection.requested" for event in uow.events
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "cell_update"),
    (
        (
            "formula",
            {"entered": GoogleSheetCellValue(kind="formula", value="=SUM(C2:E2)")},
        ),
        (
            "entered_value",
            {"entered": GoogleSheetCellValue(kind="string", value="1250.5")},
        ),
        (
            "effective_value",
            {"effective": GoogleSheetCellValue(kind="number", value="1251.5")},
        ),
        ("formatted_value", {"formatted": "1 251,50 ₽"}),
        (
            "number_format",
            {
                "number_format": GoogleSheetNumberFormat(
                    type="CURRENCY", pattern="₽ #,##0.00"
                )
            },
        ),
    ),
)
async def test_configured_cell_edit_creates_revision_and_projection_intent(
    case: str, cell_update: dict[str, object]
) -> None:
    original_snapshot = _handoff_snapshot(row_number=2)
    original_row = original_snapshot.rows[0]
    changed_row = original_row.model_copy(
        update={
            "cells": (
                original_row.cells[0],
                original_row.cells[1].model_copy(update=cell_update),
            )
        }
    )
    changed_snapshot = original_snapshot.model_copy(update={"rows": (changed_row,)})
    reader = _SheetsHandoffReader(
        snapshots=(original_snapshot, changed_snapshot)
    )
    orchestrator, _, _, _, uow = _orchestrator(reader=reader)
    before = await orchestrator.ingest(
        _command(f"{case}-before").model_copy(
            update={"command_identity": f"connector.{case}-before"}
        )
    )
    after = await orchestrator.ingest(
        _command(f"{case}-after").model_copy(
            update={
                "command_identity": f"connector.{case}-after",
                "expected_current_revision_id": before.revision.revision_id,
            }
        )
    )

    assert before.revision.revision_id != after.revision.revision_id
    assert after.revision.original_sha256 != before.revision.original_sha256
    assert uow.accepted == 2
    assert len(uow.events) == 2
    assert all(
        event.event_type == "ingestion.projection.requested" for event in uow.events
    )
async def test_successive_drive_observations_keep_metadata_only_changes_out_of_projection():
    reader = _DriveHandoffReader()
    orchestrator, _, _, _, uow = _orchestrator(reader=reader)

    first = await orchestrator.ingest(_command("drive-v7"))
    metadata_only = await orchestrator.ingest(_command("drive-v8"))
    changed = await orchestrator.ingest(_command("drive-v9"))

    assert first.source.source_object_id == metadata_only.source.source_object_id
    assert metadata_only.source.source_object_id == changed.source.source_object_id
    assert first.revision.revision_id == metadata_only.revision.revision_id
    assert changed.revision.revision_id != first.revision.revision_id
    assert first.provenance.provenance_id != metadata_only.provenance.provenance_id
    assert first.provenance.order_token == "gdrive-v:7"
    assert metadata_only.provenance.order_token == "gdrive-v:8"
    assert changed.provenance.order_token == "gdrive-v:9"
    assert len(uow.events) == 2
    assert uow.accepted == 2


@pytest.mark.asyncio
async def test_connector_representation_profile_is_revision_bearing():
    download = _observation(content=b"same bytes").model_copy(
        update={"event_identity": "drive-download-event"}
    )
    export = download.model_copy(
        update={
            "event_identity": "drive-export-event",
            "extraction_profile": "google-drive-export:v1",
            "event_digest": hashlib.sha256(b"drive-export-event").hexdigest(),
        }
    )
    orchestrator, _, _, _, uow = _orchestrator(
        reader=_SequenceReader(download, export)
    )

    first = await orchestrator.ingest(_command("drive-download"))
    second = await orchestrator.ingest(_command("drive-export"))

    assert first.source.source_object_id == second.source.source_object_id
    assert first.revision.revision_id != second.revision.revision_id
    assert len(uow.events) == 2


@pytest.mark.asyncio
async def test_read_denial_stops_before_original_storage():
    orchestrator, reader, uploader, _, _ = _orchestrator(_observation())
    reader.error = RuntimeError("read denied")

    with pytest.raises(RuntimeError, match="read denied"):
        await orchestrator.ingest(_command())

    assert reader.calls == 1
    assert uploader.calls == 0


@pytest.mark.asyncio
async def test_observation_outside_trusted_scope_stops_before_original_storage():
    orchestrator, _, uploader, _, _ = _orchestrator(
        _observation().model_copy(update={"configuration_generation": 12})
    )

    with pytest.raises(ValueError, match="outside the trusted read scope"):
        await orchestrator.ingest(_command())

    assert uploader.calls == 0


@pytest.mark.asyncio
async def test_repeated_observation_reuses_ingestion_effects():
    orchestrator, _, uploader, ingestion, uow = _orchestrator(_observation())

    first = await orchestrator.ingest(_command())
    second = await orchestrator.ingest(_command())

    assert first.revision.revision_id == second.revision.revision_id
    assert first.processing_receipt.processing_receipt_id == second.processing_receipt.processing_receipt_id
    assert uploader.calls == 2
    assert len(uow.events) == 1
    assert ingestion.receipt == second.processing_receipt


@pytest.mark.asyncio
async def test_google_drive_reappearance_records_tombstone_predecessor_before_admission():
    orchestrator, _, _, ingestion, uow = _orchestrator(_drive_observation())
    predecessor = _id("drive-source-tombstone")
    uow.current_revision_id = predecessor
    uow.current_revision_kind = RevisionKind.TOMBSTONE

    result = await orchestrator.ingest(_command("drive-reappearance-1"))

    assert result.revision.reappearance_after_tombstone_revision_id == predecessor
    assert result.revision.revision_id == ingestion.binding.source_revision_id
    assert ingestion.observation.revision == result.revision
    assert (
        ingestion.observation.receipt_association.revision_id
        == result.revision.revision_id
    )


@pytest.mark.asyncio
async def test_google_sheets_reappearance_records_tombstone_predecessor_before_admission():
    orchestrator, _, _, _, uow = _orchestrator(_observation())
    predecessor = _id("sheets-source-tombstone")
    uow.current_revision_id = predecessor
    uow.current_revision_kind = RevisionKind.TOMBSTONE

    result = await orchestrator.ingest(_command("sheets-reappearance"))

    assert result.revision.reappearance_after_tombstone_revision_id == predecessor
    assert result.revision.revision_id == result.binding.source_revision_id
    assert result.provenance.revision_id == result.revision.revision_id
    assert result.revision.source_object_id == result.source.source_object_id
    assert result.source.external_object_id == _observation().external_identity.external_object_id


@pytest.mark.asyncio
async def test_google_sheets_reappearance_reuses_source_after_absence_tombstone():
    orchestrator, _, _, _, uow = _orchestrator(_observation())
    original = await orchestrator.ingest(_command("sheets-before-absence"))
    tombstone_revision_id = _id("sheets-absence-tombstone")
    uow.current_revision_id = tombstone_revision_id
    uow.current_revision_kind = RevisionKind.TOMBSTONE

    reappeared = await orchestrator.ingest(_command("sheets-after-absence"))

    assert reappeared.source == original.source
    assert reappeared.revision.source_object_id == original.source.source_object_id
    assert reappeared.revision.reappearance_after_tombstone_revision_id == tombstone_revision_id


@pytest.mark.asyncio
async def test_google_drive_reappearance_replay_keeps_the_accepted_lineage():
    orchestrator, _, _, _, uow = _orchestrator(_drive_observation())
    predecessor = _id("drive-source-tombstone-replay")
    uow.current_revision_id = predecessor
    uow.current_revision_kind = RevisionKind.TOMBSTONE

    first = await orchestrator.ingest(_command("drive-reappearance-replay"))
    replay = await orchestrator.ingest(_command("drive-reappearance-replay"))

    assert replay.revision.revision_id == first.revision.revision_id
    assert replay.revision.reappearance_after_tombstone_revision_id == predecessor
    assert replay.canonical_acceptance is None
    assert uow.accepted == 1


@pytest.mark.asyncio
async def test_location_only_observation_rejects_a_stale_expected_source_head():
    orchestrator, _, _, _, _ = _orchestrator(_observation())
    await orchestrator.ingest(_command("current-observation"))

    stale = _command("stale-observation").model_copy(
        update={
            "command_identity": "connector.stale-observation",
            "expected_current_revision_id": _id("stale-revision"),
        }
    )

    with pytest.raises(OptimisticConflictError, match="source head is stale"):
        await orchestrator.ingest(stale)


class _EligiblePolicyRepository:
    async def resolve_current(self, source_object_id, *, workspace_id, environment_id):
        del source_object_id, workspace_id, environment_id
        return SimpleNamespace(access_policy_version_id=POLICY)

    async def resolve_policy(self, policy_id, *, workspace_id, environment_id):
        del workspace_id, environment_id
        if policy_id != POLICY:
            return None
        return SimpleNamespace(
            allows_service_processing=lambda principal, purpose, operation, now: (
                principal == _id("service")
                and purpose == "connector.ingestion.v1"
                and operation == "canonical_acceptance"
            )
        )


@pytest.mark.asyncio
async def test_handoff_uses_real_in_memory_source_admission_canonical_and_ledger_repositories():
    boundary = TrustedContextBoundary.for_testing(
        issuer_id=_id("issuer"), secret=b"connector-in-memory-secret-32-bytes"
    )
    events = OutboxEventRegistry()
    audits = AuditEventRegistry.with_default_families()
    authority = create_initial_workspace_bootstrap_authority()
    persistence = InMemoryPersistence(
        context_verifier=boundary,
        outbox_events=events,
        audit_events=audits,
        bootstrap_authority=authority,
        canonical_policy_repository=_EligiblePolicyRepository(),
        canonical_clock=lambda: NOW,
    )
    await persistence.initial_workspace_bootstrap.create_initial_workspace(
        authority, Workspace(id=WORKSPACE, slug="connector", display_name="Connector")
    )
    async with persistence.uow_factory(
        boundary.worker(
            scope=WorkspaceScope(workspace_id=WORKSPACE),
            service_principal_id=_id("service"),
            purpose=PersistencePurpose("connector.ingestion.v1"),
            operation=PersistenceOperation("environment_repository"),
            trace_id=_id("environment-trace"),
        )
    ) as uow:
        await uow.environments.add(
            Environment(
                id=ENVIRONMENT,
                workspace_id=WORKSPACE,
                kind=EnvironmentKind.DEVELOPMENT,
                display_name="dev",
            )
        )
        await uow.commit()

    reader = _SheetsHandoffReader(
        snapshots=(
            _handoff_snapshot(row_number=2),
            _handoff_snapshot(row_number=5, sheet_title="Renamed estimates"),
            _handoff_snapshot(row_number=5, formula="=SUM(C2:E2)"),
        )
    )
    uploader = _Uploader()
    orchestrator = ConnectorIngestionOrchestrator(
        uow_factory=persistence.uow_factory,
        observation_reader=reader,
        uploader=uploader,
        read_context_factory=lambda operation, trace_id: boundary.worker(
            scope=EnvironmentScope(workspace_id=WORKSPACE, environment_id=ENVIRONMENT),
            service_principal_id=_id("service"),
            purpose=PersistencePurpose("connector.ingestion.v1"),
            operation=operation,
            trace_id=trace_id,
        ),
        context_factory=lambda operation, trace_id: boundary.worker(
            scope=EnvironmentScope(workspace_id=WORKSPACE, environment_id=ENVIRONMENT),
            service_principal_id=_id("service"),
            purpose=PersistencePurpose("connector.ingestion.v1"),
            operation=operation,
            trace_id=trace_id,
        ),
        canonical_command_factory=lambda **values: build_connector_canonical_acceptance_command(
            outbox_events=events,
            audit_events=audits,
            **values,
        ),
        outbox_events=events,
        audit_events=audits,
        read_operation=PersistenceOperation("google_connection_read"),
        admission_reference_type="google_read_admission",
        clock=lambda: NOW,
    )

    first_command = _command("sheets-observation-1").model_copy(
        update={"command_identity": "connector.sheets-observation-1"}
    )
    first = await orchestrator.ingest(first_command)
    provider_v2 = google_v1_observation_to_v2(
        first.observation,
        connection_version_id=_id("provider-v2-connection-version"),
    )

    class ProviderConnectionRepository:
        def __init__(self):
            self.contexts = []

        @asynccontextmanager
        async def admit_read(self, command, context):
            del command
            self.contexts.append(context)
            yield provider_connection

    class Provider:
        def __init__(self):
            self.calls = 0

        async def read(self, connection, command):
            del connection, command
            self.calls += 1
            return provider_v2

    qualification = OpaqueProviderReference(value=_id("provider-v2-qualification"))
    provider_connection = ProviderConnection(
        connection_version_id=provider_v2.connection_version_id,
        connection_id=CONNECTION,
        approval_id=_id("provider-v2-approval"),
        version=1,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        service_principal_id=_id("service"),
        provider_kind=ProviderKind.GOOGLE,
        credential_reference=OpaqueProviderReference(
            value=_id("provider-v2-credential")
        ),
        grant_reference=OpaqueProviderReference(value=_id("provider-v2-grant")),
        approved_resource_scope_references=(
            OpaqueProviderReference(value=SCOPE),
        ),
        authorization_generation=7,
        configuration_generation=11,
        status=ProviderConnectionStatus.ACTIVE,
        capabilities=(
            ProviderCapability(
                kind=ProviderCapabilityKind.TABULAR_READ,
                status=ProviderCapabilityStatus.SUPPORTED,
                qualification_reference=qualification,
            ),
            ProviderCapability(
                kind=ProviderCapabilityKind.STABLE_IDENTITY,
                status=ProviderCapabilityStatus.SUPPORTED,
                qualification_reference=qualification,
            ),
        ),
    )
    provider = Provider()
    provider_connection_repository = ProviderConnectionRepository()
    admitted_v2_orchestrator = replace(
        orchestrator,
        observation_reader=ProviderAuthorizedObservationReader(
            admission=ProviderReadAdmission(
                repository=provider_connection_repository,
                provider=provider,
            ),
            connection_version_id=provider_v2.connection_version_id,
            required_capabilities=(ProviderCapabilityKind.TABULAR_READ,),
        ),
        read_operation=PersistenceOperation("provider_connection_read"),
        admission_reference_type="provider_read_admission",
    )
    compatibility_replay = await admitted_v2_orchestrator.ingest(first_command)

    moved = await orchestrator.ingest(
        _command("sheets-observation-2").model_copy(
            update={
                "command_identity": "connector.sheets-observation-2",
                "expected_current_revision_id": first.revision.revision_id,
            }
        )
    )
    edited = await orchestrator.ingest(
        _command("sheets-observation-3").model_copy(
            update={
                "command_identity": "connector.sheets-observation-3",
                "expected_current_revision_id": moved.revision.revision_id,
            }
        )
    )

    async with persistence.uow_factory(
        boundary.worker(
            scope=EnvironmentScope(workspace_id=WORKSPACE, environment_id=ENVIRONMENT),
            service_principal_id=_id("service"),
            purpose=PersistencePurpose("connector.ingestion.v1"),
            operation=PersistenceOperation("source_observation"),
            trace_id=_id("readback"),
        )
    ) as uow:
        current = await uow.canonical.resolve_current(first.source.source_object_id)
        revisions = await uow.sources.list_revisions(
            first.source.source_object_id, limit=10
        )
        location_provenance = await uow.sources.list_provenance(
            first.revision.revision_id
        )
        state = await uow.ingestion.derive_run_state(edited.run.ingestion_run_id)
        moved_state = await uow.ingestion.derive_run_state(moved.run.ingestion_run_id)
        await uow.rollback()

    assert current is not None
    assert provider_v2.external_identity.assurance is (
        ExternalIdentityAssurance.SCOPE_STABLE
    )
    assert provider.calls == 1
    assert len(provider_connection_repository.contexts) == 1
    assert provider_connection_repository.contexts[0].operation == (
        PersistenceOperation("provider_connection_read")
    )
    assert compatibility_replay.replay is True
    assert compatibility_replay.run == first.run
    assert compatibility_replay.item == first.item
    assert compatibility_replay.source == first.source
    assert compatibility_replay.revision == first.revision
    assert compatibility_replay.provenance == first.provenance
    assert compatibility_replay.processing_receipt == first.processing_receipt
    assert compatibility_replay.canonical_acceptance is None
    assert first.revision.revision_id == moved.revision.revision_id
    assert edited.revision.revision_id != moved.revision.revision_id
    assert current.current_revision_id == edited.revision.revision_id
    assert current.accepted_generation == 2
    assert len(revisions) == 2
    assert {item.origin_locator_value for item in location_provenance} == {
        "sheet-row:2:a1:a2:b2",
        "sheet-row:5:a1:a5:b5",
    }
    assert len({item.origin_locator_digest for item in location_provenance}) == 2
    assert state is not None
    assert state.state is IngestionRunState.SUCCEEDED
    assert moved_state is not None
    assert moved_state.state is IngestionRunState.SUCCEEDED


@pytest.mark.asyncio
async def test_google_reconciliation_composes_fake_provider_through_persisted_canonical_outbox():
    class RecordingOutboxEventRegistry(OutboxEventRegistry):
        def __init__(self):
            super().__init__()
            self.built_intents = []

        def build_intent(self, **values):
            intent = super().build_intent(**values)
            self.built_intents.append(intent)
            return intent

    class FakeGoogleSheetsService:
        def __init__(self):
            self.page_reads = []

        async def get_spreadsheet_range_page(
            self,
            connection,
            resource_scope_reference,
            spreadsheet_id,
            sheet_id,
            a1_range,
            page_token,
        ):
            del connection, resource_scope_reference
            self.page_reads.append((spreadsheet_id, sheet_id, a1_range, page_token))
            return GoogleSheetsRangePage(
                spreadsheet_id=spreadsheet_id,
                sheet_id=sheet_id,
                a1_range=a1_range,
                page_token=page_token,
                next_page_token=None,
                complete=True,
                provider_version_before="sheet-version-1",
                provider_version_after="sheet-version-1",
                snapshot=_handoff_snapshot(row_number=2),
            )

    boundary = TrustedContextBoundary.for_testing(
        issuer_id=_id("google-composed-issuer"),
        secret=b"google-composed-qualification-secret",
    )
    events = RecordingOutboxEventRegistry()
    audits = AuditEventRegistry.with_default_families()
    authority = create_initial_workspace_bootstrap_authority()
    persistence = InMemoryPersistence(
        context_verifier=boundary,
        outbox_events=events,
        audit_events=audits,
        bootstrap_authority=authority,
        canonical_policy_repository=_EligiblePolicyRepository(),
        canonical_clock=lambda: NOW,
    )
    await persistence.initial_workspace_bootstrap.create_initial_workspace(
        authority, Workspace(id=WORKSPACE, slug="google-composed", display_name="Google")
    )
    async with persistence.uow_factory(
        boundary.worker(
            scope=WorkspaceScope(workspace_id=WORKSPACE),
            service_principal_id=_id("service"),
            purpose=PersistencePurpose("connector.ingestion.v1"),
            operation=PersistenceOperation("environment_repository"),
            trace_id=_id("google-composed-environment"),
        )
    ) as uow:
        await uow.environments.add(
            Environment(
                id=ENVIRONMENT,
                workspace_id=WORKSPACE,
                kind=EnvironmentKind.DEVELOPMENT,
                display_name="dev",
            )
        )
        await uow.commit()

    def trusted_context(operation, trace_id):
        return boundary.worker(
            scope=EnvironmentScope(workspace_id=WORKSPACE, environment_id=ENVIRONMENT),
            service_principal_id=_id("service"),
            purpose=PersistencePurpose("connector.ingestion.v1"),
            operation=operation,
            trace_id=trace_id,
        )

    orchestrator = ConnectorIngestionOrchestrator(
        uow_factory=persistence.uow_factory,
        observation_reader=_SheetsHandoffReader(),
        uploader=_Uploader(),
        read_context_factory=trusted_context,
        context_factory=trusted_context,
        canonical_command_factory=lambda **values: build_connector_canonical_acceptance_command(
            outbox_events=events,
            audit_events=audits,
            **values,
        ),
        outbox_events=events,
        audit_events=audits,
        read_operation=PersistenceOperation("google_connection_read"),
        admission_reference_type="google_read_admission",
        clock=lambda: NOW,
    )

    service = FakeGoogleSheetsService()
    connection = SimpleNamespace(
        connection_id=CONNECTION,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
    )
    provider = GoogleSheetsReconciliationProviderAdapter(
        service=service,
        profiles=(
            GoogleSheetsReadProfile(
                spreadsheet_id="spreadsheet-1",
                sheet_id=17,
                business_id_column="estimate_id",
                columns=(
                    {"column_key": "estimate_id", "coordinate": "A"},
                    {"column_key": "total", "coordinate": "B"},
                ),
            ),
        ),
        clock=lambda: NOW,
    )

    class AdmittedFakeProvider:
        async def read(self, request, context):
            del context
            return await provider.read(connection, request)

    coordinator = GoogleSheetsReconciliationCoordinator(
        repository=InMemoryGoogleSheetsSyncRepository(),
        reader=GoogleSheetsAdmittedPageReader(AdmittedFakeProvider()),
        ingestion=ConnectorGoogleSheetsCanonicalIngestion(
            orchestrator=orchestrator,
            base_command=_command("google-composed-base"),
        ),
        context_factory=lambda operation, trace_id: SimpleNamespace(
            operation=operation, trace_id=trace_id
        ),
        clock=lambda: NOW,
    )
    request = GoogleSheetsReconciliationCommand(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        connection_id=CONNECTION,
        resource_scope_reference=SCOPE,
        service_principal_id=_id("service"),
        authorization_generation=7,
        configuration_generation=11,
        access_policy_version_id=POLICY,
        command_identity="qualification.google.sheets",
        idempotency_key="google-composed-run-1",
        prior_committed_cursor="cursor-1",
        proposed_cursor="cursor-2",
        trigger="scheduled_full",
        scopes=(
            GoogleSheetsReconciliationScope(
                spreadsheet_id="spreadsheet-1",
                sheet_id=17,
                a1_range="A2:B500",
            ),
        ),
    )

    result = await coordinator.run(request)
    replay = await coordinator.run(request)

    assert result.state is GoogleSheetsSyncRunState.SUCCEEDED
    assert result.accepted_rows == 1
    assert result.committed_cursor == "cursor-2"
    assert replay.state is GoogleSheetsSyncRunState.SUCCEEDED
    assert replay.sync_run_id == result.sync_run_id
    assert service.page_reads == [("spreadsheet-1", 17, "A2:B500", None)]
    revision_id = result.pages[0].items[0].revision_id
    assert revision_id is not None

    async with persistence.uow_factory(
        trusted_context(PersistenceOperation("source_observation"), _id("google-readback"))
    ) as uow:
        revision = await uow.sources.resolve_revision(revision_id)
        assert revision is not None
        source_object = await uow.sources.resolve_source(revision.source_object_id)
        current = await uow.canonical.resolve_current(revision.source_object_id)
        await uow.rollback()

    assert source_object is not None
    assert current is not None
    assert current.current_revision_id == revision_id
    assert current.accepted_generation == 1
    projection_intents = [
        intent
        for intent in events.built_intents
        if intent.event_type == "ingestion.projection.requested"
    ]
    assert len(projection_intents) == 1
    assert projection_intents[0].environment_id == ENVIRONMENT
    await persistence.delivery_repository.materialize(
        limit=100,
        materialized_at=NOW,
    )
    claims = await persistence.delivery_repository.claim_due(
        limit=100,
        worker_identity="google_connector_qualification",
        claimed_at=NOW,
        lease_duration=timedelta(minutes=1),
    )
    persisted_projection_intents = tuple(
        claim.intent
        for claim in claims
        if claim.intent.event_type == "ingestion.projection.requested"
    )
    assert len(persisted_projection_intents) == 1
    assert persisted_projection_intents[0].event_id == projection_intents[0].event_id
    assert persisted_projection_intents[0].payload_snapshot is not None
