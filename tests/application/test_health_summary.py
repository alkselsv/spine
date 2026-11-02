from __future__ import annotations

from uuid import UUID

import pytest

from spine.application.health_summary import (
    CanonicalHealthStatusServices,
    HealthStatus,
)
from spine.application.ingestion_history import IngestionHistoryPage
from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
)
from spine.application.projection_control import ProjectionStatusSnapshot
from spine.infrastructure.persistence.contexts import TrustedContextBoundary


WORKSPACE_ID = UUID("86700000-0000-4000-8000-000000000001")
ENVIRONMENT_ID = UUID("86700000-0000-4000-8000-000000000002")
FOREIGN_WORKSPACE_ID = UUID("86700000-0000-4000-8000-000000000003")
FOREIGN_ENVIRONMENT_ID = UUID("86700000-0000-4000-8000-000000000004")
SERVICE_PRINCIPAL_ID = UUID("86700000-0000-4000-8000-000000000005")
TRACE_ID = UUID("86700000-0000-4000-8000-000000000006")


class ForeignProjectionStatusService:
    async def status(self, *, context: object, projection_kind: str):
        del context
        return ProjectionStatusSnapshot(
            workspace_id=FOREIGN_WORKSPACE_ID,
            environment_id=FOREIGN_ENVIRONMENT_ID,
            projection_kind=projection_kind,
            publication_status="unpublished",
        )


class EmptyIngestionHistoryService:
    async def list_runs(self, *, context: object, limit: int, after: object | None):
        del context, limit, after
        return IngestionHistoryPage(items=(), has_more=False)

    async def list_processing_history(
        self,
        *,
        context: object,
        limit: int,
        after: object | None,
    ):
        del context, limit, after
        return IngestionHistoryPage(items=(), has_more=False)


def _services() -> CanonicalHealthStatusServices:
    boundary = TrustedContextBoundary.for_testing(
        issuer_id=UUID("86700000-0000-4000-8000-000000000007"),
        secret=b"issue-867-health-context-secret-0001",
    )
    return CanonicalHealthStatusServices(
        projection_status_service=ForeignProjectionStatusService(),
        ingestion_history_service=EmptyIngestionHistoryService(),
        context_factory=lambda workspace_id, environment_id: boundary.worker(
            scope=EnvironmentScope(
                workspace_id=workspace_id,
                environment_id=environment_id,
            ),
            service_principal_id=SERVICE_PRINCIPAL_ID,
            purpose=PersistencePurpose("health_summary"),
            operation=PersistenceOperation("inspect_health_summary"),
            trace_id=TRACE_ID,
        ),
    )


@pytest.mark.asyncio
async def test_foreign_projection_snapshot_returns_unavailable_health() -> None:
    result = await _services().publication_status(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
    )

    assert result.status is HealthStatus.UNAVAILABLE
    assert result.code == "publication.unavailable"
    assert result.provider_name == "cognee"
