from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

import pytest

from spine.application.health_summary import CanonicalHealthStatusServices, HealthStatus
from spine.application.ingestion_history import (
    IngestionHistoryPage,
    IngestionRunHistorySnapshot,
)
from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
    TrustedPersistenceContext,
)
from spine.application.projection_control import (
    ProjectionAsOfSnapshot,
    ProjectionStatusSnapshot,
)
from spine.domain.ingestion import IngestionRunState
from spine.domain.projections import ProjectionFreshness, ProjectionHealth
from spine.infrastructure.persistence.contexts import TrustedContextBoundary


WORKSPACE_ID = UUID("86700000-0000-4000-8000-000000000001")
ENVIRONMENT_ID = UUID("86700000-0000-4000-8000-000000000002")
FOREIGN_WORKSPACE_ID = UUID("86700000-0000-4000-8000-000000000003")
FOREIGN_ENVIRONMENT_ID = UUID("86700000-0000-4000-8000-000000000004")
SERVICE_PRINCIPAL_ID = UUID("86700000-0000-4000-8000-000000000005")
TRACE_ID = UUID("86700000-0000-4000-8000-000000000006")
NOW = datetime(2026, 11, 2, 17, tzinfo=timezone.utc)


@dataclass
class StaticProjectionStatusService:
    snapshot: ProjectionStatusSnapshot
    scopes: list[EnvironmentScope] = field(default_factory=list)

    async def status(
        self, *, context: TrustedPersistenceContext, projection_kind: str
    ) -> ProjectionStatusSnapshot:
        assert projection_kind == "qa"
        assert isinstance(context.scope, EnvironmentScope)
        self.scopes.append(context.scope)
        return self.snapshot


@dataclass
class StaticIngestionHistoryService:
    items: tuple[IngestionRunHistorySnapshot, ...] = ()
    scopes: list[EnvironmentScope] = field(default_factory=list)

    async def list_runs(
        self,
        *,
        context: TrustedPersistenceContext,
        limit: int,
        after: object | None,
    ) -> IngestionHistoryPage[IngestionRunHistorySnapshot]:
        assert isinstance(context.scope, EnvironmentScope)
        assert limit == 1
        assert after is None
        self.scopes.append(context.scope)
        return IngestionHistoryPage(items=self.items[:1], has_more=False)

    async def list_processing_history(
        self,
        *,
        context: TrustedPersistenceContext,
        limit: int,
        after: object | None,
    ) -> IngestionHistoryPage[object]:
        del context, limit, after
        return IngestionHistoryPage(items=(), has_more=False)


def _context_factory():
    boundary = TrustedContextBoundary.for_testing(
        issuer_id=UUID("86700000-0000-4000-8000-000000000007"),
        secret=b"issue-867-health-context-secret-0001",
    )

    def issue(scope: EnvironmentScope) -> TrustedPersistenceContext:
        return boundary.worker(
            scope=scope,
            service_principal_id=SERVICE_PRINCIPAL_ID,
            purpose=PersistencePurpose("health_summary"),
            operation=PersistenceOperation("inspect_health_summary"),
            trace_id=TRACE_ID,
        )

    return issue


def _published_projection(
    *,
    workspace_id: UUID = WORKSPACE_ID,
    environment_id: UUID = ENVIRONMENT_ID,
    freshness: ProjectionFreshness = ProjectionFreshness.CURRENT,
    health: ProjectionHealth = ProjectionHealth.HEALTHY,
) -> ProjectionStatusSnapshot:
    boundary_id = UUID("86700000-0000-4000-8000-000000000010")
    return ProjectionStatusSnapshot(
        workspace_id=workspace_id,
        environment_id=environment_id,
        projection_kind="qa",
        publication_status="published",
        snapshot_id=UUID("86700000-0000-4000-8000-000000000011"),
        activation_id=UUID("86700000-0000-4000-8000-000000000012"),
        canonical_boundary_id=boundary_id,
        as_of=ProjectionAsOfSnapshot(
            canonical_boundary_id=boundary_id,
            accepted_through_generation=1,
            boundary_time=NOW,
        ),
        activation_generation=1,
        projection_config_version_id=UUID(
            "86700000-0000-4000-8000-000000000013"
        ),
        projection_config_version=1,
        freshness=freshness,
        health=health,
        drift_count=0,
    )


def _unpublished_projection(
    *,
    workspace_id: UUID = WORKSPACE_ID,
    environment_id: UUID = ENVIRONMENT_ID,
) -> ProjectionStatusSnapshot:
    return ProjectionStatusSnapshot(
        workspace_id=workspace_id,
        environment_id=environment_id,
        projection_kind="qa",
        publication_status="unpublished",
    )


