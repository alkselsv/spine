from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest

from spine.connectors.google_api import (
    GoogleApiEndpoints,
    GoogleApiRequest,
    GoogleApiRequestPolicy,
    GoogleApiResponse,
    GoogleDesktopOAuthMaterial,
    GoogleResourceScope,
    HttpxGoogleApiTransport,
)
from spine.connectors.google_live_qualification import (
    GoogleLiveExecutionAuthorization,
    GoogleLiveCredentialEnvironment,
    GoogleLiveFileTarget,
    GoogleLiveQualificationConfig,
    GoogleLiveQualificationRefused,
    GoogleLiveQualificationReport,
    GoogleLiveQualificationReportV4,
    GoogleLiveQualificationProvider,
    GoogleLiveQualificationRunner,
    GoogleLiveProbeCounts,
    GoogleLiveSheetTarget,
    GoogleSharedDriveApproval,
    GoogleSharedDrivePreflightBinding,
    compose_google_live_qualification_runner,
    write_google_live_report,
)
from spine.connectors.google_live_composition import (
    GoogleLiveQualificationBindings,
    make_google_live_qualification_factory,
)
from spine.scripts import google_live_qualification
from spine.application.connections.google import (
    GoogleConnectionDeniedError,
    GoogleProviderReadError,
    GoogleReadCommand,
)
from spine.connectors.google_execution_envelope import (
    GoogleExecutionDeadlineExceeded,
    GoogleExecutionEnvelope,
)
from spine.connectors.google_sheets import (
    GoogleSheetsColumn,
    GoogleSheetsRangePage,
    GoogleSheetsReadProfile,
    GoogleSheetsSnapshot,
)
from spine.application.diagnostics import DiagnosticEventRegistry
from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
    TrustedContextProvenance,
    TrustedPersistenceContext,
)
from spine.domain.common import ContextOrigin
from spine.infrastructure.diagnostics import RecordingDiagnosticSink
from spine.infrastructure.private_qualification_artifacts import (
    FilePrivateQualificationArtifactWriter,
)
from spine.domain.connections.google import (
    GoogleConnection,
    GoogleConnectionStatus,
    OpaqueGoogleReference,
)


def _id(value: int) -> UUID:
    return UUID(f"00000000-0000-0000-0000-{value:012d}")


def _trusted_context() -> TrustedPersistenceContext:
    return TrustedPersistenceContext(
        scope=EnvironmentScope(_id(4), _id(5)),
        origin=ContextOrigin.INTERACTIVE,
        purpose=PersistencePurpose("google_qualification"),
        operation=PersistenceOperation("google_live_read"),
        trace_id=_id(12),
        acting_subject_id=_id(13),
        service_principal_id=None,
        provenance=TrustedContextProvenance(_id(14), b"synthetic-trusted-context"),
    )


SCOPE = OpaqueGoogleReference(value=_id(10))


def _connection() -> GoogleConnection:
    return GoogleConnection(
        connection_version_id=_id(1),
        connection_id=_id(2),
        approval_id=_id(3),
        version=1,
        workspace_id=_id(4),
        environment_id=_id(5),
        service_principal_id=_id(6),
        credential_reference=OpaqueGoogleReference(value=_id(7)),
        grant_reference=OpaqueGoogleReference(value=_id(8)),
        approved_resource_scope_references=(SCOPE,),
        authorization_generation=9,
        configuration_generation=11,
        status=GoogleConnectionStatus.ACTIVE,
    )


def _config(*, opt_in: bool) -> GoogleLiveQualificationConfig:
    return GoogleLiveQualificationConfig(
        opt_in=opt_in,
        connection=_connection(),
        resource_scope=GoogleResourceScope(
            reference=SCOPE,
            workspace_id=_connection().workspace_id,
            environment_id=_connection().environment_id,
            connection_id=_connection().connection_id,
            authorization_generation=9,
            configuration_generation=11,
            allowed_file_ids=("synthetic-file",),
        ),
        endpoints=GoogleApiEndpoints(
            drive_base_url="https://www.googleapis.test/drive/v3",
            sheets_base_url="https://sheets.googleapis.test/v4",
            oauth_token_url="https://oauth2.googleapis.test/token",
            approved_oauth_scopes=(
                "https://www.googleapis.com/auth/drive.readonly",
                "https://www.googleapis.com/auth/spreadsheets.readonly",
            ),
            provider_api_version="google-drive:v3+google-sheets:v4",
            configuration_version="approved-live-config:v1",
        ),
        request_policy=GoogleApiRequestPolicy(
            timeout_seconds=5.0,
            max_attempts=1,
            max_requests_per_run=500,
            max_concurrent_requests=2,
            backoff_ceiling_seconds=(),
            drive_page_size=10,
            sheets_page_size=20,
        ),
        credential_environment=GoogleLiveCredentialEnvironment(
            client_id="GOOGLE_TEST_CLIENT_ID",
            client_secret="GOOGLE_TEST_CLIENT_SECRET",
            refresh_token="GOOGLE_TEST_REFRESH_TOKEN",
        ),
        file_targets=(
            GoogleLiveFileTarget(
                file_id="synthetic-file", download=False, export_mime_types=()
            ),
        ),
        shared_drive_approved=False,
        execution_envelope=GoogleExecutionEnvelope(
            max_run_duration_seconds=30.0,
            max_response_body_bytes=1024,
            max_drive_content_bytes=512,
            max_aggregate_response_bytes=4096,
            max_drive_file_targets=2,
            max_exports=2,
            max_spreadsheets=2,
            max_numeric_sheets=4,
            max_aggregate_rows=100,
            max_api_pages=20,
            max_configured_ranges=8,
        ),
    )


def _valid_report() -> GoogleLiveQualificationReport:
    return GoogleLiveQualificationReport(
        status="passed",
        provider_api_version="google-drive:v3+google-sheets:v4",
        configuration_version="approved-live-config:v1",
        authorization_generation=9,
        configuration_generation=11,
        metadata_reads=1,
        downloads=0,
        exports=0,
        snapshot_pages=0,
        change_pages=0,
        sheet_pages=0,
        oauth_request_attempts=1,
        drive_request_attempts=1,
        sheets_request_attempts=0,
        configured_max_attempts=1,
        shared_drive_status="NOT_EXERCISED",
        failure_stage=None,
    )


def test_v4_report_remains_readable_without_v5_identity_codes() -> None:
    payload = _valid_report().model_dump(mode="json")
    payload["schema_version"] = "google-live-qualification-report:v4"

    report = GoogleLiveQualificationReportV4.model_validate(payload)

    assert report.schema_version == "google-live-qualification-report:v4"
    assert report.status == "passed"


def test_v4_report_rejects_v5_identity_code() -> None:
    payload = _valid_report().model_dump(mode="json")
    payload.update(
        {
            "schema_version": "google-live-qualification-report:v4",
            "status": "failed",
            "failure_stage": "sheets",
            "failure_class": "contract",
            "retry_disposition": "not_retryable",
            "failure_code": "provider_sheets_identity_missing",
        }
    )

    with pytest.raises(ValueError):
        GoogleLiveQualificationReportV4.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("failure_stage", "drive"),
        ("failure_class", "policy"),
        ("retry_disposition", "not_applicable"),
    ),
)
def test_v5_identity_code_requires_consistent_sheets_contract_evidence(
    field: str,
    value: str,
) -> None:
    payload = _valid_report().model_dump(mode="json")
    payload.update(
        {
            "status": "failed",
            "failure_stage": "sheets",
            "failure_class": "contract",
            "retry_disposition": "not_retryable",
            "failure_code": "provider_sheets_identity_missing",
            field: value,
        }
    )

    with pytest.raises(ValueError):
        GoogleLiveQualificationReport.model_validate(payload)


class RecordingAdmission:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def read(self, command, context):
        del context
        self.calls.append(str(command.connection_id))
        return GoogleLiveProbeCounts(metadata_reads=1)


class CancelledAdmission(RecordingAdmission):
    async def read(self, command, context):
        await super().read(command, context)
        raise asyncio.CancelledError


class ApprovedEnvelopeVerifier:
    def __init__(self, approved: bool = True) -> None:
        self.approved = approved
        self.fingerprints: list[str] = []

    def is_approved(self, fingerprint: str) -> bool:
        self.fingerprints.append(fingerprint)
        return self.approved


class RecordingService:
    def __init__(self, configuration_fingerprint: str) -> None:
        self.calls = []
        self.configuration_fingerprint = configuration_fingerprint

    async def get_metadata_for_file(self, connection, scope, file_id):
        del connection, scope
        self.calls.append(file_id)
        return SimpleNamespace(version="1")


class CurrentConnectionRepository:
    def __init__(self, current: GoogleConnection) -> None:
        self.current = current
        self.commands = []
        self.suspensions = []

    @asynccontextmanager
    async def admit_read(self, command, context):
        del context
        self.commands.append(command)
        yield self.current

    async def suspend_revoked(self, connection, context, *, occurred_at):
        self.suspensions.append((connection, context, occurred_at))


def _runner(
    config: GoogleLiveQualificationConfig, admission, preflight=None
) -> GoogleLiveQualificationRunner:
    return GoogleLiveQualificationRunner(
        config=config,
        admission=admission,  # type: ignore[arg-type]
        context=object(),  # type: ignore[arg-type]
        approval_verifier=ApprovedEnvelopeVerifier(),
        shared_drive_preflight=preflight,
    )


class StaticCredentialResolver:
    async def resolve(self, credential_reference, grant_reference):
        del credential_reference, grant_reference
        return GoogleDesktopOAuthMaterial(
            client_id="synthetic-client",
            client_secret="synthetic-client-secret",
            refresh_token="synthetic-refresh-token",
        )


class NoOpRetryObserver:
    async def record_retry(self, event):
        del event


