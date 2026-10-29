from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest

from spine.application.connections.google import GoogleConnectionDeniedError
from spine.application.diagnostics import DiagnosticEventRegistry
from spine.application.google_sheets_reconciliation import (
    GoogleSheetsAdmittedPageReader,
)
from spine.application.persistence import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
)
from spine.connectors.google_api import (
    GoogleApiEndpoints,
    GoogleApiRequestPolicy,
    GoogleDesktopOAuthMaterial,
    GoogleResourceScope,
)
from spine.connectors.google_composition import compose_google_api_readers
from spine.connectors.google_drive import DriveFileReadProfile
from spine.domain.connections.google import OpaqueGoogleReference
from spine.infrastructure.diagnostics import RecordingDiagnosticSink
from spine.infrastructure.persistence.contexts import TrustedContextBoundary


def _id(value: int) -> UUID:
    return UUID(f"00000000-0000-0000-0000-{value:012d}")


class DenyingRepository:
    @asynccontextmanager
    async def admit_read(self, command, context):
        del command, context
        raise GoogleConnectionDeniedError()
        yield  # pragma: no cover


class RecordingResolver:
    def __init__(self) -> None:
        self.calls = []

    async def resolve(self, credential_reference, grant_reference):
        self.calls.append((credential_reference, grant_reference))
        return GoogleDesktopOAuthMaterial("client", "secret", "refresh")


class RecordingTransport:
    def __init__(self) -> None:
        self.requests = []

    async def request(self, request):
        self.requests.append(request)
        raise AssertionError("provider transport must not be called")


class RecordingRetryObserver:
    async def record_retry(self, event):
        raise AssertionError(f"unexpected retry: {event!r}")


@pytest.mark.asyncio
async def test_production_composition_denies_before_credentials_and_transport() -> None:
    workspace_id = _id(1)
    environment_id = _id(2)
    connection_id = _id(3)
    scope_reference = OpaqueGoogleReference(value=_id(4))
    resolver = RecordingResolver()
    transport = RecordingTransport()
    registry = DiagnosticEventRegistry.with_default_families()
    composition = compose_google_api_readers(
        endpoints=GoogleApiEndpoints(
            drive_base_url="https://www.googleapis.test/drive/v3",
            sheets_base_url="https://sheets.googleapis.test/v4",
            oauth_token_url="https://oauth2.googleapis.test/token",
            approved_oauth_scopes=("https://example.test/drive.readonly",),
            provider_api_version="google:v1",
            configuration_version="approved:v1",
        ),
        request_policy=GoogleApiRequestPolicy(
            timeout_seconds=1.0,
            max_attempts=1,
            backoff_seconds=(),
            drive_page_size=10,
            sheets_page_size=10,
        ),
        resource_scopes=(
            GoogleResourceScope(
                reference=scope_reference,
                workspace_id=workspace_id,
                environment_id=environment_id,
                connection_id=connection_id,
                authorization_generation=1,
                configuration_generation=1,
                allowed_file_ids=("file-1",),
            ),
        ),
        credential_resolver=resolver,
        transport=transport,
        retry_observer=RecordingRetryObserver(),
        connection_repository=DenyingRepository(),  # type: ignore[arg-type]
        diagnostic_sink=RecordingDiagnosticSink(registry),
        diagnostic_registry=registry,
        clock=lambda: datetime(2026, 10, 29, tzinfo=timezone.utc),
        sleep=lambda _delay: _completed_sleep(),
        drive_profile=DriveFileReadProfile(),
        sheet_profiles=(),
    )
    assert isinstance(
        composition.sheets_reconciliation_reader, GoogleSheetsAdmittedPageReader
    )
    boundary = TrustedContextBoundary.for_testing(
        issuer_id=_id(90), secret=b"issue-652-composition-secret-0001"
    )
    context = boundary.worker(
        scope=EnvironmentScope(
            workspace_id=workspace_id, environment_id=environment_id
        ),
        service_principal_id=_id(5),
        purpose=PersistencePurpose("google_connector"),
        operation=PersistenceOperation("google_connection_read"),
        trace_id=_id(6),
    )

    with pytest.raises(GoogleConnectionDeniedError):
        await composition.drive_observation_reader.read(
            SimpleNamespace(
                connection_id=connection_id,
                resource_scope_reference=scope_reference.value,
                authorization_generation=1,
                configuration_generation=1,
                resource_object_reference="file-1",
            ),
            context,
        )

    assert resolver.calls == []
    assert transport.requests == []


async def _completed_sleep() -> None:
    return None
