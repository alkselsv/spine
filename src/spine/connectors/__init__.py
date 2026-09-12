"""External source connectors behind a common incremental-sync contract."""

from spine.connectors.base import Connector, SourceRecord, SyncBatch

__all__ = ["Connector", "SourceRecord", "SyncBatch"]