def _ingestion_run(state: IngestionRunState) -> IngestionRunHistorySnapshot:
    return IngestionRunHistorySnapshot(
        run_id=UUID("86700000-0000-4000-8000-000000000020"),
        requested_at=NOW,
        run_state=state,
        items=(),
    )


def _services(
    *,
    projection: ProjectionStatusSnapshot | None = None,
    ingestion_items: tuple[IngestionRunHistorySnapshot, ...] = (),
) -> CanonicalHealthStatusServices:
    return CanonicalHealthStatusServices(
        projection_status_service=StaticProjectionStatusService(
            projection or _published_projection()
        ),
        ingestion_history_service=StaticIngestionHistoryService(ingestion_items),
        context_factory=_context_factory(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("projection", "expected_status", "expected_code"),
    [
        (_published_projection(), HealthStatus.READY, "publication.ready"),
        (
            _published_projection(freshness=ProjectionFreshness.STALE),
            HealthStatus.STALE,
            "publication.stale",
        ),
        (
            _published_projection(health=ProjectionHealth.DEGRADED),
            HealthStatus.DEGRADED,
            "publication.degraded",
        ),
        (
            _unpublished_projection(),
            HealthStatus.DEGRADED,
            "publication.unpublished",
        ),
        (
            ProjectionStatusSnapshot.unavailable(
                workspace_id=WORKSPACE_ID,
                environment_id=ENVIRONMENT_ID,
                projection_kind="qa",
            ),
            HealthStatus.UNAVAILABLE,
            "publication.unavailable",
        ),
    ],
    ids=["ready", "stale", "degraded", "unpublished", "unavailable"],
)
async def test_projection_state_maps_bounded_health(
    projection: ProjectionStatusSnapshot,
    expected_status: HealthStatus,
    expected_code: str,
) -> None:
    result = await _services(projection=projection).publication_status(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
    )

    assert result.status is expected_status
    assert result.code == expected_code
    assert result.provider_name is None


@pytest.mark.asyncio
async def test_foreign_projection_snapshot_returns_unavailable_health() -> None:
    result = await _services(
        projection=_unpublished_projection(
            workspace_id=FOREIGN_WORKSPACE_ID,
            environment_id=FOREIGN_ENVIRONMENT_ID,
        )
    ).publication_status(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
    )

    assert result.status is HealthStatus.UNAVAILABLE
    assert result.code == "publication.unavailable"
    assert result.provider_name is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "expected_status", "expected_code"),
    [
        (None, HealthStatus.READY, "ingestion.ready"),
        (IngestionRunState.REQUESTED, HealthStatus.READY, "ingestion.ready"),
        (IngestionRunState.RECEIVING, HealthStatus.READY, "ingestion.ready"),
        (IngestionRunState.PROCESSING, HealthStatus.READY, "ingestion.ready"),
        (IngestionRunState.SUCCEEDED, HealthStatus.READY, "ingestion.ready"),
        (
            IngestionRunState.PARTIALLY_SUCCEEDED,
            HealthStatus.DEGRADED,
            "ingestion.partial",
        ),
        (IngestionRunState.FAILED, HealthStatus.DEGRADED, "ingestion.degraded"),
        (IngestionRunState.CANCELLED, HealthStatus.DEGRADED, "ingestion.degraded"),
    ],
)
async def test_ingestion_run_state_maps_bounded_health(
    state: IngestionRunState | None,
    expected_status: HealthStatus,
    expected_code: str,
) -> None:
    items = () if state is None else (_ingestion_run(state),)
    result = await _services(ingestion_items=items).ingestion_status(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
    )

    assert result.status is expected_status
    assert result.code == expected_code
    assert result.provider_name == "postgresql"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "expected_status"),
    [
        (IngestionRunState.REQUESTED, "accepted"),
        (IngestionRunState.RECEIVING, "running"),
        (IngestionRunState.PROCESSING, "running"),
        (IngestionRunState.SUCCEEDED, "completed"),
        (IngestionRunState.PARTIALLY_SUCCEEDED, "completed"),
        (IngestionRunState.FAILED, "failed"),
        (IngestionRunState.CANCELLED, "failed"),
    ],
)
async def test_ingestion_run_state_maps_last_operation(
    state: IngestionRunState,
    expected_status: Literal["accepted", "running", "completed", "failed"],
) -> None:
    result = await _services(
        ingestion_items=(_ingestion_run(state),)
    ).last_operation(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
    )

    assert result is not None
    assert result.operation == "ingestion"
    assert result.status == expected_status
    assert result.completed_at is None


@pytest.mark.asyncio
async def test_empty_ingestion_history_has_no_last_operation() -> None:
    result = await _services().last_operation(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
    )

    assert result is None
