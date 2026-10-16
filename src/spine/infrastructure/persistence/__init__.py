"""Persistence adapters."""

from spine.infrastructure.persistence.in_memory import InMemoryPersistence
from spine.infrastructure.persistence.postgresql import PostgreSQLPersistence
from spine.infrastructure.persistence.backup_recovery import (
    PostgreSQLBackupRecoveryRepository,
)
from spine.infrastructure.persistence.backup_recovery_in_memory import (
    InMemoryRecoveryRepository,
)
from spine.infrastructure.persistence.parsed_artifacts import (
    InMemoryParsedArtifactRepository,
    ParsedArtifactCitationExcerptResolver,
    PostgreSQLParsedArtifactRepository,
)
from spine.infrastructure.persistence.projection_composition import (
    build_production_projection_request_binding,
    build_production_projection_request_consumer,
    create_production_outbox_worker,
)

__all__ = [
    "InMemoryPersistence",
    "PostgreSQLPersistence",
    "PostgreSQLBackupRecoveryRepository",
    "InMemoryRecoveryRepository",
    "InMemoryParsedArtifactRepository",
    "ParsedArtifactCitationExcerptResolver",
    "PostgreSQLParsedArtifactRepository",
    "build_production_projection_request_binding",
    "build_production_projection_request_consumer",
    "create_production_outbox_worker",
]
