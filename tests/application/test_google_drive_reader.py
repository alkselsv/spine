from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest

from spine.application.connections.google import (
    GoogleConnectionDeniedError,
    GoogleReadAdmission,
    GoogleReadCommand,
)
from spine.application.diagnostics import DiagnosticEventRegistry
from spine.application.persistence import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
)
from spine.connectors.google_drive import (
    DriveFileContent,
    DriveFileMetadata,
    DriveFileReadProfile,
    GoogleDriveConcurrentModificationError,
    GoogleDriveObservationReader,
    GoogleDriveProviderAdapter,
)
from spine.domain.connections.google import OpaqueGoogleReference
from spine.infrastructure.diagnostics import RecordingDiagnosticSink
from spine.infrastructure.persistence.contexts import TrustedContextBoundary


NOW = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
CONNECTION = UUID("30000000-0000-4000-8000-000000000001")
SCOPE = OpaqueGoogleReference(
    value=UUID("40000000-0000-4000-8000-000000000001")
)


class FakeDriveService:
    def __init__(
        self,
        metadata: tuple[DriveFileMetadata, ...],
        content: DriveFileContent,
    ) -> None:
        self.metadata = list(metadata)
        self.content = content
        self.calls: list[tuple[str, str, str | None]] = []

    async def get_metadata(self, connection, resource_scope_reference):
        del connection
        self.calls.append(("metadata", str(resource_scope_reference.value), None))
        return self.metadata.pop(0)

    async def download(self, connection, resource_scope_reference, file_id, version):
        del connection
        self.calls.append(("download", str(resource_scope_reference.value), version))
        assert file_id == "file-42"
        return self.content

    async def export(
        self,
        connection,
        resource_scope_reference,
        file_id,
        version,
        export_mime_type,
    ):
        del connection
        self.calls.append(("export", str(resource_scope_reference.value), version))
        assert file_id == "file-42"
        assert export_mime_type == "application/pdf"
        return self.content


def _metadata(*, version: str = "7", mime_type: str = "application/pdf"):
    return DriveFileMetadata(
        file_id="file-42",
        name="approved-synthetic.pdf",
        mime_type=mime_type,
        version=version,
        current_url="https://drive.google.test/file-42",
    )


def _command() -> GoogleReadCommand:
    return GoogleReadCommand(
        connection_id=CONNECTION,
        resource_scope_reference=SCOPE,
        authorization_generation=7,
        configuration_generation=11,
    )


def _provider(service: FakeDriveService, *, export_mime_type: str | None = None):
    return GoogleDriveProviderAdapter(
        service=service,
        profile=DriveFileReadProfile(export_mime_type=export_mime_type),
        clock=lambda: NOW,
    )


@pytest.mark.asyncio
async def test_drive_reader_preserves_exact_download_bytes_and_version_guard():
    service = FakeDriveService(
        (_metadata(), _metadata()),
        DriveFileContent(content=b"%PDF-exact", media_type="application/pdf"),
    )
    observation = await _provider(service).read(
        SimpleNamespace(
            connection_id=CONNECTION,
            workspace_id=UUID("10000000-0000-4000-8000-000000000001"),
            environment_id=UUID("20000000-0000-4000-8000-000000000001"),
        ),
        _command(),
    )

    assert observation.file_id == "file-42"
    assert observation.original_bytes == b"%PDF-exact"
    assert observation.provider_version_before == "7"
    assert observation.provider_version_after == "7"
    assert observation.representation_profile == "google-drive-download:v1"
    assert observation.to_normalized_observation().original_bytes == b"%PDF-exact"
    assert [call[0] for call in service.calls] == ["metadata", "download", "metadata"]


@pytest.mark.asyncio
async def test_drive_reader_uses_approved_export_profile_and_retains_export_mime():
    service = FakeDriveService(
        (
            _metadata(
                version="9",
                mime_type="application/vnd.google-apps.document",
            ),
            _metadata(
                version="9",
                mime_type="application/vnd.google-apps.document",
            ),
        ),
        DriveFileContent(content=b"exported-pdf", media_type="application/pdf"),
    )

    observation = await _provider(service, export_mime_type="application/pdf").read(
        SimpleNamespace(
            connection_id=CONNECTION,
            workspace_id=UUID("10000000-0000-4000-8000-000000000001"),
            environment_id=UUID("20000000-0000-4000-8000-000000000001"),
        ),
        _command(),
    )

    normalized = observation.to_normalized_observation()
    assert observation.original_bytes == b"exported-pdf"
    assert observation.provider_media_type == "application/vnd.google-apps.document"
    assert normalized.media_type == "application/pdf"
    assert normalized.extraction_profile == "google-drive-export:v1"
    assert service.calls[1][0] == "export"


@pytest.mark.asyncio
async def test_drive_reader_rejects_provider_version_change_before_handoff():
    service = FakeDriveService(
        (_metadata(version="7"), _metadata(version="8")),
        DriveFileContent(content=b"torn-read", media_type="application/pdf"),
    )

    with pytest.raises(GoogleDriveConcurrentModificationError):
        await _provider(service).read(
            SimpleNamespace(
                connection_id=CONNECTION,
                workspace_id=UUID("10000000-0000-4000-8000-000000000001"),
                environment_id=UUID("20000000-0000-4000-8000-000000000001"),
            ),
            _command(),
        )


@pytest.mark.asyncio
async def test_google_drive_admission_denies_before_metadata_request():
    service = FakeDriveService(
        (_metadata(), _metadata()),
        DriveFileContent(content=b"must-not-read", media_type="application/pdf"),
    )

    class DenyingRepository:
        @asynccontextmanager
        async def admit_read(self, command, context):
            del command, context
            raise GoogleConnectionDeniedError()
            yield  # pragma: no cover

    boundary = TrustedContextBoundary.for_testing(
        issuer_id=UUID("90000000-0000-4000-8000-000000000001"),
        secret=b"google-drive-reader-test-secret-0001",
    )
    registry = DiagnosticEventRegistry.with_default_families()
    admission = GoogleReadAdmission(
        repository=DenyingRepository(),
        provider=_provider(service),
        diagnostic_sink=RecordingDiagnosticSink(registry),
        diagnostic_registry=registry,
        clock=lambda: NOW,
    )
    reader = GoogleDriveObservationReader(admission=admission)

    with pytest.raises(GoogleConnectionDeniedError):
        await reader.read(
            type(
                "ConnectorCommand",
                (),
                {
                    "connection_id": CONNECTION,
                    "resource_scope_reference": SCOPE.value,
                    "authorization_generation": 7,
                    "configuration_generation": 11,
                },
            )(),
            boundary.worker(
                scope=EnvironmentScope(
                    workspace_id=UUID("10000000-0000-4000-8000-000000000001"),
                    environment_id=UUID("20000000-0000-4000-8000-000000000001"),
                ),
                service_principal_id=UUID("30000000-0000-4000-8000-000000000002"),
                purpose=PersistencePurpose("google_connector"),
                operation=PersistenceOperation("google_connection_read"),
                trace_id=UUID("90000000-0000-4000-8000-000000000002"),
            ),
        )

    assert service.calls == []