class QueuedResponseTransport:
    def __init__(self, responses: list[GoogleApiResponse]) -> None:
        self.responses = responses

    async def request(self, request):
        del request
        return self.responses.pop(0)


class RecordingQueuedResponseTransport(QueuedResponseTransport):
    def __init__(self, responses: list[GoogleApiResponse]) -> None:
        super().__init__(responses)
        self.requests: list[GoogleApiRequest] = []

    async def request(self, request: GoogleApiRequest) -> GoogleApiResponse:
        self.requests.append(request)
        return await super().request(request)


class OnePageSheetsService:
    def __init__(
        self, configuration_fingerprint: str, page: GoogleSheetsRangePage
    ) -> None:
        self.configuration_fingerprint = configuration_fingerprint
        self.page = page
        self.transport = QueuedResponseTransport([])

    @property
    def sleep(self):
        async def no_sleep(delay: float) -> None:
            del delay

        return no_sleep

    @contextmanager
    def using_run_request_budget(self):
        yield object()

    @contextmanager
    def using_transport(self, transport, *, sleep=None):
        del transport, sleep
        yield

    async def get_spreadsheet_range_page(
        self,
        connection,
        resource_scope_reference,
        spreadsheet_id,
        sheet_id,
        a1_range,
        page_token,
    ) -> GoogleSheetsRangePage:
        del (
            connection,
            resource_scope_reference,
            spreadsheet_id,
            sheet_id,
            a1_range,
            page_token,
        )
        return self.page


def _composed_runner(
    config: GoogleLiveQualificationConfig,
    transport,
    *,
    credential_resolver=None,
    retry_observer=None,
    diagnostic_sink=None,
) -> GoogleLiveQualificationRunner:
    registry = DiagnosticEventRegistry.with_default_families()
    return GoogleLiveQualificationBindings(
        credential_resolver=(
            credential_resolver or StaticCredentialResolver()
        ),  # type: ignore[arg-type]
        transport=transport,
        retry_observer=retry_observer or NoOpRetryObserver(),  # type: ignore[arg-type]
        connection_repository=CurrentConnectionRepository(config.connection),  # type: ignore[arg-type]
        diagnostic_sink=diagnostic_sink or RecordingDiagnosticSink(registry),
        diagnostic_registry=registry,
        clock=lambda: datetime(2026, 10, 29, tzinfo=timezone.utc),
        sleep=lambda delay: asyncio.sleep(0),
        context=_trusted_context(),
        approval_verifier=ApprovedEnvelopeVerifier(),  # type: ignore[arg-type]
    ).build_runner(config)


def _shared_drive_config() -> GoogleLiveQualificationConfig:
    config = _config(opt_in=True)
    scope = config.resource_scope.model_copy(
        update={"drive_id": "synthetic-drive", "supports_all_drives": True}
    )
    return config.model_copy(
        update={"resource_scope": scope, "shared_drive_approved": True}
    )


class RecordingSharedDrivePreflight:
    def __init__(self, *, approved: bool, authorized: bool, mismatch: bool = False):
        self.approved = approved
        self.authorized = authorized
        self.mismatch = mismatch
        self.approval_bindings = []
        self.authorization_bindings = []

    async def resolve_approval(
        self, binding: GoogleSharedDrivePreflightBinding
    ) -> GoogleSharedDriveApproval | None:
        self.approval_bindings.append(binding)
        if not self.approved:
            return None
        returned_binding = binding
        if self.mismatch:
            returned_binding = binding.model_copy(
                update={"shared_drive_id": "another-drive"}
            )
        return GoogleSharedDriveApproval(
            binding=returned_binding,
            approved=True,
            decision_reference="synthetic-decision-v1",
        )

    async def resolve_execution_authorization(
        self, binding: GoogleSharedDrivePreflightBinding
    ) -> GoogleLiveExecutionAuthorization | None:
        self.authorization_bindings.append(binding)
        if not self.authorized:
            return None
        return GoogleLiveExecutionAuthorization(
            binding=binding,
            authorization_reference="synthetic-execution-auth-v1",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("config_opt_in", "environment_opt_in"), ((False, False), (True, False))
)
async def test_live_harness_refuses_before_provider_calls_without_double_opt_in(
    config_opt_in: bool, environment_opt_in: bool
) -> None:
    admission = RecordingAdmission()

    with pytest.raises(GoogleLiveQualificationRefused):
        await _runner(_config(opt_in=config_opt_in), admission).run(
            environment_opt_in=environment_opt_in
        )

    assert admission.calls == []


@pytest.mark.asyncio
async def test_live_harness_refuses_unapproved_envelope_before_provider_calls() -> None:
    admission = RecordingAdmission()
    runner = GoogleLiveQualificationRunner(
        config=_config(opt_in=True),
        admission=admission,  # type: ignore[arg-type]
        context=object(),  # type: ignore[arg-type]
        approval_verifier=ApprovedEnvelopeVerifier(approved=False),
    )

    with pytest.raises(GoogleLiveQualificationRefused):
        await runner.run(environment_opt_in=True)

    assert admission.calls == []


@pytest.mark.asyncio
async def test_live_harness_returns_typed_cancelled_outcome() -> None:
    admission = CancelledAdmission()
    report = await _runner(_config(opt_in=True), admission).run(environment_opt_in=True)

    assert report.status == "failed"
    assert report.failure_code == "cancelled"
    assert report.failure_class == "cancelled"
    assert report.retry_disposition == "not_applicable"
    assert len(admission.calls) == 1


def _expire_provider_run(service):
    async def expire(awaitable, timeout: float):
        del timeout
        task = asyncio.ensure_future(awaitable)
        await service.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        raise asyncio.TimeoutError

    return expire


@pytest.mark.asyncio
async def test_live_harness_deadline_cancels_provider_and_starts_no_later_calls() -> (
    None
):
    config = _config(opt_in=True)

    class BlockingService:
        configuration_fingerprint = config.api_configuration_fingerprint
        transport = object()
        calls: list[str] = []
        started = asyncio.Event()
        cancelled = asyncio.Event()

        @property
        def sleep(self):
            async def no_sleep(delay: float) -> None:
                del delay

            return no_sleep

        @contextmanager
        def using_transport(self, transport, *, sleep=None):
            del transport, sleep
            yield

        @contextmanager
        def using_run_request_budget(self):
            yield object()

        async def get_metadata_for_file(self, connection, scope, file_id):
            del connection, scope
            self.calls.append(file_id)
            self.started.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled.set()

    service = BlockingService()
    provider = GoogleLiveQualificationProvider(
        config=config,
        service=service,  # type: ignore[arg-type]
        monotonic=lambda: 0.0,
        wait_for=_expire_provider_run(service),
    )

    class ProviderAdmission:
        async def read(self, command, context):
            del context
            return await provider.read(config.connection, command)

    runner = GoogleLiveQualificationRunner(
        config=config,
        admission=ProviderAdmission(),  # type: ignore[arg-type]
        context=object(),  # type: ignore[arg-type]
        approval_verifier=ApprovedEnvelopeVerifier(),
    )

    report = await runner.run(environment_opt_in=True)

    assert report.failure_code == "deadline_exceeded"
    assert report.failure_class == "deadline"
    assert report.retry_disposition == "stopped_by_deadline"
    assert service.calls == ["synthetic-file"]
    assert service.cancelled.is_set()


@pytest.mark.asyncio
async def test_live_harness_deadline_includes_admission_and_cancels_it() -> None:
    config = _config(opt_in=True)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class BlockingAdmission:
        async def read(self, command, context):
            del command, context
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    async def expire_admission(awaitable, timeout: float):
        assert timeout == config.execution_envelope.max_run_duration_seconds
        task = asyncio.ensure_future(awaitable)
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        raise asyncio.TimeoutError

    runner = GoogleLiveQualificationRunner(
        config=config,
        admission=BlockingAdmission(),  # type: ignore[arg-type]
        context=object(),  # type: ignore[arg-type]
        approval_verifier=ApprovedEnvelopeVerifier(),
        monotonic=lambda: 0.0,
        wait_for=expire_admission,
    )

    report = await runner.run(environment_opt_in=True)

    assert report.failure_code == "deadline_exceeded"
    assert report.failure_class == "deadline"
    assert report.retry_disposition == "stopped_by_deadline"
    assert cancelled.is_set()


def _sheet_target(
    spreadsheet_id: str, sheet_id: int, ranges: tuple[str, ...] = ("A1:A2",)
) -> GoogleLiveSheetTarget:
    return GoogleLiveSheetTarget(
        profile=GoogleSheetsReadProfile(
            spreadsheet_id=spreadsheet_id,
            sheet_id=sheet_id,
            business_id_column="business_id",
            columns=(GoogleSheetsColumn(column_key="business_id", coordinate="A"),),
        ),
        a1_ranges=ranges,
    )


def _sheet_identity_response(
    spreadsheet_id: str, sheet_id: int, title: str
) -> GoogleApiResponse:
    return GoogleApiResponse(
        status_code=200,
        json_body={
            "spreadsheetId": spreadsheet_id,
            "sheets": [
                {"properties": {"sheetId": sheet_id, "title": title}},
            ],
        },
    )


