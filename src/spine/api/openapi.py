"""Canonical OpenAPI composition independent of deployable adapter wiring."""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterable, Sequence
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
from spine.application.document_ingestion import DocumentUploadCommand
from spine.application.ingestion_api import (
    DocumentIngestionService,
    IngestionStatusSnapshot,
    IngestionSubmission,
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
from spine.application.authorized_ask import AuthorizedAskService
from spine.application.citation_inspector import CitationInspectorService
from spine.application.feedback import FeedbackService
from spine.application.qa_run_history import QARunHistoryService
from spine.application.qa_events import QAEventStream
from spine.application.qa_events import DisclosureAuthorizer
from spine.application.authorized_ask import AuthorizedAskResult
from spine.domain.qa_events import QAEvent
from spine.application.parsed_content_inspection import ParsedContentInspectionService
from spine.application.operational_correlation import (
    UnavailableOperationalCorrelationReader,
)
from spine.application.processing_commands import (
    ProcessingCommandResult,
    ProcessingCommandService,
    ReprocessProcessingCommand,
    RetryProcessingCommand,
)
from spine.application.document_lifecycle import (
    DocumentLifecycleCommandService,
    ReplaceDocumentCommand,
    ReplacementProcessingIntent,
    SourceLifecycleOperationReceipt,
    TombstoneDocumentCommand,
)
from spine.application.persistence.context import TrustedPersistenceContext
from spine.auth import RoutePolicyRegistry


class _ContractAuthorizationComposer:
    """Bind route metadata without providing an executable auth adapter."""

    def bind_route(self, route_id: str) -> object:
        del route_id
        return object()


class _UnavailableDocumentIngestionService:
    """Expose upload/status routes without making the schema app executable."""

    async def submit(
        self,
        *,
        context: TrustedPersistenceContext,
        command: DocumentUploadCommand,
        chunks: AsyncIterable[bytes],
    ) -> IngestionSubmission:
        del context, command, chunks
        raise RuntimeError("The OpenAPI composition is not executable.")

    async def status(
        self,
        *,
        context: TrustedPersistenceContext,
        run_id: UUID,
        item_id: UUID,
    ) -> IngestionStatusSnapshot | None:
        del context, run_id, item_id
        raise RuntimeError("The OpenAPI composition is not executable.")


class _UnavailableProcessingCommandService:
    async def retry(
        self,
        *,
        context: TrustedPersistenceContext,
        command: RetryProcessingCommand,
    ) -> ProcessingCommandResult:
        del context, command
        raise RuntimeError("The OpenAPI composition is not executable.")

    async def reprocess(
        self,
        *,
        context: TrustedPersistenceContext,
        command: ReprocessProcessingCommand,
    ) -> ProcessingCommandResult:
        del context, command
        raise RuntimeError("The OpenAPI composition is not executable.")

    async def replacement(
        self,
        *,
        context: TrustedPersistenceContext,
        intent: ReplacementProcessingIntent,
    ) -> ProcessingCommandResult:
        del context, intent
        raise RuntimeError("The OpenAPI composition is not executable.")

    async def status(
        self,
        *,
        context: TrustedPersistenceContext,
        operation_id: UUID,
    ) -> ProcessingCommandResult | None:
        del context, operation_id
        raise RuntimeError("The OpenAPI composition is not executable.")


class _UnavailableDocumentLifecycleCommandService:
    async def replace(
        self,
        *,
        context: TrustedPersistenceContext,
        command: ReplaceDocumentCommand,
    ) -> SourceLifecycleOperationReceipt:
        del context, command
        raise RuntimeError("The OpenAPI composition is not executable.")

    async def tombstone(
        self,
        *,
        context: TrustedPersistenceContext,
        command: TombstoneDocumentCommand,
    ) -> SourceLifecycleOperationReceipt:
        del context, command
        raise RuntimeError("The OpenAPI composition is not executable.")

    async def status(
        self,
        *,
        context: TrustedPersistenceContext,
        operation_id: UUID,
    ) -> SourceLifecycleOperationReceipt | None:
        del context, operation_id
        raise RuntimeError("The OpenAPI composition is not executable.")

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


class _UnavailableAuthorizedAskService:
    async def ask(self, command):
        del command
        raise RuntimeError("The OpenAPI composition is not executable.")

    async def cancel(self, command):
        del command
        raise RuntimeError("The OpenAPI composition is not executable.")


class _UnavailableCitationInspectorService:
    async def inspect(self, **kwargs):
        del kwargs
        raise RuntimeError("The OpenAPI composition is not executable.")


class _UnavailableFeedbackService:
    async def submit(self, **kwargs):
        del kwargs
        raise RuntimeError("The OpenAPI composition is not executable.")


class _UnavailableQARunHistoryService:
    async def list_runs(self, **kwargs):
        del kwargs
        raise RuntimeError("The OpenAPI composition is not executable.")

    async def get_run(self, **kwargs):
        del kwargs
        raise RuntimeError("The OpenAPI composition is not executable.")


class _UnavailableQAStreamService:
    async def publish_result(self, result: AuthorizedAskResult) -> QAEvent | None:
        del result
        raise RuntimeError("The OpenAPI composition is not executable.")

    def open_stream(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        session_id: UUID,
        trace_id: UUID,
        run_id: UUID | None = None,
        message_id: UUID | None = None,
        disclosure_authorizer: DisclosureAuthorizer | None = None,
    ) -> QAEventStream:
        del (
            workspace_id,
            environment_id,
            session_id,
            trace_id,
            run_id,
            message_id,
            disclosure_authorizer,
        )
        raise RuntimeError("The OpenAPI composition is not executable.")


class _UnavailableParsedContentInspectionService:
    async def inspect(self, **kwargs):
        del kwargs
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
        document_ingestion_service=cast(
            DocumentIngestionService,
            _UnavailableDocumentIngestionService(),
        ),
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
        authorized_ask_service=cast(
            AuthorizedAskService, _UnavailableAuthorizedAskService()
        ),
        citation_inspector_service=cast(
            CitationInspectorService, _UnavailableCitationInspectorService()
        ),
        feedback_service=cast(FeedbackService, _UnavailableFeedbackService()),
        qa_run_history_service=cast(
            QARunHistoryService, _UnavailableQARunHistoryService()
        ),
        qa_run_history_cursor_secret=secrets.token_bytes(32),
        qa_stream_service=_UnavailableQAStreamService(),
        parsed_content_inspection_service=cast(
            ParsedContentInspectionService,
            _UnavailableParsedContentInspectionService(),
        ),
        parsed_content_cursor_secret=secrets.token_bytes(32),
        processing_command_service=cast(
            ProcessingCommandService,
            _UnavailableProcessingCommandService(),
        ),
        document_lifecycle_command_service=cast(
            DocumentLifecycleCommandService,
            _UnavailableDocumentLifecycleCommandService(),
        ),
        operational_correlation_reader=UnavailableOperationalCorrelationReader(),
    )


__all__ = ["create_openapi_app"]
