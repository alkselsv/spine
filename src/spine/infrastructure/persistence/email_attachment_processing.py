"""Append-only persistence for email attachment processing receipts."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import json
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert

from spine.application.email_attachment_ingestion import (
    EmailAttachmentProcessingConflict,
    EmailAttachmentProcessingPersistenceResult,
)
from spine.application.persistence.context import (
    EnvironmentScope,
    TrustedContextVerifier,
    TrustedPersistenceContext,
)
from spine.application.persistence.errors import InvalidPersistenceContextError
from spine.domain.artifacts.email_attachment import (
    EmailAttachmentProcessingReceipt,
    email_attachment_processing_effect_digest,
)
from spine.infrastructure.persistence.postgresql_mappings import (
    email_attachment_processing_receipts,
)


class InMemoryEmailAttachmentProcessingRepository:
    """Tenant-scoped deterministic fake used by offline application tests."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._records: dict[
            tuple[UUID, UUID, UUID, str, str], EmailAttachmentProcessingReceipt
        ] = {}

    async def resolve(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        attachment_artifact_id: UUID,
        processor_profile: str,
        authority_digest: str,
    ) -> EmailAttachmentProcessingReceipt | None:
        async with self._lock:
            return self._records.get(
                (
                    workspace_id,
                    environment_id,
                    attachment_artifact_id,
                    processor_profile,
                    authority_digest,
                )
            )

    async def persist(
        self, receipt: EmailAttachmentProcessingReceipt
    ) -> EmailAttachmentProcessingPersistenceResult:
        key = (
            receipt.workspace_id,
            receipt.environment_id,
            receipt.attachment_artifact_id,
            receipt.processor_profile,
            receipt.authority_digest,
        )
        async with self._lock:
            prior = self._records.get(key)
            if prior is not None:
                if email_attachment_processing_effect_digest(
                    prior
                ) != email_attachment_processing_effect_digest(receipt):
                    raise EmailAttachmentProcessingConflict()
                return EmailAttachmentProcessingPersistenceResult(prior, replay=True)
            self._records[key] = receipt
            return EmailAttachmentProcessingPersistenceResult(receipt, replay=False)


