from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from spine.connectors.google_api import (
    GoogleApiEndpoints,
    GoogleApiRequestPolicy,
    GoogleResourceScope,
)
from spine.connectors.google_live_qualification import (
    GoogleLiveExecutionAuthorization,
    GoogleLiveCredentialEnvironment,
    GoogleLiveFileTarget,
    GoogleLiveQualificationConfig,
    GoogleLiveQualificationRefused,
    GoogleLiveQualificationProvider,
    GoogleLiveQualificationRunner,
    GoogleLiveProbeCounts,
    GoogleLiveSheetTarget,
    GoogleSharedDriveApproval,
    GoogleSharedDrivePreflightBinding,
    compose_google_live_qualification_runner,
    write_google_live_report,
)
from spine.application.connections.google import GoogleReadCommand
from spine.connectors.google_execution_envelope import GoogleExecutionEnvelope
from spine.connectors.google_sheets import GoogleSheetsColumn, GoogleSheetsReadProfile
from spine.application.diagnostics import DiagnosticEventRegistry
from spine.infrastructure.diagnostics import RecordingDiagnosticSink
from spine.domain.connections.google import (
    GoogleConnection,
    GoogleConnectionStatus,
    OpaqueGoogleReference,
)


def _id(value: int) -> UUID:
    return UUID(f"00000000-0000-0000-0000-{value:012d}")


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
            backoff_seconds=(),
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

    @asynccontextmanager
    async def admit_read(self, command, context):
        del context
        self.commands.append(command)
        yield self.current

    async def suspend_revoked(self, connection, context, *, occurred_at):
        raise AssertionError("not expected")


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
    report = await _runner(_config(opt_in=True), admission).run(
        environment_opt_in=True
    )

    assert report.status == "failed"
    assert report.failure_code == "cancelled"
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
async def test_live_harness_deadline_cancels_provider_and_starts_no_later_calls() -> None:
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
    assert admission.calls == []


@pytest.mark.asyncio
async def test_live_harness_writes_only_sanitized_versioned_evidence(
    tmp_path: Path,
) -> None:
    admission = RecordingAdmission()
    runner = _runner(_config(opt_in=True), admission)

    report = await runner.run(environment_opt_in=True)
    report_path = tmp_path / "google-live-report.json"
    write_google_live_report(report_path, report)
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


def test_live_config_rejects_targets_that_do_not_equal_exact_allowlist() -> None:
    payload = _config(opt_in=True).model_dump()
    payload["resource_scope"]["allowed_file_ids"] = ("different-file",)
    with pytest.raises(ValueError, match="exact file allowlist"):
        GoogleLiveQualificationConfig.model_validate(payload)


def test_live_effective_fingerprint_includes_every_envelope_field() -> None:
    config = _config(opt_in=True)
    changed = config.model_copy(
        update={
            "execution_envelope": config.execution_envelope.model_copy(
                update={"max_aggregate_rows": 101}
            )
        }
    )

    assert config.effective_configuration_fingerprint != changed.effective_configuration_fingerprint


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
        assert preflight.approval_bindings[0].environment_id == _connection().environment_id
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
