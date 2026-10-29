from __future__ import annotations

from contextlib import asynccontextmanager
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
    GoogleLiveCredentialEnvironment,
    GoogleLiveFileTarget,
    GoogleLiveQualificationConfig,
    GoogleLiveQualificationRefused,
    GoogleLiveQualificationRunner,
    GoogleLiveProbeCounts,
    compose_google_live_qualification_runner,
    write_google_live_report,
)
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
    )


class RecordingAdmission:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def read(self, command, context):
        del context
        self.calls.append(str(command.connection_id))
        return GoogleLiveProbeCounts(metadata_reads=1)


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
    config: GoogleLiveQualificationConfig, admission
) -> GoogleLiveQualificationRunner:
    return GoogleLiveQualificationRunner(
        config=config,
        admission=admission,  # type: ignore[arg-type]
        context=object(),  # type: ignore[arg-type]
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
    )

    report = await runner.run(environment_opt_in=True)

    assert report.status == "failed"
    assert report.failure_code == "provider_contract_rejected"
    assert service.calls == []
