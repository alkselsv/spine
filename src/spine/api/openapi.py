"""Canonical OpenAPI composition independent of deployable adapter wiring."""

from __future__ import annotations

import secrets
from collections.abc import Sequence
from typing import cast
from uuid import UUID

from fastapi import FastAPI

from spine.api.authorization import (
    ApiAuthorizationReadiness,
    ApiAuthorizationRuntime,
    RouteAuthorizationComposer,
)
from spine.api.main import create_app
from spine.application.batch_ingestion import (
    BatchDocumentInput,
    BatchDocumentUploadCommand,
)
from spine.application.batch_ingestion_api import (
    BatchDocumentIngestionService,
    BatchIngestionSnapshot,
)
from spine.application.ingestion_history import (
    IngestionHistoryPage,
    IngestionHistoryPosition,
)
from spine.application.ingestion_history import IngestionHistoryService
from spine.application.persistence.context import TrustedPersistenceContext
from spine.auth import RoutePolicyRegistry


class _ContractAuthorizationComposer:
    """Bind route metadata without providing an executable auth adapter."""

    def bind_route(self, route_id: str) -> object:
        del route_id
        return object()


class _UnavailableBatchDocumentIngestionService:
    async def submit(
        self,
        *,
        context: TrustedPersistenceContext,
        command: BatchDocumentUploadCommand,
        items: Sequence[BatchDocumentInput],
    ) -> BatchIngestionSnapshot:
        del context, command, items
        raise RuntimeError("The OpenAPI composition is not executable.")

    async def status(
        self,
        *,
        context: TrustedPersistenceContext,
        run_id: UUID,
    ) -> BatchIngestionSnapshot | None:
        del context, run_id
        raise RuntimeError("The OpenAPI composition is not executable.")


class _UnavailableIngestionHistoryService:
    async def list_runs(
        self,
        *,
        context: TrustedPersistenceContext,
        limit: int,
        after: IngestionHistoryPosition | None,
    ) -> IngestionHistoryPage:
        del context, limit, after
        raise RuntimeError("The OpenAPI composition is not executable.")

    async def list_processing_history(
        self,
        *,
        context: TrustedPersistenceContext,
        limit: int,
        after: IngestionHistoryPosition | None,
    ) -> IngestionHistoryPage:
        del context, limit, after
        raise RuntimeError("The OpenAPI composition is not executable.")

async def _contract_readiness() -> ApiAuthorizationReadiness:
    return ApiAuthorizationReadiness(
        ready=False,
        code="authorization.unavailable",
    )


def create_openapi_app() -> FastAPI:
    """Compose every published route for deterministic contract generation."""

    route_policies = RoutePolicyRegistry()
    authorization_runtime = ApiAuthorizationRuntime(
        composer=cast(
            RouteAuthorizationComposer,
            _ContractAuthorizationComposer(),
        ),
        route_policies=route_policies,
        readiness_probe=_contract_readiness,
    )
    return create_app(
        authorization_runtime=authorization_runtime,
        batch_document_ingestion_service=cast(
            BatchDocumentIngestionService,
            _UnavailableBatchDocumentIngestionService(),
        ),
        ingestion_history_service=cast(
            IngestionHistoryService,
            _UnavailableIngestionHistoryService(),
        ),
        ingestion_history_cursor_secret=secrets.token_bytes(32),
    )


__all__ = ["create_openapi_app"]
