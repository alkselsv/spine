"""Production composition for governed Google provider readers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

from spine.application.connections.google import (
    GoogleConnectionRepository,
    GoogleReadAdmission,
)
from spine.application.lifecycle import CurrentLifecycleEligibilityReader
from spine.application.diagnostics import (
    DiagnosticEventRegistry,
    DiagnosticSink,
)
from spine.application.google_sheets_reconciliation import (
    GoogleSheetsAdmittedPageReader,
)
from spine.connectors.google_api import (
    GoogleApiEndpoints,
    GoogleApiRequestPolicy,
    GoogleApiRetryObserver,
    GoogleApiService,
    GoogleApiTransport,
    GoogleCredentialResolver,
    GoogleResourceScope,
)
from spine.connectors.google_drive import (
    DriveFileReadProfile,
    GoogleDriveIncrementalReader,
    GoogleDriveInitialSnapshotReader,
    GoogleDriveObservation,
    GoogleDriveObservationReader,
    GoogleDriveProviderAdapter,
)
from spine.connectors.google_sheets import (
    GoogleSheetsProviderPage,
    GoogleSheetsReadProfile,
    GoogleSheetsReconciliationProviderAdapter,
)


@dataclass(frozen=True, slots=True)
class GoogleApiReaderComposition:
    """Concrete service wired only through the existing admitted reader seams."""

    drive_observation_reader: GoogleDriveObservationReader
    drive_snapshot_reader: GoogleDriveInitialSnapshotReader
    drive_incremental_reader: GoogleDriveIncrementalReader
    sheets_reconciliation_reader: GoogleSheetsAdmittedPageReader


def compose_google_api_readers(
    *,
    endpoints: GoogleApiEndpoints,
    request_policy: GoogleApiRequestPolicy,
    resource_scopes: tuple[GoogleResourceScope, ...],
    credential_resolver: GoogleCredentialResolver,
    transport: GoogleApiTransport,
    retry_observer: GoogleApiRetryObserver,
    connection_repository: GoogleConnectionRepository,
    lifecycle_reader: CurrentLifecycleEligibilityReader,
    diagnostic_sink: DiagnosticSink,
    diagnostic_registry: DiagnosticEventRegistry,
    clock: Callable[[], datetime],
    sleep: Callable[[float], Awaitable[None]],
    drive_profile: DriveFileReadProfile,
    sheet_profiles: tuple[GoogleSheetsReadProfile, ...],
) -> GoogleApiReaderComposition:
    """Build one production adapter graph without a second ingestion path."""

    service = GoogleApiService(
        endpoints=endpoints,
        request_policy=request_policy,
        resource_scopes=resource_scopes,
        credential_resolver=credential_resolver,
        transport=transport,
        retry_observer=retry_observer,
        sheet_profiles=sheet_profiles,
        clock=clock,
        sleep=sleep,
    )
    drive_admission: GoogleReadAdmission[GoogleDriveObservation] = GoogleReadAdmission(
        repository=connection_repository,
        lifecycle_reader=lifecycle_reader,
        provider=GoogleDriveProviderAdapter(
            service=service,
            profile=drive_profile,
            clock=clock,
        ),
        diagnostic_sink=diagnostic_sink,
        diagnostic_registry=diagnostic_registry,
        clock=clock,
    )
    sheets_admission: GoogleReadAdmission[GoogleSheetsProviderPage] = (
        GoogleReadAdmission(
            repository=connection_repository,
            lifecycle_reader=lifecycle_reader,
            provider=GoogleSheetsReconciliationProviderAdapter(
                service=service,
                profiles=sheet_profiles,
                clock=clock,
            ),
            diagnostic_sink=diagnostic_sink,
            diagnostic_registry=diagnostic_registry,
            clock=clock,
        )
    )
    return GoogleApiReaderComposition(
        drive_observation_reader=GoogleDriveObservationReader(
            admission=drive_admission
        ),
        drive_snapshot_reader=GoogleDriveInitialSnapshotReader(
            connection_repository=connection_repository,
            service=service,
            admission=drive_admission,
        ),
        drive_incremental_reader=GoogleDriveIncrementalReader(
            connection_repository=connection_repository,
            service=service,
            admission=drive_admission,
        ),
        sheets_reconciliation_reader=GoogleSheetsAdmittedPageReader(sheets_admission),
    )


__all__ = ["GoogleApiReaderComposition", "compose_google_api_readers"]