@pytest.mark.asyncio
async def test_offline_composition_reports_numeric_tab_mismatch_safely() -> None:
    config = _config(opt_in=True)
    config = config.model_copy(
        update={
            "resource_scope": config.resource_scope.model_copy(
                update={
                    "allowed_file_ids": (),
                    "allowed_spreadsheet_ids": ("synthetic-sheet",),
                }
            ),
            "file_targets": (),
            "sheet_targets": (
                _sheet_target("synthetic-sheet", 1, ("'Synthetic'!A1:A2",)),
            ),
        }
    )
    responses = [
        GoogleApiResponse(
            status_code=200,
            json_body={"access_token": "synthetic-token", "expires_in": 3600},
        ),
        _sheet_identity_response("synthetic-sheet", 2, "Synthetic"),
    ]

    report = await _composed_runner(
        config, QueuedResponseTransport(responses)
    ).run(environment_opt_in=True)

    assert report.status == "failed"
    assert report.schema_version == "google-live-qualification-report:v5"
    assert report.failure_code == "provider_sheets_identity_id_mismatch"
    assert report.failure_stage == "sheets"
    assert report.sheets_request_attempts == 1
    assert report.drive_request_attempts == 0
    assert report.sheet_pages == 0
    assert "synthetic-sheet" not in report.model_dump_json()
    assert "Synthetic" not in report.model_dump_json()
    assert responses == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "resource_kind", ("files", "exports", "spreadsheets", "numeric_sheets", "ranges")
)
async def test_live_harness_rejects_configured_resource_caps_before_admission(
    resource_kind: str,
) -> None:
    config = _config(opt_in=True)
    envelope = config.execution_envelope
    updates: dict[str, object] = {}
    if resource_kind == "files":
        updates["file_targets"] = (
            *config.file_targets,
            GoogleLiveFileTarget(file_id="another-file", download=False),
        )
        envelope = envelope.model_copy(update={"max_drive_file_targets": 1})
    elif resource_kind == "exports":
        updates["file_targets"] = (
            GoogleLiveFileTarget(
                file_id="synthetic-file",
                download=False,
                export_mime_types=("application/pdf", "text/csv"),
            ),
        )
        envelope = envelope.model_copy(update={"max_exports": 1})
    elif resource_kind == "spreadsheets":
        updates["sheet_targets"] = (
            _sheet_target("sheet-a", 1),
            _sheet_target("sheet-b", 1),
        )
        envelope = envelope.model_copy(update={"max_spreadsheets": 1})
    elif resource_kind == "numeric_sheets":
        updates["sheet_targets"] = (
            _sheet_target("sheet-a", 1),
            _sheet_target("sheet-a", 2),
        )
        envelope = envelope.model_copy(update={"max_numeric_sheets": 1})
    else:
        updates["sheet_targets"] = (_sheet_target("sheet-a", 1, ("A1:A2", "A3:A4")),)
        envelope = envelope.model_copy(update={"max_configured_ranges": 1})
    updates["execution_envelope"] = envelope
    over_limit_config = config.model_copy(update=updates)
    admission = RecordingAdmission()

    report = await _runner(over_limit_config, admission).run(environment_opt_in=True)

    assert report.status == "failed"
    assert report.failure_code == "resource_limit_exceeded"
    assert report.failure_stage == "harness"
    assert report.oauth_request_attempts == 0
    assert report.drive_request_attempts == 0
    assert report.sheets_request_attempts == 0
    assert admission.calls == []


@pytest.mark.asyncio
async def test_live_harness_writes_only_sanitized_versioned_evidence(
    tmp_path: Path,
) -> None:
    admission = RecordingAdmission()
    runner = _runner(_config(opt_in=True), admission)

    report = await runner.run(environment_opt_in=True)
    report_path = tmp_path / "google-live-report.json"
    write_google_live_report(
        report_path,
        report,
        artifact_writer=FilePrivateQualificationArtifactWriter(),
    )
    serialized = report_path.read_text(encoding="utf-8")

    assert report.status == "passed"
    assert report.metadata_reads == 1
    assert report.provider_api_version == "google-drive:v3+google-sheets:v4"
    assert report.configuration_version == "approved-live-config:v1"
    assert report.shared_drive_status == "NOT_EXERCISED"
    assert admission.calls == [str(_connection().connection_id)]
    for forbidden in (
        "client-secret",
        "refresh-token",
        "access_token",
        '"source_content":',
        '"raw_error":',
    ):
        assert forbidden not in serialized.lower()


def test_live_report_serializes_exact_versioned_json(tmp_path: Path) -> None:
    report = _valid_report()
    report_path = tmp_path / "google-live-report.json"

    write_google_live_report(
        report_path,
        report,
        artifact_writer=FilePrivateQualificationArtifactWriter(),
    )

    serialized = report_path.read_text(encoding="utf-8")
    assert json.loads(serialized) == report.model_dump(mode="json")
    assert serialized.endswith("}\n")
    assert not serialized.endswith("}\n\n")


def test_live_config_rejects_targets_that_do_not_equal_exact_allowlist() -> None:
    payload = _config(opt_in=True).model_dump()
    payload["resource_scope"]["allowed_file_ids"] = ("different-file",)
    with pytest.raises(ValueError, match="exact file allowlist"):
        GoogleLiveQualificationConfig.model_validate(payload)


@pytest.mark.parametrize("evidence_mask", range(15))
def test_live_report_rejects_every_partial_failed_evidence_combination(
    evidence_mask: int,
) -> None:
    valid = _valid_report().model_dump()
    evidence = {
        "failure_stage": "drive",
        "failure_class": "transport",
        "retry_disposition": "not_retryable",
        "failure_code": "provider_unavailable",
    }
    valid["status"] = "failed"
    for index, (field, value) in enumerate(evidence.items()):
        valid[field] = value if evidence_mask & (1 << index) else None

    with pytest.raises(ValueError, match="failure evidence"):
        GoogleLiveQualificationReport.model_validate(valid)


@pytest.mark.parametrize(
    "evidence_field",
    ("failure_stage", "failure_class", "retry_disposition", "failure_code"),
)
def test_live_report_rejects_passed_status_with_failure_evidence(
    evidence_field: str,
) -> None:
    valid = _valid_report().model_dump()
    valid[evidence_field] = {
        "failure_stage": "drive",
        "failure_class": "transport",
        "retry_disposition": "not_retryable",
        "failure_code": "provider_unavailable",
    }[evidence_field]

    with pytest.raises(ValueError, match="failure evidence"):
        GoogleLiveQualificationReport.model_validate(valid)


def test_live_effective_fingerprint_includes_every_envelope_field() -> None:
    config = _config(opt_in=True)
    changed = config.model_copy(
        update={
            "execution_envelope": config.execution_envelope.model_copy(
                update={"max_aggregate_rows": 101}
            )
        }
    )

    assert (
        config.effective_configuration_fingerprint
        != changed.effective_configuration_fingerprint
    )


@pytest.mark.asyncio
async def test_live_harness_rejects_noncurrent_connection_before_provider_calls() -> (
    None
):
    config = _config(opt_in=True)
    current = config.connection.model_copy(
        update={
            "connection_version_id": _id(101),
            "version": 2,
            "predecessor_version_id": config.connection.connection_version_id,
        }
    )
    repository = CurrentConnectionRepository(current)
    service = RecordingService(config.api_configuration_fingerprint)
    registry = DiagnosticEventRegistry.with_default_families()
    runner = compose_google_live_qualification_runner(
        config=config,
        service=service,  # type: ignore[arg-type]
        repository=repository,  # type: ignore[arg-type]
        diagnostic_sink=RecordingDiagnosticSink(registry),
        diagnostic_registry=registry,
        clock=lambda: datetime(2026, 10, 29, tzinfo=timezone.utc),
        context=object(),  # type: ignore[arg-type]
        approval_verifier=ApprovedEnvelopeVerifier(),
    )

    report = await runner.run(environment_opt_in=True)

    assert report.status == "failed"
    assert report.failure_code == "provider_contract_rejected"
    assert report.failure_class == "contract"
    assert report.retry_disposition == "not_retryable"
    assert len(repository.commands) == 1
    assert service.calls == []


@pytest.mark.asyncio
async def test_live_harness_rejects_mismatched_effective_configuration() -> None:
    config = _config(opt_in=True)
    repository = CurrentConnectionRepository(config.connection)
    service = RecordingService("different-effective-configuration")
    registry = DiagnosticEventRegistry.with_default_families()
    runner = compose_google_live_qualification_runner(
        config=config,
        service=service,  # type: ignore[arg-type]
        repository=repository,  # type: ignore[arg-type]
        diagnostic_sink=RecordingDiagnosticSink(registry),
        diagnostic_registry=registry,
        clock=lambda: datetime(2026, 10, 29, tzinfo=timezone.utc),
        context=object(),  # type: ignore[arg-type]
        approval_verifier=ApprovedEnvelopeVerifier(),
    )

    report = await runner.run(environment_opt_in=True)

    assert report.status == "failed"
    assert report.failure_code == "provider_contract_rejected"
    assert service.calls == []


@pytest.mark.asyncio
async def test_shared_drive_boolean_does_not_replace_external_preflight() -> None:
    admission = RecordingAdmission()

    with pytest.raises(GoogleLiveQualificationRefused, match="externally approved"):
        await _runner(_shared_drive_config(), admission).run(environment_opt_in=True)

    assert admission.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("approved", "authorized", "mismatch"),
    ((False, True, False), (True, False, False), (True, True, True)),
)
async def test_shared_drive_preflight_denies_missing_or_mismatched_binding_before_provider(
    approved: bool, authorized: bool, mismatch: bool
) -> None:
    admission = RecordingAdmission()
    preflight = RecordingSharedDrivePreflight(
        approved=approved, authorized=authorized, mismatch=mismatch
    )

    with pytest.raises(GoogleLiveQualificationRefused):
        await _runner(_shared_drive_config(), admission, preflight).run(
            environment_opt_in=True
        )

    assert admission.calls == []
    if approved:
        assert len(preflight.approval_bindings) == 1
        assert preflight.approval_bindings[0].workspace_id == _connection().workspace_id
        assert (
            preflight.approval_bindings[0].environment_id
            == _connection().environment_id
        )
        assert preflight.approval_bindings[0].shared_drive_id == "synthetic-drive"
        assert preflight.approval_bindings[0].allowed_file_ids == ("synthetic-file",)
        assert len(preflight.authorization_bindings) == (0 if mismatch else 1)
    else:
        assert preflight.authorization_bindings == []


