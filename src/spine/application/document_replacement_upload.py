"""Application path for replacing content on an existing uploaded source."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterable
from dataclasses import dataclass
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field

from spine.application.document_ingestion import DocumentUploadCommand
from spine.application.ingestion_api import (
    DocumentIngestionService,
    IngestionSubmission,
)
from spine.application.persistence.context import (
    EnvironmentScope,
    TrustedPersistenceContext,
)
from spine.application.persistence.errors import OptimisticConflictError
from spine.application.persistence.unit_of_work import UnitOfWorkFactory
from spine.domain.sources import IdentityMode, RevisionKind


class DocumentReplacementUploadCommand(BaseModel):
    """Browser-owned replacement metadata with server-owned source identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_object_id: UUID
    expected_current_revision_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=255)
    declared_filename: str = Field(min_length=1, max_length=255)
    declared_media_type: str = Field(min_length=1, max_length=255)
    expected_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    expected_byte_length: int | None = Field(default=None, ge=0)


@dataclass(frozen=True, slots=True)
class DocumentReplacementUploadService:
    """Resolve a source in trusted scope, then use canonical ingestion with CAS."""

    uow_factory: UnitOfWorkFactory
    ingestion_service: DocumentIngestionService

    async def replace(
        self,
        *,
        context: TrustedPersistenceContext,
        command: DocumentReplacementUploadCommand,
        chunks: AsyncIterable[bytes],
    ) -> IngestionSubmission:
        scope = context.scope
        if not isinstance(scope, EnvironmentScope):
            raise ValueError(
                "Document replacement requires workspace and environment scope."
            )

        async with self.uow_factory(context) as uow:
            source = await uow.sources.resolve_source(command.source_object_id)
            current = await uow.canonical.resolve_current(command.source_object_id)
            if (
                source is None
                or (source.workspace_id, source.environment_id)
                != (scope.workspace_id, scope.environment_id)
                or source.source_kind != "document"
                or source.identity_mode is not IdentityMode.UPLOAD
                or source.upload_identity is None
            ):
                await uow.rollback()
                # Do not disclose whether a different workspace owns this object.
                raise OptimisticConflictError("Document replacement is unavailable.")
            upload_identity = source.upload_identity
            run_id = uuid5(
                NAMESPACE_URL,
                "spine:document-ingestion:v1:"
                f"{scope.workspace_id}:{scope.environment_id}:{upload_identity}:"
                f"{command.idempotency_key}:run",
            )
            previous_run = await uow.ingestion.derive_run_state(run_id)
            if (
                current is None
                or current.current_revision_id != command.expected_current_revision_id
                or current.current_revision_kind is not RevisionKind.CONTENT
            ) and previous_run is None:
                await uow.rollback()
                raise OptimisticConflictError(
                    "Document revision is stale or unavailable."
                )
            await uow.commit()

        digest = hashlib.sha256(
            f"{command.source_object_id}:{command.idempotency_key}".encode()
        ).hexdigest()[:32]
        ingestion_command = DocumentUploadCommand(
            workspace_id=scope.workspace_id,
            environment_id=scope.environment_id,
            command_identity=f"api.document.replacement.{digest}",
            idempotency_key=command.idempotency_key,
            upload_identity=upload_identity,
            purpose="document.upload.v1",
            declared_media_type=command.declared_media_type,
            declared_filename=command.declared_filename,
            expected_sha256=command.expected_sha256,
            expected_byte_length=command.expected_byte_length,
            expected_current_revision_id=command.expected_current_revision_id,
            source_created_at=source.created_at,
        )
        result = await self.ingestion_service.submit(
            context=context,
            command=ingestion_command,
            chunks=chunks,
        )
        if result.source_object_id != command.source_object_id:
            raise RuntimeError("Document replacement resolved an unexpected source.")
        return result


__all__ = ["DocumentReplacementUploadCommand", "DocumentReplacementUploadService"]
