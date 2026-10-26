from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from types import SimpleNamespace
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from spine.application.connector_ingestion import (
    ConnectorIngestionCommand,
    ConnectorIngestionOrchestrator,
    ConnectorIngestionStatus,
    build_connector_canonical_acceptance_command,
)
from spine.application.connections.google import GoogleReadCommand
from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
    WorkspaceScope,
)
from spine.application.persistence.errors import OptimisticConflictError
from spine.application.persistence.outbox import OutboxEventRegistry
from spine.application.persistence.ingestion import IngestionMutationResult
from spine.application.persistence.idempotency import OpaqueResultReference
from spine.application.diagnostics import AuditEventRegistry
from spine.connectors import ConnectorCoverage, NormalizedConnectorObservation
from spine.connectors.google_sheets import (
    GoogleSheetsCellData,
    GoogleSheetsProviderAdapter,
    GoogleSheetsReadProfile,
    GoogleSheetsRowData,
    GoogleSheetsSnapshot,
)
from spine.domain.ingestion import (
    IngestionItemState,
    IngestionRunState,
    IngestionTargetKind,
    IngestionTransition,
    ProcessingReceipt,
)
from spine.domain.sources.google import (
    GoogleRegisterRowKey,
    GoogleSheetCellValue,
    GoogleSheetNumberFormat,
    google_source_identity,
)
from spine.domain.connections.google import OpaqueGoogleReference
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
    import hashlib

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
        self.current_revision_id = command.revision.revision_id
        return command

    async def _resolve_current(self, source_object_id):
        del source_object_id
        if self.current_revision_id is None:
            return None
        return SimpleNamespace(current_revision_id=self.current_revision_id)

    async def _append(self, intent):
        self.events.append(intent)
        return intent.event_id

    def _trace_id_snapshot(self):
        return self.trace_id


def _orchestrator(observation=None, *, reader=None):
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
                revision=kwargs["revision"]
            ),
            outbox_events=OutboxEventRegistry(),
            audit_events=AuditEventRegistry.with_default_families(),
            clock=lambda: NOW,
        ),
        reader,
        uploader,
        ingestion,
        uow,
    )


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
        del connection, resource_scope_reference
        if self.snapshots is not None:
            snapshot = self.snapshots[min(self.read_count, len(self.snapshots) - 1)]
            self.read_count += 1
            return snapshot
        return GoogleSheetsSnapshot(
            spreadsheet_id=spreadsheet_id,
            sheet_id=sheet_id,
            sheet_title="Estimates",
            locale="ru_RU",
            time_zone="Europe/Moscow",
            rows=(
                GoogleSheetsRowData(
                    row_number=2,
                    a1_range="A2:B2",
                    cells=(
                        GoogleSheetsCellData(
                            column_key="estimate_id",
                            coordinate="A2",
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
                            coordinate="B2",
                            entered=GoogleSheetCellValue(
                                kind="formula", value="=SUM(C2:D2)"
                            ),
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
        clock=lambda: NOW,
    )

    first = await orchestrator.ingest(
        _command("sheets-observation-1").model_copy(
            update={"command_identity": "connector.sheets-observation-1"}
        )
    )
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
