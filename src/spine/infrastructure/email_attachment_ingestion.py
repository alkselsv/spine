"""Trusted object-store adapter for governed email attachment ingestion."""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timedelta, timezone
from uuid import UUID

from spine.application.email_attachment_ingestion import (
    EmailAttachmentDocumentIngestion,
    EmailAttachmentIngestionCoordinator,
    EmailAttachmentOriginalMissing,
    EmailAttachmentProcessingDecision,
    EmailAttachmentProcessingRepository,
)
from spine.application.email_attachments import EmailAttachmentRepository
from spine.application.processing_disposition import (
    CurrentProcessingDispositionAuthority,
)
from spine.domain.artifacts.email_attachment import EmailAttachmentArtifact
from spine.infrastructure.object_storage.contracts import AuthorizedReadGrantRegistry
from spine.infrastructure.object_storage.errors import (
    IntegrityMismatch,
    ObjectCorrupt,
    ObjectNotFound,
)
from spine.infrastructure.object_storage.port import OriginalObjectStore


class ConfiguredEmailAttachmentProcessingAuthority:
    """Immutable deployment policy for child type and byte admission."""

    def __init__(self, decision: EmailAttachmentProcessingDecision) -> None:
        self._decision = decision

    async def resolve(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        attachment: EmailAttachmentArtifact,
    ) -> EmailAttachmentProcessingDecision | None:
        if (
            attachment.workspace_id != workspace_id
            or attachment.environment_id != environment_id
        ):
            return None
        return self._decision


class ObjectStoreEmailAttachmentOriginalReader:
    """Open the retained object through a one-shot, scope-bound capability."""

    def __init__(
        self,
        *,
        object_store: OriginalObjectStore,
        grant_registry: AuthorizedReadGrantRegistry,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        grant_lifetime: timedelta = timedelta(minutes=1),
    ) -> None:
        if grant_lifetime <= timedelta(0):
            raise ValueError("grant_lifetime must be positive")
        self._object_store = object_store
        self._grant_registry = grant_registry
        self._clock = clock
        self._grant_lifetime = grant_lifetime

    def read(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        attachment: EmailAttachmentArtifact,
        max_bytes: int,
        policy_version: str,
        authority_digest: str,
    ) -> AsyncIterator[bytes]:
        if (
            workspace_id != attachment.workspace_id
            or environment_id != attachment.environment_id
            or max_bytes <= 0
        ):
            raise ValueError("attachment read scope is invalid")
        return self._read(
            workspace_id=workspace_id,
            environment_id=environment_id,
            attachment=attachment,
            max_bytes=max_bytes,
            policy_version=policy_version,
            authority_digest=authority_digest,
        )

    async def _read(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        attachment: EmailAttachmentArtifact,
        max_bytes: int,
        policy_version: str,
        authority_digest: str,
    ) -> AsyncIterator[bytes]:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        reference = attachment.write_receipt.object_reference
        lifecycle_version = (
            "ea:"
            + base64.urlsafe_b64encode(bytes.fromhex(authority_digest))
            .decode("ascii")
            .rstrip("=")
            .lower()
        )
        grant = self._grant_registry.issue(
            workspace_id=workspace_id,
            environment_id=environment_id,
            source_revision_id=attachment.source_revision_id,
            object_reference=reference,
            purpose="email.attachment.processing",
            authorization_decision_version=policy_version,
            lifecycle_version=lifecycle_version,
            expires_at=now + self._grant_lifetime,
        )
        lease = self._grant_registry.consume(
            grant,
            workspace_id=workspace_id,
            environment_id=environment_id,
            source_revision_id=attachment.source_revision_id,
            object_reference=reference,
            purpose="email.attachment.processing",
            authorization_decision_version=policy_version,
            lifecycle_version=lifecycle_version,
            now=now,
        )
        try:
            async for chunk in self._object_store.open_bounded_read(
                lease, max_bytes=max_bytes
            ):
                yield chunk
        except (ObjectNotFound, ObjectCorrupt, IntegrityMismatch):
            raise EmailAttachmentOriginalMissing() from None


def build_email_attachment_ingestion(
    *,
    attachments: EmailAttachmentRepository,
    parent_authority: CurrentProcessingDispositionAuthority,
    decision: EmailAttachmentProcessingDecision,
    object_store: OriginalObjectStore,
    grant_registry: AuthorizedReadGrantRegistry,
    ingestion: EmailAttachmentDocumentIngestion,
    receipts: EmailAttachmentProcessingRepository,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> EmailAttachmentIngestionCoordinator:
    """Wire the governed child route to the existing document-ingestion port."""

    return EmailAttachmentIngestionCoordinator(
        attachments=attachments,
        parent_authority=parent_authority,
        attachment_authority=ConfiguredEmailAttachmentProcessingAuthority(decision),
        originals=ObjectStoreEmailAttachmentOriginalReader(
            object_store=object_store,
            grant_registry=grant_registry,
            clock=clock,
        ),
        ingestion=ingestion,
        receipts=receipts,
        clock=clock,
    )


__all__ = [
    "ConfiguredEmailAttachmentProcessingAuthority",
    "ObjectStoreEmailAttachmentOriginalReader",
    "build_email_attachment_ingestion",
]