@pytest.mark.asyncio
async def test_shared_drive_fake_run_keeps_status_not_exercised() -> None:
    admission = RecordingAdmission()
    preflight = RecordingSharedDrivePreflight(approved=True, authorized=True)

    report = await _runner(_shared_drive_config(), admission, preflight).run(
        environment_opt_in=True
    )

    assert report.status == "passed"
    assert report.shared_drive_status == "NOT_EXERCISED"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stale_field",
    ("connection_version", "authorization_generation", "configuration_generation"),
)
async def test_offline_composition_fails_stale_admission_closed(
    stale_field: str,
) -> None:
    config = _config(opt_in=True)
    if stale_field == "connection_version":
        updates = {
            "connection_version_id": _id(101),
            "version": 2,
            "predecessor_version_id": config.connection.connection_version_id,
        }
    else:
        updates = {
            stale_field: getattr(config.connection, stale_field) + 1,
        }
    stale_connection = config.connection.model_copy(update=updates)
    repository = CurrentConnectionRepository(stale_connection)
    resolver_calls: list[tuple[object, object]] = []
    transport_calls: list[object] = []

    class Resolver:
        async def resolve(self, credential_reference, grant_reference):
            resolver_calls.append((credential_reference, grant_reference))
            raise AssertionError("stale admission must not resolve credentials")

    class Transport:
        async def request(self, request):
            transport_calls.append(request)
            raise AssertionError("stale admission must not issue provider requests")

    class RetryObserver:
        async def record_retry(self, event):
            raise AssertionError("no retry is expected")

    registry = DiagnosticEventRegistry.with_default_families()
    bindings = GoogleLiveQualificationBindings(
        credential_resolver=Resolver(),  # type: ignore[arg-type]
        transport=Transport(),  # type: ignore[arg-type]
        retry_observer=RetryObserver(),  # type: ignore[arg-type]
        connection_repository=repository,  # type: ignore[arg-type]
        diagnostic_sink=RecordingDiagnosticSink(registry),
        diagnostic_registry=registry,
        clock=lambda: datetime(2026, 10, 29, tzinfo=timezone.utc),
        sleep=lambda delay: asyncio.sleep(0),
        context=_trusted_context(),
        approval_verifier=ApprovedEnvelopeVerifier(),  # type: ignore[arg-type]
    )

    runner = bindings.build_runner(config)
    deployment_factory = make_google_live_qualification_factory(bindings)
    with pytest.raises(GoogleLiveQualificationRefused, match="unsupported"):
        replace(bindings, credential_resolver=object()).build_runner(config)  # type: ignore[arg-type]
    report = await runner.run(environment_opt_in=True)

    assert runner.config is config
    assert deployment_factory(config).config is config
    assert report.failure_code == "provider_contract_rejected"
    assert len(repository.commands) == 1
    assert resolver_calls == []
    assert transport_calls == []


@pytest.mark.parametrize("mismatched_scope", ("workspace", "environment"))
def test_offline_composition_rejects_mismatched_context_scope_before_runner_creation(
    mismatched_scope: str,
) -> None:
    config = _config(opt_in=True)
    registry = DiagnosticEventRegistry.with_default_families()
    context = _trusted_context()
    mismatched_context = replace(
        context,
        scope=EnvironmentScope(
            (
                _id(104)
                if mismatched_scope == "workspace"
                else config.connection.workspace_id
            ),
            (
                _id(105)
                if mismatched_scope == "environment"
                else config.connection.environment_id
            ),
        ),
    )
    bindings = GoogleLiveQualificationBindings(
        credential_resolver=type("Resolver", (), {"resolve": lambda *_: None})(),
        transport=type("Transport", (), {"request": lambda *_: None})(),
        retry_observer=type("RetryObserver", (), {"record_retry": lambda *_: None})(),
        connection_repository=type("Repository", (), {"admit_read": lambda *_: None})(),
        diagnostic_sink=RecordingDiagnosticSink(registry),
        diagnostic_registry=registry,
        clock=lambda: datetime(2026, 10, 29, tzinfo=timezone.utc),
        sleep=lambda delay: asyncio.sleep(0),
        context=mismatched_context,
        approval_verifier=ApprovedEnvelopeVerifier(),  # type: ignore[arg-type]
    )

    with pytest.raises(GoogleLiveQualificationRefused, match="context"):
        bindings.build_runner(config)


@pytest.mark.asyncio
async def test_offline_composition_records_oauth_transport_failure_attempt() -> None:
    config = _config(opt_in=True)

    class Transport:
        async def request(self, request):
            raise TimeoutError(
                "synthetic-refresh-token provider-payload-sentinel "
                f"unavailable at {request.url}"
            )

    class RetryObserver:
        async def record_retry(self, event):
            raise AssertionError(f"OAuth transport failure must not retry: {event!r}")

    runner = _composed_runner(config, Transport(), retry_observer=RetryObserver())

    report = await runner.run(environment_opt_in=True)

    assert report.schema_version == "google-live-qualification-report:v5"
    assert report.status == "failed"
    assert report.failure_code == "provider_unavailable"
    assert report.failure_stage == "oauth_refresh"
    assert report.failure_class == "timeout"
    assert report.retry_disposition == "not_retryable"
    assert report.configured_max_attempts == 1
    assert report.oauth_request_attempts == 1
    assert report.drive_request_attempts == 0
    assert report.sheets_request_attempts == 0
    assert report.metadata_reads == 0


@pytest.mark.asyncio
async def test_offline_composition_records_closed_drive_download_policy_reason() -> None:
    config = _config(opt_in=True)
    config = config.model_copy(
        update={
            "file_targets": (
                config.file_targets[0].model_copy(update={"download": True}),
            )
        }
    )
    responses = [
        GoogleApiResponse(
            status_code=200,
            json_body={"access_token": "synthetic-token", "expires_in": 3600},
        ),
        GoogleApiResponse(
            status_code=200,
            json_body={
                "id": "synthetic-file",
                "name": "Synthetic file.pdf",
                "mimeType": "application/pdf",
                "version": "1",
            },
        ),
        GoogleApiResponse(
            status_code=403,
            json_body={
                "error": {
                    "status": "PERMISSION_DENIED",
                    "errors": [
                        {
                            "reason": "insufficientPermissions",
                            "message": (
                                "synthetic-file provider-payload-sentinel "
                                "synthetic-refresh-token"
                            ),
                        }
                    ],
                }
            },
        ),
    ]

    report = await _composed_runner(
        config, QueuedResponseTransport(responses)
    ).run(environment_opt_in=True)
    serialized = report.model_dump_json()

    assert report.schema_version == "google-live-qualification-report:v5"
    assert report.failure_code == "provider_oauth_scope_insufficient"
    assert report.failure_stage == "drive"
    assert report.failure_class == "policy"
    assert report.retry_disposition == "not_retryable"
    assert report.oauth_request_attempts == 1
    assert report.drive_request_attempts == 2
    assert report.sheets_request_attempts == 0
    assert report.metadata_reads == 1
    assert report.downloads == 0
    assert responses == []
    assert "synthetic-file" not in serialized
    assert "provider-payload-sentinel" not in serialized
    assert "synthetic-refresh-token" not in serialized


@pytest.mark.asyncio
async def test_live_evidence_surfaces_do_not_disclose_private_values() -> None:
    config = _config(opt_in=True)
    provider_response = GoogleApiResponse(
        status_code=503,
        json_body={"error": {"message": "drive-provider-payload-sentinel"}},
        content=b"drive-source-content-sentinel",
    )
    responses = [
        GoogleApiResponse(
            status_code=200,
            json_body={"access_token": "synthetic-token", "expires_in": 3600},
        ),
        provider_response,
    ]
    registry = DiagnosticEventRegistry.with_default_families()
    sink = RecordingDiagnosticSink(registry)
    runner = _composed_runner(
        config,
        QueuedResponseTransport(responses),
        diagnostic_sink=sink,
    )
    provider = GoogleLiveQualificationProvider(
        config=config,
        service=RecordingService(config.api_configuration_fingerprint),  # type: ignore[arg-type]
        monotonic=lambda: 0.0,
    )

    report = await runner.run(environment_opt_in=True)

    safe_evidence = "\n".join(
        (
            report.model_dump_json(),
            repr(config),
            repr(runner),
            repr(provider),
            repr(provider_response),
            *(str(event.payload_json()) for event in sink.events),
        )
    )
    for forbidden in (
        "synthetic-client-secret",
        "synthetic-refresh-token",
        "drive-provider-payload-sentinel",
        "drive-source-content-sentinel",
        "synthetic-file",
        config.endpoints.oauth_token_url,
    ):
        assert forbidden not in safe_evidence


@pytest.mark.asyncio
async def test_offline_composition_attributes_post_provider_failure_to_harness() -> (
    None
):
    config = _config(opt_in=True)
    responses = [
        GoogleApiResponse(
            status_code=200,
            json_body={"access_token": "synthetic-token", "expires_in": 3600},
        ),
        GoogleApiResponse(
            status_code=200,
            json_body={
                "id": "synthetic-file",
                "name": "Synthetic file.txt",
                "mimeType": "text/plain",
                "version": "1",
            },
        ),
    ]
    runner = _composed_runner(config, QueuedResponseTransport(responses))

    async def fail_after_provider(awaitable, timeout: float):
        del timeout
        await awaitable
        raise RuntimeError("synthetic harness failure")

    runner = replace(runner, wait_for=fail_after_provider)

    report = await runner.run(environment_opt_in=True)

    assert report.failure_code == "harness_failure"
    assert report.failure_stage == "harness"
    assert report.failure_class == "harness"
    assert report.retry_disposition == "not_applicable"
    assert report.oauth_request_attempts == 1
    assert report.drive_request_attempts == 1
    assert report.metadata_reads == 1