class PostgreSQLEmailAttachmentProcessingRepository:
    """Tenant-scoped append-only persistence for exact processing lineage."""

    def __init__(
        self,
        session_factory: Callable[[], Any],
        *,
        context_factory: Callable[[UUID, UUID], TrustedPersistenceContext],
        context_verifier: TrustedContextVerifier,
    ) -> None:
        self._session_factory = session_factory
        self._context_factory = context_factory
        self._context_verifier = context_verifier

    async def resolve(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        attachment_artifact_id: UUID,
        processor_profile: str,
        authority_digest: str,
    ) -> EmailAttachmentProcessingReceipt | None:
        async with self._session_factory() as session:
            await self._bind_tenant_scope(session, workspace_id, environment_id)
            row = (
                await session.execute(
                    select(email_attachment_processing_receipts.c.record).where(
                        email_attachment_processing_receipts.c.workspace_id
                        == workspace_id,
                        email_attachment_processing_receipts.c.environment_id
                        == environment_id,
                        email_attachment_processing_receipts.c.attachment_artifact_id
                        == attachment_artifact_id,
                        email_attachment_processing_receipts.c.processor_profile
                        == processor_profile,
                        email_attachment_processing_receipts.c.authority_digest
                        == authority_digest,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            receipt = EmailAttachmentProcessingReceipt.model_validate_json(
                json.dumps(row)
            )
            if (
                receipt.workspace_id != workspace_id
                or receipt.environment_id != environment_id
                or receipt.attachment_artifact_id != attachment_artifact_id
                or receipt.processor_profile != processor_profile
                or receipt.authority_digest != authority_digest
            ):
                raise EmailAttachmentProcessingConflict()
            return receipt

    async def persist(
        self, receipt: EmailAttachmentProcessingReceipt
    ) -> EmailAttachmentProcessingPersistenceResult:
        async with self._session_factory() as session:
            async with session.begin():
                await self._bind_tenant_scope(
                    session, receipt.workspace_id, receipt.environment_id
                )
                inserted = await session.execute(
                    insert(email_attachment_processing_receipts)
                    .values(
                        workspace_id=receipt.workspace_id,
                        environment_id=receipt.environment_id,
                        processing_receipt_id=receipt.processing_receipt_id,
                        attachment_artifact_id=receipt.attachment_artifact_id,
                        processor_profile=receipt.processor_profile,
                        authority_digest=receipt.authority_digest,
                        disposition=receipt.disposition.value,
                        child_source_object_id=receipt.child_source_object_id,
                        child_source_revision_id=receipt.child_source_revision_id,
                        child_processing_receipt_id=receipt.child_processing_receipt_id,
                        parsed_artifact_id=receipt.parsed_artifact_id,
                        parsed_artifact_digest=receipt.parsed_artifact_digest,
                        receipt_digest=receipt.receipt_sha256,
                        record=receipt.model_dump(mode="json"),
                        completed_at=receipt.completed_at,
                    )
                    .on_conflict_do_nothing()
                    .returning(
                        email_attachment_processing_receipts.c.processing_receipt_id
                    )
                )
                if inserted.scalar_one_or_none() is not None:
                    return EmailAttachmentProcessingPersistenceResult(
                        receipt, replay=False
                    )
                row = (
                    await session.execute(
                        select(email_attachment_processing_receipts.c.record).where(
                            email_attachment_processing_receipts.c.workspace_id
                            == receipt.workspace_id,
                            email_attachment_processing_receipts.c.environment_id
                            == receipt.environment_id,
                            email_attachment_processing_receipts.c.attachment_artifact_id
                            == receipt.attachment_artifact_id,
                            email_attachment_processing_receipts.c.processor_profile
                            == receipt.processor_profile,
                            email_attachment_processing_receipts.c.authority_digest
                            == receipt.authority_digest,
                        )
                    )
                ).scalar_one_or_none()
                if row is None:
                    raise EmailAttachmentProcessingConflict()
                prior = EmailAttachmentProcessingReceipt.model_validate_json(
                    json.dumps(row)
                )
                if (
                    prior.processing_receipt_id != receipt.processing_receipt_id
                    or email_attachment_processing_effect_digest(prior)
                    != email_attachment_processing_effect_digest(receipt)
                ):
                    raise EmailAttachmentProcessingConflict()
                return EmailAttachmentProcessingPersistenceResult(prior, replay=True)

    async def _bind_tenant_scope(
        self, session: Any, workspace_id: UUID, environment_id: UUID
    ) -> None:
        try:
            context = self._context_verifier.verify(
                self._context_factory(workspace_id, environment_id)
            )
        except Exception:
            raise InvalidPersistenceContextError(
                "Email attachment processing persistence context is invalid."
            ) from None
        if not isinstance(context.scope, EnvironmentScope) or (
            context.scope.workspace_id,
            context.scope.environment_id,
        ) != (workspace_id, environment_id):
            raise InvalidPersistenceContextError(
                "Email attachment processing scope does not match its tenant."
            )
        await session.execute(
            text(
                "SELECT set_config('spine.workspace_id', :workspace_id, true), "
                "set_config('spine.environment_id', :environment_id, true), "
                "set_config('spine.acting_subject_id', :acting_subject_id, true), "
                "set_config('spine.service_principal_id', :service_principal_id, true), "
                "set_config('spine.purpose', :purpose, true), "
                "set_config('spine.operation', :operation, true), "
                "set_config('spine.trace_id', :trace_id, true)"
            ),
            {
                "workspace_id": str(workspace_id),
                "environment_id": str(environment_id),
                "acting_subject_id": str(context.acting_subject_id or ""),
                "service_principal_id": str(context.service_principal_id or ""),
                "purpose": context.purpose.value,
                "operation": context.operation.value,
                "trace_id": str(context.trace_id),
            },
        )


__all__ = [
    "InMemoryEmailAttachmentProcessingRepository",
    "PostgreSQLEmailAttachmentProcessingRepository",
]
