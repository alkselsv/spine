"""External source connectors behind a common incremental-sync contract."""

from spine.connectors.base import Connector, SourceRecord, SyncBatch
from spine.connectors.observations import (
    CONNECTOR_OBSERVATION_SCHEMA,
    ConnectorCoverage,
    NormalizedConnectorObservation,
)
from spine.connectors.google_drive import (
    DriveFileContent,
    DriveFileMetadata,
    DriveFileReadProfile,
    GoogleDriveConcurrentModificationError,
    GoogleDriveObservation,
    GoogleDriveObservationReader,
    GoogleDriveProvider,
    GoogleDriveProviderAdapter,
    GoogleDriveReader,
    GoogleDriveService,
)

__all__ = [
    "CONNECTOR_OBSERVATION_SCHEMA",
    "Connector",
    "ConnectorCoverage",
    "NormalizedConnectorObservation",
    "SourceRecord",
    "SyncBatch",
    "DriveFileContent",
    "DriveFileMetadata",
    "DriveFileReadProfile",
    "GoogleDriveConcurrentModificationError",
    "GoogleDriveObservation",
    "GoogleDriveObservationReader",
    "GoogleDriveProvider",
    "GoogleDriveProviderAdapter",
    "GoogleDriveReader",
    "GoogleDriveService",
]