@pytest.mark.asyncio
async def test_offline_composition_attributes_inflight_deadline_to_provider() -> None:
    config = _config(opt_in=True)

    class Transport:
        def __init__(self) -> None:
            self.calls = 0

        async def request(self, request):
            del request
            self.calls += 1
            if self.calls == 1:
                return GoogleApiResponse(
                    status_code=200,
                    json_body={"access_token": "synthetic-token", "expires_in": 3600},
                )
            raise GoogleExecutionDeadlineExceeded(
                "Google qualification deadline exceeded."
            )

    transport = Transport()
    report = await _composed_runner(config, transport).run(environment_opt_in=True)

    assert report.failure_code == "deadline_exceeded"
    assert report.failure_stage == "drive"
    assert report.failure_class == "deadline"
    assert report.retry_disposition == "stopped_by_deadline"
    assert report.drive_request_attempts == 1
    assert transport.calls == 2


@pytest.mark.asyncio
async def test_offline_composition_reports_resource_limit_without_retry() -> None:
    config = _config(opt_in=True)
    responses = [
        GoogleApiResponse(
            status_code=200,
            json_body={"access_token": "synthetic-token", "expires_in": 3600},
        ),
        GoogleApiResponse(
            status_code=200,
            json_body={
                "id": "synthetic-file",
                "name": "Synthetic file.txt",
                "mimeType": "text/plain",
                "version": "1",
            },
            content=b"x" * 1025,
        ),
    ]

    report = await _composed_runner(
        config, QueuedResponseTransport(responses)
    ).run(environment_opt_in=True)

    assert report.failure_code == "resource_limit_exceeded"
    assert report.failure_stage == "harness"
    assert report.failure_class == "resource_limit"
    assert report.retry_disposition == "not_applicable"
    assert report.drive_request_attempts == 1
    assert responses == []


@pytest.mark.asyncio
async def test_offline_composition_resets_per_run_progress() -> None:
    config = _config(opt_in=True)
    responses = [
        GoogleApiResponse(
            status_code=200,
            json_body={"access_token": "synthetic-token", "expires_in": 3600},
        ),
        GoogleApiResponse(
            status_code=200,
            json_body={
                "id": "synthetic-file",
                "name": "Synthetic file.txt",
                "mimeType": "text/plain",
                "version": "1",
            },
        ),
        GoogleApiResponse(
            status_code=200,
            json_body={
                "id": "synthetic-file",
                "name": "Synthetic file.txt",
                "mimeType": "text/plain",
                "version": "1",
            },
        ),
    ]
    runner = _composed_runner(config, QueuedResponseTransport(responses))

    first = await runner.run(environment_opt_in=True)
    second = await runner.run(environment_opt_in=True)

    assert (first.oauth_request_attempts, first.drive_request_attempts) == (1, 1)
    assert (second.oauth_request_attempts, second.drive_request_attempts) == (0, 1)
    assert first.metadata_reads == second.metadata_reads == 1


@pytest.mark.asyncio
async def test_offline_composition_records_every_bounded_drive_attempt() -> None:
    config = _config(opt_in=True)
    config = config.model_copy(
        update={
            "request_policy": config.request_policy.model_copy(
                update={"max_attempts": 2, "backoff_ceiling_seconds": (1.0,)}
            )
        }
    )
    responses = [
        GoogleApiResponse(
            status_code=200,
            json_body={"access_token": "synthetic-token", "expires_in": 3600},
        ),
        GoogleApiResponse(status_code=503),
        GoogleApiResponse(status_code=503),
    ]
    transport = QueuedResponseTransport(responses)
    runner = _composed_runner(config, transport)

    report = await runner.run(environment_opt_in=True)

    assert report.status == "failed"
    assert report.failure_stage == "drive"
    assert report.oauth_request_attempts == 1
    assert report.drive_request_attempts == 2
    assert report.sheets_request_attempts == 0
    assert report.metadata_reads == 0
    assert responses == []


@pytest.mark.asyncio
async def test_offline_composition_reports_exhausted_drive_http_5xx_retries() -> None:
    config = _config(opt_in=True)
    config = config.model_copy(
        update={
            "request_policy": config.request_policy.model_copy(
                update={
                    "max_attempts": 4,
                    "backoff_ceiling_seconds": (1.0, 2.0, 4.0),
                }
            )
        }
    )
    responses = [
        GoogleApiResponse(
            status_code=200,
            json_body={"access_token": "synthetic-token", "expires_in": 3600},
        ),
        *(GoogleApiResponse(status_code=503) for _ in range(4)),
    ]
    runner = _composed_runner(config, QueuedResponseTransport(responses))

    report = await runner.run(environment_opt_in=True)

    assert report.failure_class == "http_5xx"
    assert report.retry_disposition == "attempts_exhausted"
    assert report.configured_max_attempts == 4
    assert report.drive_request_attempts == 4
    assert responses == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "failure_class"),
    (
        (TimeoutError("synthetic timeout"), "timeout"),
        (
            GoogleProviderReadError(
                failure_class="transport",
                retry_disposition="stopped_before_retry",
            ),
            "transport",
        ),
    ),
)
async def test_offline_composition_reports_exhausted_drive_transport_retries(
    failure: Exception, failure_class: str
) -> None:
    config = _config(opt_in=True)
    config = config.model_copy(
        update={
            "request_policy": config.request_policy.model_copy(
                update={"max_attempts": 2, "backoff_ceiling_seconds": (1.0,)}
            )
        }
    )

    class Transport:
        def __init__(self) -> None:
            self.calls = 0

        async def request(self, request):
            del request
            self.calls += 1
            if self.calls == 1:
                return GoogleApiResponse(
                    status_code=200,
                    json_body={"access_token": "synthetic-token", "expires_in": 3600},
                )
            raise failure

    transport = Transport()
    report = await _composed_runner(config, transport).run(environment_opt_in=True)

    assert report.failure_stage == "drive"
    assert report.failure_class == failure_class
    assert report.retry_disposition == "attempts_exhausted"
    assert report.configured_max_attempts == 2
    assert report.drive_request_attempts == 2
    assert transport.calls == 3


@pytest.mark.asyncio
async def test_offline_composition_reports_second_unauthorized_as_nonretryable() -> (
    None
):
    config = _config(opt_in=True)
    config = config.model_copy(
        update={
            "request_policy": config.request_policy.model_copy(
                update={
                    "max_attempts": 4,
                    "backoff_ceiling_seconds": (1.0, 2.0, 4.0),
                }
            )
        }
    )
    responses = [
        GoogleApiResponse(
            status_code=200,
            json_body={"access_token": "first-token", "expires_in": 3600},
        ),
        GoogleApiResponse(status_code=401),
        GoogleApiResponse(
            status_code=200,
            json_body={"access_token": "second-token", "expires_in": 3600},
        ),
        GoogleApiResponse(status_code=401),
    ]

    report = await _composed_runner(
        config, QueuedResponseTransport(responses)
    ).run(environment_opt_in=True)

    assert report.failure_code == "authorization_revoked"
    assert report.failure_stage == "drive"
    assert report.failure_class == "authorization"
    assert report.retry_disposition == "not_retryable"
    assert report.configured_max_attempts == 4
    assert report.oauth_request_attempts == 2
    assert report.drive_request_attempts == 2
    assert responses == []


@pytest.mark.asyncio
async def test_offline_composition_reports_failure_before_transport_retry() -> None:
    config = _config(opt_in=True)

    class Resolver:
        async def resolve(self, credential_reference, grant_reference):
            del credential_reference, grant_reference
            raise GoogleProviderReadError(
                failure_class="credential_resolution",
                retry_disposition="stopped_before_retry",
            )

    class Transport:
        async def request(self, request):
            raise AssertionError(f"credential failure must precede transport: {request!r}")

    report = await _composed_runner(
        config,
        Transport(),
        credential_resolver=Resolver(),
    ).run(environment_opt_in=True)

    assert report.failure_stage == "harness"
    assert report.failure_class == "credential_resolution"
    assert report.retry_disposition == "stopped_before_retry"
    assert report.oauth_request_attempts == 0
    assert report.drive_request_attempts == 0


@pytest.mark.asyncio
async def test_offline_composition_reports_cancelled_before_transport() -> None:
    config = _config(opt_in=True)
    transport_calls: list[object] = []

    class Resolver:
        async def resolve(self, credential_reference, grant_reference):
            del credential_reference, grant_reference
            raise asyncio.CancelledError

    class Transport:
        async def request(self, request):
            transport_calls.append(request)
            raise AssertionError("cancelled credential resolution must stop transport")

    report = await _composed_runner(
        config,
        Transport(),
        credential_resolver=Resolver(),
    ).run(environment_opt_in=True)

    assert report.failure_code == "cancelled"
    assert report.failure_stage == "harness"
    assert report.failure_class == "cancelled"
    assert report.retry_disposition == "not_applicable"
    assert report.oauth_request_attempts == 0
    assert report.drive_request_attempts == 0
    assert transport_calls == []


@pytest.mark.asyncio
async def test_offline_composition_reports_nonretryable_drive_policy_denial() -> None:
    config = _config(opt_in=True)
    config = config.model_copy(
        update={
            "request_policy": config.request_policy.model_copy(
                update={
                    "max_attempts": 4,
                    "backoff_ceiling_seconds": (1.0, 2.0, 4.0),
                }
            )
        }
    )
    responses = [
        GoogleApiResponse(
            status_code=200,
            json_body={"access_token": "synthetic-token", "expires_in": 3600},
        ),
        GoogleApiResponse(status_code=403, json_body={"error": {}}),
    ]
    runner = _composed_runner(config, QueuedResponseTransport(responses))

    report = await runner.run(environment_opt_in=True)

    assert report.failure_class == "policy"
    assert report.retry_disposition == "not_retryable"
    assert report.configured_max_attempts == 4
    assert report.drive_request_attempts == 1
    assert responses == []


