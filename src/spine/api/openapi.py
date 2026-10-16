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
from spine.application.document_queries import (
    DocumentPage,
    DocumentPosition,
    DocumentQueryFilters,
    DocumentQueryRequester,
    DocumentQueryService,
    DocumentSnapshot,
    RevisionPosition,
    RevisionSnapshot,
)
from spine.application.projection_control import (
    UnavailableProjectionRecoveryService,
    UnavailableProjectionStatusService,
)
from spine.application.qa_sessions import QASessionService
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


class _UnavailableDocumentQueryService:
    async def list_documents(
        self,
        *,
        context: TrustedPersistenceContext,
        requester: DocumentQueryRequester,
        limit: int,
        after: DocumentPosition | None,
        filters: DocumentQueryFilters | None = None,
    ) -> DocumentPage[DocumentSnapshot]:
        del context, requester, limit, after, filters
        raise RuntimeError("The OpenAPI composition is not executable.")

    async def get_document(
        self,
        *,
        context: TrustedPersistenceContext,
        requester: DocumentQueryRequester,
        source_object_id: UUID,
    ) -> DocumentSnapshot | None:
        del context, requester, source_object_id
        raise RuntimeError("The OpenAPI composition is not executable.")

    async def list_revisions(
        self,
        *,
        context: TrustedPersistenceContext,
        requester: DocumentQueryRequester,
        source_object_id: UUID,
        limit: int,
        after: RevisionPosition | None,
    ) -> DocumentPage[RevisionSnapshot] | None:
        del context, requester, source_object_id, limit, after
        raise RuntimeError("The OpenAPI composition is not executable.")


class _UnavailableQASessionService:
    async def create_session(self, *, context, requester, idempotency_key):
        del context, requester, idempotency_key
        raise RuntimeError("The OpenAPI composition is not executable.")

    async def list_sessions(self, *, context, limit, after):
        del context, limit, after
        raise RuntimeError("The OpenAPI composition is not executable.")

    async def get_session(self, *, context, session_id):
        del context, session_id
        raise RuntimeError("The OpenAPI composition is not executable.")

    async def restore_session(self, *, context, session_id, expected_version, idempotency_key):
        del context, session_id, expected_version, idempotency_key
        raise RuntimeError("The OpenAPI composition is not executable.")

    async def create_pending_message(self, *, context, requester, session_id, question, run_id, idempotency_key):
        del context, requester, session_id, question, run_id, idempotency_key
        raise RuntimeError("The OpenAPI composition is not executable.")

    async def list_messages(self, *, context, requester, session_id, limit, after):
        del context, requester, session_id, limit, after
        raise RuntimeError("The OpenAPI composition is not executable.")

    async def get_message(self, *, context, requester, session_id, message_id):
        del context, requester, session_id, message_id
        raise RuntimeError("The OpenAPI composition is not executable.")

    async def link_message_to_run(self, *, context, session_id, message_id, run_id, idempotency_key):
        del context, session_id, message_id, run_id, idempotency_key
        raise RuntimeError("The OpenAPI composition is not executable.")

    async def complete_message(self, *, context, session_id, message_id, answer, outcome, cancelled, idempotency_key):
        del context, session_id, message_id, answer, outcome, cancelled, idempotency_key
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
        document_query_service=cast(
            DocumentQueryService,
            _UnavailableDocumentQueryService(),
        ),
        document_query_cursor_secret=secrets.token_bytes(32),
        projection_status_service=UnavailableProjectionStatusService(),
        projection_recovery_service=UnavailableProjectionRecoveryService(),
        qa_session_service=cast(QASessionService, _UnavailableQASessionService()),
        qa_session_cursor_secret=secrets.token_bytes(32),
    )


__all__ = ["create_openapi_app"]
