"""Safe application-facing health status contracts.

Health metadata is operational information, not evidence or tenant content.  The
status services behind this seam must therefore return bounded, content-free
snapshots only.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Literal, Protocol
from uuid import UUID


class HealthStatus(str, Enum):
    READY = "ready"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    STALE = "stale"


SAFE_HEALTH_CODES = frozenset(
    {
        "authorization.ready",
        "local.ready",
        "oidc.ready",
        "oidc.check_deferred",
        "oidc.stale_keys",
        "oidc.provider_unavailable",
        "oidc.provider_incompatible",
        "oidc.runtime_closed",
        "authorization.degraded",
        "authorization.unavailable",
        "authorization.stale",
        "authorization.store_unavailable",
        "authorization.provider_unavailable",
        "authorization.provider_incompatible",
        "dependency.unavailable",
        "ingestion.ready",
        "ingestion.degraded",
        "ingestion.partial",
        "ingestion.unavailable",
        "ingestion.stale",
        "publication.ready",
        "publication.degraded",
        "publication.unavailable",
        "publication.stale",
    })
SAFE_PROVIDER_NAMES = frozenset(
    {
        "local",
        "oidc",
        "postgresql",
        "filesystem",
        "cognee",
        "neo4j",
        "pgvector",
        "memory",
    }
)
SAFE_OPERATION_NAMES = frozenset(
    {"authorization", "ingestion", "publication", "reconciliation"}
)
SAFE_OPERATION_STATUSES = frozenset(
    {"accepted", "running", "completed", "failed", "stale", "unavailable"}
)

HealthOperationName = Literal[
    "authorization", "ingestion", "publication", "reconciliation"
]
HealthOperationStatus = Literal[
    "accepted", "running", "completed", "failed", "stale", "unavailable"
]


@dataclass(frozen=True, slots=True)
class HealthComponentSnapshot:
    """A bounded status returned by a projection or ingestion status service."""

    status: HealthStatus
    code: str
    provider_name: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, HealthStatus):
            raise TypeError("health status must use the canonical enum")
        if self.code not in SAFE_HEALTH_CODES:
            raise ValueError("health code is not allowlisted")
        if self.provider_name is not None and self.provider_name not in SAFE_PROVIDER_NAMES:
            raise ValueError("provider name is not allowlisted")


@dataclass(frozen=True, slots=True)
class HealthOperationSnapshot:
    """Safe metadata for the most recent operational attempt."""

    operation: HealthOperationName
    status: HealthOperationStatus
    completed_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.operation not in SAFE_OPERATION_NAMES:
            raise ValueError("operation name is not allowlisted")
        if self.status not in SAFE_OPERATION_STATUSES:
            raise ValueError("operation status is not allowlisted")
        if self.completed_at is not None and (
            self.completed_at.tzinfo is None or self.completed_at.utcoffset() is None
        ):
            raise ValueError("completed_at must be timezone-aware")


class HealthStatusServices(Protocol):
    """Read-only, tenant-scoped operational status ports."""

    async def publication_status(
        self, *, workspace_id: UUID, environment_id: UUID
    ) -> HealthComponentSnapshot: ...

    async def ingestion_status(
        self, *, workspace_id: UUID, environment_id: UUID
    ) -> HealthComponentSnapshot: ...

    async def last_operation(
        self, *, workspace_id: UUID, environment_id: UUID
    ) -> HealthOperationSnapshot | None: ...


@dataclass(frozen=True, slots=True)
class UnavailableHealthStatusServices:
    """Safe fallback when operational status adapters are not composed."""

    async def publication_status(
        self, *, workspace_id: UUID, environment_id: UUID
    ) -> HealthComponentSnapshot:
        del workspace_id, environment_id
        return HealthComponentSnapshot(
            status=HealthStatus.UNAVAILABLE,
            code="dependency.unavailable",
        )

    async def ingestion_status(
        self, *, workspace_id: UUID, environment_id: UUID
    ) -> HealthComponentSnapshot:
        del workspace_id, environment_id
        return HealthComponentSnapshot(
            status=HealthStatus.UNAVAILABLE,
            code="dependency.unavailable",
        )

    async def last_operation(
        self, *, workspace_id: UUID, environment_id: UUID
    ) -> HealthOperationSnapshot | None:
        del workspace_id, environment_id
        return None


@dataclass(frozen=True, slots=True)
class HealthSummarySnapshot:
    """Safe application result assembled independently of HTTP transport."""

    status: HealthStatus
    build_version: str
    authentication_mode: Literal["local", "oidc"] | None
    configuration_version: str | None
    authorization: HealthComponentSnapshot
    publication: HealthComponentSnapshot
    ingestion: HealthComponentSnapshot
    last_operation: HealthOperationSnapshot | None


async def collect_health_summary(
    *,
    services: HealthStatusServices,
    workspace_id: UUID,
    environment_id: UUID,
    authorization: HealthComponentSnapshot,
    authentication_mode: Literal["local", "oidc"] | None,
    configuration_version: str | None,
    build_version: str,
    timeout_seconds: float,
) -> HealthSummarySnapshot:
    """Collect bounded operational status through application ports."""

    if timeout_seconds <= 0 or timeout_seconds > 5:
        raise ValueError("health summary timeout must be between 0 and 5 seconds")
    publication, ingestion, last_operation = await asyncio.gather(
        _bounded_component(
            services.publication_status(
                workspace_id=workspace_id,
                environment_id=environment_id,
            ),
            timeout_seconds=timeout_seconds,
        ),
        _bounded_component(
            services.ingestion_status(
                workspace_id=workspace_id,
                environment_id=environment_id,
            ),
            timeout_seconds=timeout_seconds,
        ),
        _bounded_operation(
            services.last_operation(
                workspace_id=workspace_id,
                environment_id=environment_id,
            ),
            timeout_seconds=timeout_seconds,
        ),
    )
    return HealthSummarySnapshot(
        status=_overall_status(authorization, publication, ingestion),
        build_version=build_version,
        authentication_mode=authentication_mode,
        configuration_version=configuration_version,
        authorization=authorization,
        publication=publication,
        ingestion=ingestion,
        last_operation=last_operation,
    )


async def _bounded_component(
    operation: Awaitable[HealthComponentSnapshot], *, timeout_seconds: float
) -> HealthComponentSnapshot:
    try:
        result = await asyncio.wait_for(operation, timeout=timeout_seconds)
        if type(result) is not HealthComponentSnapshot:
            raise TypeError("health service returned a non-canonical component")
        return result
    except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        return HealthComponentSnapshot(
            status=HealthStatus.UNAVAILABLE,
            code="dependency.unavailable",
        )


async def _bounded_operation(
    operation: Awaitable[HealthOperationSnapshot | None], *, timeout_seconds: float
) -> HealthOperationSnapshot | None:
    try:
        result = await asyncio.wait_for(operation, timeout=timeout_seconds)
        if result is not None and type(result) is not HealthOperationSnapshot:
            raise TypeError("health service returned a non-canonical operation")
        return result
    except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        return None


def _overall_status(
    authorization: HealthComponentSnapshot,
    publication: HealthComponentSnapshot,
    ingestion: HealthComponentSnapshot,
) -> HealthStatus:
    statuses = (authorization.status, publication.status, ingestion.status)
    if HealthStatus.UNAVAILABLE in statuses:
        return HealthStatus.UNAVAILABLE
    if HealthStatus.STALE in statuses:
        return HealthStatus.STALE
    if HealthStatus.DEGRADED in statuses:
        return HealthStatus.DEGRADED
    return HealthStatus.READY


__all__ = [
    "HealthOperationName",
    "HealthComponentSnapshot",
    "HealthOperationSnapshot",
    "HealthOperationStatus",
    "HealthSummarySnapshot",
    "HealthStatus",
    "HealthStatusServices",
    "SAFE_HEALTH_CODES",
    "SAFE_OPERATION_NAMES",
    "SAFE_OPERATION_STATUSES",
    "SAFE_PROVIDER_NAMES",
    "UnavailableHealthStatusServices",
    "collect_health_summary",
]