@pytest.mark.asyncio
async def test_offline_composition_reports_drive_404_as_safe_not_found() -> None:
    config = _config(opt_in=True)
    responses = [
        GoogleApiResponse(
            status_code=200,
            json_body={"access_token": "synthetic-token", "expires_in": 3600},
        ),
        GoogleApiResponse(
            status_code=404,
            json_body={"error": {"message": "provider-payload-sentinel"}},
        ),
    ]

    report = await _composed_runner(
        config, QueuedResponseTransport(responses)
    ).run(environment_opt_in=True)

    assert report.failure_code == "provider_not_found"
    assert report.failure_stage == "drive"
    assert report.failure_class == "not_found"
    assert report.retry_disposition == "not_retryable"
    assert report.oauth_request_attempts == 1
    assert report.drive_request_attempts == 1
    assert report.sheets_request_attempts == 0
    assert report.metadata_reads == 0
    assert report.downloads == 0
    assert report.exports == 0
    assert "provider-payload-sentinel" not in report.model_dump_json()
    assert responses == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scenario", "failure_code", "failure_stage", "failure_class", "disposition"),
    (
        (
            "quota",
            "provider_quota_exhausted",
            "drive",
            "quota",
            "not_retryable",
        ),
        (
            "not_found",
            "provider_not_found",
            "sheets",
            "not_found",
            "not_retryable",
        ),
        (
            "unclassified",
            "provider_unavailable",
            "drive",
            "unclassified",
            "stopped_before_retry",
        ),
    ),
)
async def test_offline_composition_reports_remaining_closed_failure_classes(
    scenario: str,
    failure_code: str,
    failure_stage: str,
    failure_class: str,
    disposition: str,
) -> None:
    config = _config(opt_in=True)
    responses: list[GoogleApiResponse | Exception] = [
        GoogleApiResponse(
            status_code=200,
            json_body={"access_token": "synthetic-token", "expires_in": 3600},
        )
    ]
    if scenario == "quota":
        responses.append(
            GoogleApiResponse(
                status_code=429,
                json_body={
                    "error": {"errors": [{"reason": "dailyLimitExceeded"}]}
                },
            )
        )
    elif scenario == "not_found":
        config = config.model_copy(
            update={
                "resource_scope": config.resource_scope.model_copy(
                    update={
                        "allowed_file_ids": (),
                        "allowed_spreadsheet_ids": ("synthetic-sheet",),
                    }
                ),
                "file_targets": (),
                "sheet_targets": (
                    _sheet_target("synthetic-sheet", 1, ("'Synthetic'!A1:A2",)),
                ),
            }
        )
        responses.extend(
            (
                _sheet_identity_response("synthetic-sheet", 1, "Synthetic"),
                GoogleApiResponse(
                    status_code=200,
                    json_body={"id": "synthetic-sheet", "version": "1"},
                ),
                GoogleApiResponse(status_code=404),
            )
        )
    else:
        responses.append(RuntimeError("raw-provider-detail-sentinel"))

    class Transport:
        async def request(self, request):
            del request
            response = responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response

    report = await _composed_runner(config, Transport()).run(environment_opt_in=True)

    assert report.failure_code == failure_code
    assert report.failure_stage == failure_stage
    assert report.failure_class == failure_class
    assert report.retry_disposition == disposition
    assert "raw-provider-detail-sentinel" not in report.model_dump_json()
    assert responses == []


@pytest.mark.asyncio
async def test_offline_composition_reports_httpx_stream_failure_as_transport() -> None:
    config = _config(opt_in=True)

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == config.endpoints.oauth_token_url:
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json={"access_token": "synthetic-token", "expires_in": 3600},
            )
        raise httpx.StreamError("raw-stream-error provider-id-sentinel")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        report = await _composed_runner(
            config, HttpxGoogleApiTransport(client)
        ).run(environment_opt_in=True)

    assert report.status == "failed"
    assert report.failure_code == "provider_unavailable"
    assert report.failure_stage == "drive"
    assert report.failure_class == "transport"
    assert report.retry_disposition == "attempts_exhausted"
    assert report.oauth_request_attempts == 1
    assert report.drive_request_attempts == 1
    assert report.sheets_request_attempts == 0
    assert "raw-stream-error" not in report.model_dump_json()
    assert "provider-id-sentinel" not in report.model_dump_json()


def _phase1_sheet_target(
    *, sheet_id: int, title: str
) -> GoogleLiveSheetTarget:
    return GoogleLiveSheetTarget(
        profile=GoogleSheetsReadProfile(
            spreadsheet_id="synthetic-register",
            sheet_id=sheet_id,
            business_id_column="immutable_business_id",
            columns=(
                GoogleSheetsColumn(
                    column_key="immutable_business_id", coordinate="A"
                ),
                GoogleSheetsColumn(column_key="formula_total", coordinate="B"),
                GoogleSheetsColumn(column_key="service_date", coordinate="C"),
                GoogleSheetsColumn(column_key="amount", coordinate="D"),
                GoogleSheetsColumn(column_key="ratio", coordinate="E"),
            ),
        ),
        a1_ranges=(f"'{title}'!A2:E5000",),
    )


def _phase1_file_metadata(
    file_id: str, *, name: str, media_type: str, version: str
) -> GoogleApiResponse:
    return GoogleApiResponse(
        status_code=200,
        json_body={
            "id": file_id,
            "name": name,
            "mimeType": media_type,
            "version": version,
            "webViewLink": f"https://drive.test/{file_id}",
            "parents": ["synthetic-root"],
            "modifiedTime": "2026-11-04T12:00:00Z",
            "trashed": False,
        },
    )


def _phase1_sheet_identity(sheet_id: int, title: str) -> GoogleApiResponse:
    return GoogleApiResponse(
        status_code=200,
        json_body={
            "spreadsheetId": "synthetic-register",
            "sheets": [{"properties": {"sheetId": sheet_id, "title": title}}],
        },
    )


def _phase1_sheet_grid(
    *, sheet_id: int, title: str, business_id: str
) -> GoogleApiResponse:
    return GoogleApiResponse(
        status_code=200,
        json_body={
            "spreadsheetId": "synthetic-register",
            "properties": {"locale": "en_US", "timeZone": "UTC"},
            "sheets": [
                {
                    "properties": {"sheetId": sheet_id, "title": title},
                    "data": [
                        {
                            "startRow": 1,
                            "startColumn": 0,
                            "rowData": [
                                {
                                    "values": [
                                        {
                                            "userEnteredValue": {
                                                "stringValue": business_id
                                            },
                                            "effectiveValue": {
                                                "stringValue": business_id
                                            },
                                            "formattedValue": business_id,
                                            "effectiveFormat": {
                                                "numberFormat": {"type": "TEXT"}
                                            },
                                        },
                                        {
                                            "userEnteredValue": {
                                                "formulaValue": "=D2*2"
                                            },
                                            "effectiveValue": {"numberValue": 25.0},
                                            "formattedValue": "25.00",
                                            "effectiveFormat": {
                                                "numberFormat": {
                                                    "type": "NUMBER",
                                                    "pattern": "0.00",
                                                }
                                            },
                                        },
                                        {
                                            "userEnteredValue": {
                                                "numberValue": 45965.0
                                            },
                                            "effectiveValue": {
                                                "numberValue": 45965.0
                                            },
                                            "formattedValue": "2025-11-04",
                                            "effectiveFormat": {
                                                "numberFormat": {
                                                    "type": "DATE",
                                                    "pattern": "yyyy-mm-dd",
                                                }
                                            },
                                        },
                                        {
                                            "userEnteredValue": {
                                                "numberValue": 12.5
                                            },
                                            "effectiveValue": {
                                                "numberValue": 12.5
                                            },
                                            "formattedValue": "$12.50",
                                            "effectiveFormat": {
                                                "numberFormat": {
                                                    "type": "CURRENCY",
                                                    "pattern": "$#,##0.00",
                                                }
                                            },
                                        },
                                        {
                                            "userEnteredValue": {
                                                "numberValue": 0.125
                                            },
                                            "effectiveValue": {
                                                "numberValue": 0.125
                                            },
                                            "formattedValue": "12.5%",
                                            "effectiveFormat": {
                                                "numberFormat": {
                                                    "type": "PERCENT",
                                                    "pattern": "0.0%",
                                                }
                                            },
                                        },
                                    ]
                                }
                            ],
                        }
                    ],
                }
            ],
        },
    )


@pytest.mark.asyncio
async def test_provider_accepts_rows_nested_in_the_range_page_snapshot() -> None:
    sheet_target = _phase1_sheet_target(sheet_id=101, title="records")
    config = _config(opt_in=True).model_copy(
        update={
            "resource_scope": _config(opt_in=True).resource_scope.model_copy(
                update={
                    "allowed_file_ids": (),
                    "allowed_spreadsheet_ids": ("synthetic-register",),
                }
            ),
            "file_targets": (),
            "sheet_targets": (sheet_target,),
        }
    )
    page = GoogleSheetsRangePage(
        spreadsheet_id="synthetic-register",
        sheet_id=101,
        a1_range="'records'!A2:E5000",
        page_token=None,
        next_page_token=None,
        complete=True,
        provider_version_before="21",
        provider_version_after="21",
        snapshot=GoogleSheetsSnapshot(
            spreadsheet_id="synthetic-register",
            sheet_id=101,
            sheet_title="records",
            locale="en_US",
            time_zone="UTC",
            rows=(),
        ),
    )
    provider = GoogleLiveQualificationProvider(
        config=config,
        service=OnePageSheetsService(  # type: ignore[arg-type]
            config.api_configuration_fingerprint, page
        ),
        monotonic=lambda: 0.0,
    )

    counts = await provider.read(
        config.connection,
        GoogleReadCommand(
            connection_id=config.connection.connection_id,
            resource_scope_reference=config.resource_scope.reference,
            authorization_generation=config.connection.authorization_generation,
            configuration_generation=config.connection.configuration_generation,
        ),
    )

    assert counts.sheet_pages == 1


