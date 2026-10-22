"""Persistence adapters."""

from spine.infrastructure.persistence.in_memory import InMemoryPersistence
from spine.infrastructure.persistence.postgresql import PostgreSQLPersistence
from spine.infrastructure.persistence.postgresql_operational_metrics import (
    PostgreSQLOperationalMetricsStore,
)
from spine.infrastructure.persistence.postgresql_operational_correlation import (
    PostgreSQLOperationalCorrelationStore,
)
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
from spine.infrastructure.persistence.cognee_projection_receipt import (
    CogneeProjectionReceiptBuilder,
)
from spine.infrastructure.persistence.projection_composition import (
    build_postgresql_cognee_projection_delivery_binding,
    build_postgresql_cognee_projection_request_binding,
    build_postgresql_cognee_projection_request_consumer,
    build_projection_delivery_route_binding,
    build_production_projection_delivery_binding,
    build_production_projection_request_binding,
    build_production_projection_request_consumer,
    create_production_outbox_worker,
    prepare_projection_delivery_binding,
)

__all__ = [
    "InMemoryPersistence",
    "PostgreSQLPersistence",
    "PostgreSQLOperationalMetricsStore",
    "PostgreSQLOperationalCorrelationStore",
    "PostgreSQLBackupRecoveryRepository",
    "InMemoryRecoveryRepository",
    "InMemoryParsedArtifactRepository",
    "ParsedArtifactCitationExcerptResolver",
    "PostgreSQLParsedArtifactRepository",
    "CogneeProjectionReceiptBuilder",
    "build_postgresql_cognee_projection_delivery_binding",
    "build_postgresql_cognee_projection_request_binding",
    "build_postgresql_cognee_projection_request_consumer",
    "build_projection_delivery_route_binding",
    "build_production_projection_delivery_binding",
    "build_production_projection_request_binding",
    "build_production_projection_request_consumer",
    "create_production_outbox_worker",
    "prepare_projection_delivery_binding",
]