@pytest.mark.asyncio
async def test_phase1_offline_replay_completes_every_approved_read_operation() -> None:
    config = _config(opt_in=True)
    config = config.model_copy(
        update={
            "resource_scope": config.resource_scope.model_copy(
                update={
                    "folder_id": "synthetic-root",
                    "allowed_file_ids": (
                        "synthetic-pdf",
                        "synthetic-docx",
                        "synthetic-native-doc",
                    ),
                    "allowed_spreadsheet_ids": ("synthetic-register",),
                }
            ),
            "request_policy": config.request_policy.model_copy(
                update={"sheets_page_size": 4999}
            ),
            "file_targets": (
                GoogleLiveFileTarget(file_id="synthetic-pdf", download=True),
                GoogleLiveFileTarget(file_id="synthetic-docx", download=True),
                GoogleLiveFileTarget(
                    file_id="synthetic-native-doc",
                    download=False,
                    export_mime_types=(
                        "application/pdf",
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    ),
                ),
            ),
            "sheet_targets": (
                _phase1_sheet_target(sheet_id=101, title="records"),
                _phase1_sheet_target(sheet_id=202, title="archive"),
            ),
            "execution_envelope": config.execution_envelope.model_copy(
                update={"max_drive_file_targets": 3}
            ),
        }
    )
    responses = [
        GoogleApiResponse(
            status_code=200,
            json_body={"access_token": "synthetic-token", "expires_in": 3600},
        ),
        _phase1_file_metadata(
            "synthetic-pdf",
            name="Synthetic brief.pdf",
            media_type="application/pdf",
            version="11",
        ),
        GoogleApiResponse(
            status_code=200,
            content=b"synthetic-pdf-bytes",
            headers={"content-type": "application/pdf"},
        ),
        _phase1_file_metadata(
            "synthetic-docx",
            name="Synthetic notes.docx",
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            version="12",
        ),
        GoogleApiResponse(
            status_code=200,
            content=b"synthetic-docx-bytes",
            headers={
                "content-type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            },
        ),
        _phase1_file_metadata(
            "synthetic-native-doc",
            name="Synthetic native document",
            media_type="application/vnd.google-apps.document",
            version="13",
        ),
        GoogleApiResponse(
            status_code=200,
            content=b"synthetic-export-pdf",
            headers={"content-type": "application/pdf"},
        ),
        GoogleApiResponse(
            status_code=200,
            content=b"synthetic-export-docx",
            headers={
                "content-type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            },
        ),
        GoogleApiResponse(
            status_code=200, json_body={"startPageToken": "start-token-1"}
        ),
        GoogleApiResponse(
            status_code=200,
            json_body={
                "incompleteSearch": False,
                "files": [
                    {"id": "synthetic-pdf"},
                    {"id": "synthetic-docx"},
                    {"id": "synthetic-native-doc"},
                ],
            },
        ),
        GoogleApiResponse(
            status_code=200,
            json_body={
                "newStartPageToken": "start-token-2",
                "changes": [],
            },
        ),
        _phase1_sheet_identity(101, "records"),
        GoogleApiResponse(
            status_code=200,
            json_body={"id": "synthetic-register", "version": "21"},
        ),
        _phase1_sheet_grid(
            sheet_id=101,
            title="records",
            business_id="018f3e70-7b5a-4d0c-8e31-5c391a854201",
        ),
        GoogleApiResponse(
            status_code=200,
            json_body={"id": "synthetic-register", "version": "21"},
        ),
        _phase1_sheet_identity(202, "archive"),
        GoogleApiResponse(
            status_code=200,
            json_body={"id": "synthetic-register", "version": "21"},
        ),
        _phase1_sheet_grid(
            sheet_id=202,
            title="archive",
            business_id="018f3e70-7b5a-4d0c-8e31-5c391a854202",
        ),
        GoogleApiResponse(
            status_code=200,
            json_body={"id": "synthetic-register", "version": "21"},
        ),
    ]
    transport = RecordingQueuedResponseTransport(responses)

    report = await _composed_runner(config, transport).run(environment_opt_in=True)

    assert report.status == "passed", json.dumps(
        {
            "failure_stage": report.failure_stage,
            "failure_code": report.failure_code,
            "failure_class": report.failure_class,
            "retry_disposition": report.retry_disposition,
            "metadata_reads": report.metadata_reads,
            "downloads": report.downloads,
            "exports": report.exports,
            "snapshot_pages": report.snapshot_pages,
            "change_pages": report.change_pages,
            "sheet_pages": report.sheet_pages,
            "request_count": len(transport.requests),
            "remaining_responses": len(responses),
        },
        sort_keys=True,
    )
    assert report.schema_version == "google-live-qualification-report:v5"
    assert report.metadata_reads == 3
    assert report.downloads == 2
    assert report.exports == 2
    assert report.snapshot_pages == 1
    assert report.change_pages == 1
    assert report.sheet_pages == 2
    assert report.oauth_request_attempts == 1
    assert report.drive_request_attempts == 14
    assert report.sheets_request_attempts == 4
    assert report.shared_drive_status == "NOT_EXERCISED"
    assert report.failure_stage is None
    assert report.failure_code is None
    assert report.failure_class is None
    assert report.retry_disposition is None
    assert responses == []

    assert [(request.method, request.url) for request in transport.requests] == [
        ("POST", "https://oauth2.googleapis.test/token"),
        ("GET", "https://www.googleapis.test/drive/v3/files/synthetic-pdf"),
        ("GET", "https://www.googleapis.test/drive/v3/files/synthetic-pdf"),
        ("GET", "https://www.googleapis.test/drive/v3/files/synthetic-docx"),
        ("GET", "https://www.googleapis.test/drive/v3/files/synthetic-docx"),
        (
            "GET",
            "https://www.googleapis.test/drive/v3/files/synthetic-native-doc",
        ),
        (
            "GET",
            "https://www.googleapis.test/drive/v3/files/synthetic-native-doc/export",
        ),
        (
            "GET",
            "https://www.googleapis.test/drive/v3/files/synthetic-native-doc/export",
        ),
        (
            "GET",
            "https://www.googleapis.test/drive/v3/changes/startPageToken",
        ),
        ("GET", "https://www.googleapis.test/drive/v3/files"),
        ("GET", "https://www.googleapis.test/drive/v3/changes"),
        (
            "GET",
            "https://sheets.googleapis.test/v4/spreadsheets/synthetic-register",
        ),
        (
            "GET",
            "https://www.googleapis.test/drive/v3/files/synthetic-register",
        ),
        (
            "GET",
            "https://sheets.googleapis.test/v4/spreadsheets/synthetic-register",
        ),
        (
            "GET",
            "https://www.googleapis.test/drive/v3/files/synthetic-register",
        ),
        (
            "GET",
            "https://sheets.googleapis.test/v4/spreadsheets/synthetic-register",
        ),
        (
            "GET",
            "https://www.googleapis.test/drive/v3/files/synthetic-register",
        ),
        (
            "GET",
            "https://sheets.googleapis.test/v4/spreadsheets/synthetic-register",
        ),
        (
            "GET",
            "https://www.googleapis.test/drive/v3/files/synthetic-register",
        ),
    ]
    metadata_fields = (
        "id,name,mimeType,version,webViewLink,driveId,parents,modifiedTime,trashed"
    )
    assert [transport.requests[index].query for index in (1, 3, 5)] == [
        {"fields": metadata_fields, "supportsAllDrives": "false"},
        {"fields": metadata_fields, "supportsAllDrives": "false"},
        {"fields": metadata_fields, "supportsAllDrives": "false"},
    ]
    assert transport.requests[2].query == {
        "alt": "media",
        "supportsAllDrives": "false",
    }
    assert transport.requests[4].query == {
        "alt": "media",
        "supportsAllDrives": "false",
    }
    assert [transport.requests[index].query for index in (6, 7)] == [
        {"mimeType": "application/pdf"},
        {
            "mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        },
    ]
    assert transport.requests[8].query == {"supportsAllDrives": "false"}
    assert transport.requests[9].query == {
        "fields": "nextPageToken,incompleteSearch,files(id,driveId)",
        "pageSize": "10",
        "supportsAllDrives": "false",
        "includeItemsFromAllDrives": "false",
        "q": "'synthetic-root' in parents and trashed = false",
    }
    change_request = transport.requests[10]
    assert change_request.url.endswith("/changes")
    assert change_request.query == {
        "pageToken": "start-token-1",
        "pageSize": "10",
        "includeRemoved": "true",
        "supportsAllDrives": "false",
        "includeItemsFromAllDrives": "false",
        "fields": (
            "nextPageToken,newStartPageToken,"
            "changes(fileId,removed,file(id,driveId,version,trashed,parents))"
        ),
    }
    identity_requests = (transport.requests[11], transport.requests[15])
    assert [request.query for request in identity_requests] == [
        {
            "includeGridData": "false",
            "ranges": "'records'!A2:E5000",
            "fields": "spreadsheetId,sheets(properties(sheetId,title))",
        },
        {
            "includeGridData": "false",
            "ranges": "'archive'!A2:E5000",
            "fields": "spreadsheetId,sheets(properties(sheetId,title))",
        },
    ]
    version_requests = tuple(transport.requests[index] for index in (12, 14, 16, 18))
    assert all(
        request.query == {"fields": "id,version", "supportsAllDrives": "false"}
        for request in version_requests
    )
    sheets_fields = (
        "spreadsheetId,properties(locale,timeZone),"
        "developerMetadata(metadataId,metadataKey,metadataValue,location),"
        "sheets(properties(sheetId,title),developerMetadata(metadataId,metadataKey,"
        "metadataValue,location),data(startRow,startColumn,rowData(values("
        "userEnteredValue,effectiveValue,formattedValue,effectiveFormat(numberFormat)))))"
    )
    grid_requests = (transport.requests[13], transport.requests[17])
    assert [request.query for request in grid_requests] == [
        {
            "includeGridData": "true",
            "ranges": "'records'!A2:E5000",
            "fields": sheets_fields,
        },
        {
            "includeGridData": "true",
            "ranges": "'archive'!A2:E5000",
            "fields": sheets_fields,
        },
    ]


@pytest.mark.asyncio
async def test_offline_composition_preserves_completed_operations_before_failure() -> (
    None
):
    config = _config(opt_in=True)
    config = config.model_copy(
        update={
            "resource_scope": config.resource_scope.model_copy(
                update={"allowed_file_ids": ("first-file", "second-file")}
            ),
            "file_targets": (
                GoogleLiveFileTarget(file_id="first-file", download=False),
                GoogleLiveFileTarget(file_id="second-file", download=False),
            ),
        }
    )
    responses = [
        GoogleApiResponse(
            status_code=200,
            json_body={"access_token": "synthetic-token", "expires_in": 3600},
        ),
        GoogleApiResponse(
            status_code=200,
            json_body={
                "id": "first-file",
                "name": "First synthetic file.txt",
                "mimeType": "text/plain",
                "version": "1",
            },
        ),
        GoogleApiResponse(status_code=503),
    ]
    runner = _composed_runner(config, QueuedResponseTransport(responses))

    report = await runner.run(environment_opt_in=True)

    assert report.status == "failed"
    assert report.failure_stage == "drive"
    assert report.oauth_request_attempts == 1
    assert report.drive_request_attempts == 2
    assert report.metadata_reads == 1
    assert responses == []


@pytest.mark.asyncio
async def test_offline_composition_records_every_bounded_sheets_attempt() -> None:
    config = _config(opt_in=True)
    config = config.model_copy(
        update={
            "resource_scope": config.resource_scope.model_copy(
                update={
                    "allowed_file_ids": (),
                    "allowed_spreadsheet_ids": ("synthetic-sheet",),
                }
            ),
            "file_targets": (),
            "sheet_targets": (
                _sheet_target("synthetic-sheet", 1, ("'Synthetic'!A1:A2",)),
            ),
            "request_policy": config.request_policy.model_copy(
                update={"max_attempts": 2, "backoff_ceiling_seconds": (1.0,)}
            ),
        }
    )
    responses = [
        GoogleApiResponse(
            status_code=200,
            json_body={"access_token": "synthetic-token", "expires_in": 3600},
        ),
        _sheet_identity_response("synthetic-sheet", 1, "Synthetic"),
        GoogleApiResponse(
            status_code=200,
            json_body={"id": "synthetic-sheet", "version": "1"},
        ),
        GoogleApiResponse(status_code=503),
        GoogleApiResponse(status_code=503),
    ]
    transport = QueuedResponseTransport(responses)
    runner = _composed_runner(config, transport)

    report = await runner.run(environment_opt_in=True)

    assert report.status == "failed"
    assert report.failure_code == "provider_unavailable"
    assert report.failure_stage == "sheets"
    assert report.failure_class == "http_5xx"
    assert report.retry_disposition == "attempts_exhausted"
    assert report.configured_max_attempts == 2
    assert report.oauth_request_attempts == 1
    assert report.drive_request_attempts == 1
    assert report.sheets_request_attempts == 3
    assert report.sheet_pages == 0
    assert responses == []


@pytest.mark.asyncio
async def test_offline_composition_uses_injected_provider_ports_after_admission() -> (
    None
):
    config = _config(opt_in=True)
    request_policy = GoogleApiRequestPolicy.model_validate(
        config.request_policy.model_dump()
        | {"max_attempts": 2, "backoff_ceiling_seconds": (1.0,)}
    )
    config = config.model_copy(update={"request_policy": request_policy})
    repository = CurrentConnectionRepository(config.connection)
    resolver_calls: list[tuple[object, object]] = []
    retry_events = []
    sleep_delays: list[float] = []
    clock_calls: list[datetime] = []
    responses = [
        GoogleApiResponse(
            status_code=200,
            json_body={"access_token": "synthetic-access-token", "expires_in": 3600},
        ),
        GoogleApiResponse(status_code=503),
        GoogleApiResponse(
            status_code=200,
            json_body={
                "id": "synthetic-file",
                "name": "Synthetic file.txt",
                "mimeType": "text/plain",
                "version": "1",
            },
        ),
    ]

    class Resolver:
        async def resolve(self, credential_reference, grant_reference):
            resolver_calls.append((credential_reference, grant_reference))
            return GoogleDesktopOAuthMaterial(
                client_id="synthetic-client",
                client_secret="synthetic-client-secret",
                refresh_token="synthetic-refresh-token",
            )

    class Transport:
        async def request(self, request):
            return responses.pop(0)

    class RetryObserver:
        async def record_retry(self, event):
            retry_events.append(event)

    registry = DiagnosticEventRegistry.with_default_families()
    sink = RecordingDiagnosticSink(registry)

    async def sleep(delay: float) -> None:
        sleep_delays.append(delay)

    def clock() -> datetime:
        value = datetime(2026, 10, 29, tzinfo=timezone.utc)
        clock_calls.append(value)
        return value

    bindings = GoogleLiveQualificationBindings(
        credential_resolver=Resolver(),  # type: ignore[arg-type]
        transport=Transport(),  # type: ignore[arg-type]
        retry_observer=RetryObserver(),  # type: ignore[arg-type]
        jitter=lambda _lower, _upper: 0.25,
        connection_repository=repository,  # type: ignore[arg-type]
        diagnostic_sink=sink,
        diagnostic_registry=registry,
        clock=clock,
        sleep=sleep,
        context=_trusted_context(),
        approval_verifier=ApprovedEnvelopeVerifier(),  # type: ignore[arg-type]
    )

    report = await bindings.build_runner(config).run(environment_opt_in=True)

    assert report.status == "passed", report.failure_code
    assert report.failure_stage is None
    assert report.oauth_request_attempts == 1
    assert report.drive_request_attempts == 2
    assert report.sheets_request_attempts == 0
    assert report.metadata_reads == 1
    assert resolver_calls == [
        (config.connection.credential_reference, config.connection.grant_reference)
    ]
    assert len(retry_events) == 1
    assert retry_events[0].reason == "transient"
    assert sleep_delays == [0.25]
    assert clock_calls
    assert sink.events == ()
    assert responses == []


@pytest.mark.asyncio
async def test_offline_composition_uses_injected_sanitized_diagnostic_sink() -> None:
    config = _config(opt_in=True)
    registry = DiagnosticEventRegistry.with_default_families()
    sink = RecordingDiagnosticSink(registry)

    class DeniedRepository:
        @asynccontextmanager
        async def admit_read(self, command, context):
            del command, context
            raise GoogleConnectionDeniedError()
            yield  # pragma: no cover

    class Resolver:
        async def resolve(self, credential_reference, grant_reference):
            raise AssertionError("denied admission must not resolve credentials")

    class Transport:
        async def request(self, request):
            raise AssertionError("denied admission must not issue provider requests")

    class RetryObserver:
        async def record_retry(self, event):
            raise AssertionError("denied admission cannot trigger retries")

    bindings = GoogleLiveQualificationBindings(
        credential_resolver=Resolver(),  # type: ignore[arg-type]
        transport=Transport(),  # type: ignore[arg-type]
        retry_observer=RetryObserver(),  # type: ignore[arg-type]
        connection_repository=DeniedRepository(),  # type: ignore[arg-type]
        diagnostic_sink=sink,
        diagnostic_registry=registry,
        clock=lambda: datetime(2026, 10, 29, tzinfo=timezone.utc),
        sleep=lambda delay: asyncio.sleep(0),
        context=_trusted_context(),
        approval_verifier=ApprovedEnvelopeVerifier(),  # type: ignore[arg-type]
    )

    report = await bindings.build_runner(config).run(environment_opt_in=True)

    assert report.failure_code == "provider_contract_rejected"
    assert len(sink.events) == 1
    assert sink.events[0].payload_json() == {"failure_code": "authorization.denied"}


def test_offline_composition_requires_a_shared_drive_preflight_binding() -> None:
    config = _shared_drive_config()
    registry = DiagnosticEventRegistry.with_default_families()
    bindings = GoogleLiveQualificationBindings(
        credential_resolver=object(),  # type: ignore[arg-type]
        transport=object(),  # type: ignore[arg-type]
        retry_observer=object(),  # type: ignore[arg-type]
        connection_repository=object(),  # type: ignore[arg-type]
        diagnostic_sink=RecordingDiagnosticSink(registry),
        diagnostic_registry=registry,
        clock=lambda: datetime(2026, 10, 29, tzinfo=timezone.utc),
        sleep=lambda delay: asyncio.sleep(0),
        context=object(),  # type: ignore[arg-type]
        approval_verifier=object(),  # type: ignore[arg-type]
    )

    with pytest.raises(GoogleLiveQualificationRefused, match="preflight"):
        bindings.build_runner(config)


@pytest.mark.asyncio
async def test_cli_sanitizes_unavailable_deployment_factory_before_runner_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable_factory(_config):
        raise RuntimeError("secret-bearing deployment error")

    monkeypatch.setattr(
        google_live_qualification,
        "_load_composition",
        lambda reference: unavailable_factory,
    )

    with pytest.raises(GoogleLiveQualificationRefused) as error:
        await google_live_qualification._run(
            _config(opt_in=True),
            tmp_path / "report.json",
            environment_opt_in=True,
            composition_reference="deployment:factory",
        )

    assert str(error.value) == "the approved live composition is unavailable"
    assert error.value.__cause__ is None
    assert not (tmp_path / "report.json").exists()
