"""Governed routing of retained email attachments into document ingestion."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import ConfigDict, Field

from spine.application.document_ingestion import (
    DocumentIngestionResult,
    DocumentIngestionStatus,
    DocumentUploadCommand,
)
from spine.application.email_attachments import EmailAttachmentRepository
from spine.application.processing_disposition import (
    CurrentProcessingDispositionAuthority,
    OrdinaryProcessingEligibilityReason,
)
from spine.domain.artifacts.email_attachment import (
    EmailAttachmentArtifact,
    EmailAttachmentProcessingDisposition,
    EmailAttachmentProcessingReceipt,
    email_attachment_processing_receipt_digest,
)
from spine.domain.common import DefinitionModel

SUPPORTED_MEDIA_TYPES = frozenset(
    {
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "text/markdown",
        "text/plain",
        "text/x-markdown",
    }
)
DANGEROUS_MEDIA_TYPES = frozenset(
    {
        "application/java-archive",
        "application/vnd.microsoft.portable-executable",
        "application/vnd.ms-excel.sheet.macroenabled.12",
        "application/vnd.ms-word.document.macroenabled.12",
        "application/x-7z-compressed",
        "application/x-dosexec",
        "application/x-rar-compressed",
        "application/x-tar",
        "application/zip",
    }
)
PROCESSOR_PROFILE = "email-attachment-document-ingestion:v1"


class EmailAttachmentIngestionUnavailable(PermissionError):
    """Content-free failure when the requested attachment identity is unavailable."""

    def __init__(self) -> None:
        super().__init__("email attachment processing is unavailable")


class EmailAttachmentOriginalMissing(FileNotFoundError):
    """Expected original bytes no longer exist at the governed reference."""

    def __init__(self) -> None:
        super().__init__("email attachment original is missing")


class EmailAttachmentProcessingConflict(RuntimeError):
    """A stable attachment-processing identity was reused for other lineage."""

    def __init__(self) -> None:
        super().__init__("email attachment processing conflicts with immutable history")


class EmailAttachmentProcessingDecision(DefinitionModel):
    """Current policy answer for one exact attachment artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    allowed: bool
    policy_version: str = Field(
        min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9_.:-]{0,127}$"
    )
    max_attachment_bytes: int = Field(gt=0)
    allowed_media_types: tuple[str, ...] = Field(min_length=1)


class EmailAttachmentProcessingAuthority(Protocol):
    async def resolve(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        attachment: EmailAttachmentArtifact,
    ) -> EmailAttachmentProcessingDecision | None: ...


class EmailAttachmentOriginalReader(Protocol):
    def read(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        attachment: EmailAttachmentArtifact,
        max_bytes: int,
        policy_version: str,
        authority_digest: str,
    ) -> AsyncIterable[bytes]: ...


class EmailAttachmentDocumentIngestion(Protocol):
    async def ingest(
        self, *, request: DocumentUploadCommand, chunks: AsyncIterable[bytes]
    ) -> DocumentIngestionResult: ...


@dataclass(frozen=True, slots=True)
class EmailAttachmentProcessingPersistenceResult:
    receipt: EmailAttachmentProcessingReceipt
    replay: bool


@dataclass(frozen=True, slots=True)
class EmailAttachmentBatchProcessingResult:
    completed: tuple[EmailAttachmentProcessingPersistenceResult, ...]
    retryable_attachment_ids: tuple[UUID, ...]


class EmailAttachmentProcessingRepository(Protocol):
    async def resolve_by_child_processing_receipt_id(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        child_processing_receipt_id: UUID,
    ) -> EmailAttachmentProcessingReceipt | None: ...

    async def resolve_by_id(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        processing_receipt_id: UUID,
    ) -> EmailAttachmentProcessingReceipt | None: ...

    async def resolve(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        attachment_artifact_id: UUID,
        processor_profile: str,
        authority_digest: str,
    ) -> EmailAttachmentProcessingReceipt | None: ...

    async def persist(
        self, receipt: EmailAttachmentProcessingReceipt
    ) -> EmailAttachmentProcessingPersistenceResult: ...


class EmailAttachmentIngestionCoordinator:
    """Process each retained part independently without trusting provider names."""

    def __init__(
        self,
        *,
        attachments: EmailAttachmentRepository,
        parent_authority: CurrentProcessingDispositionAuthority,
        attachment_authority: EmailAttachmentProcessingAuthority,
        originals: EmailAttachmentOriginalReader,
        ingestion: EmailAttachmentDocumentIngestion,
        receipts: EmailAttachmentProcessingRepository,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._attachments = attachments
        self._parent_authority = parent_authority
        self._attachment_authority = attachment_authority
        self._originals = originals
        self._ingestion = ingestion
        self._receipts = receipts
        self._clock = clock

    async def process(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        attachment_artifact_id: UUID,
    ) -> EmailAttachmentProcessingPersistenceResult:
        attachment = await self._attachments.get(
            workspace_id=workspace_id,
            environment_id=environment_id,
            artifact_id=attachment_artifact_id,
        )
        if attachment is None:
            raise EmailAttachmentIngestionUnavailable()

        parent, decision = await self._resolve_authority(attachment)
        authority_digest = _authority_digest(parent, decision)
        prior = await self._receipts.resolve(
            workspace_id=workspace_id,
            environment_id=environment_id,
            attachment_artifact_id=attachment_artifact_id,
            processor_profile=PROCESSOR_PROFILE,
            authority_digest=authority_digest,
        )
        if prior is not None:
            return EmailAttachmentProcessingPersistenceResult(prior, replay=True)
        if parent is None or decision is None or not decision.allowed:
            return await self._persist_disposition(
                attachment,
                decision=decision,
                authority_digest=authority_digest,
                disposition=EmailAttachmentProcessingDisposition.POLICY_DENIED,
            )
        if attachment.byte_length > decision.max_attachment_bytes:
            return await self._persist_disposition(
                attachment,
                decision=decision,
                authority_digest=authority_digest,
                disposition=EmailAttachmentProcessingDisposition.OVERSIZED,
            )
        if attachment.media_type in DANGEROUS_MEDIA_TYPES:
            return await self._persist_disposition(
                attachment,
                decision=decision,
                authority_digest=authority_digest,
                disposition=EmailAttachmentProcessingDisposition.DANGEROUS,
            )
        if (
            attachment.media_type not in SUPPORTED_MEDIA_TYPES
            or attachment.media_type not in decision.allowed_media_types
        ):
            return await self._persist_disposition(
                attachment,
                decision=decision,
                authority_digest=authority_digest,
                disposition=EmailAttachmentProcessingDisposition.UNSUPPORTED,
            )

        try:
            content = await _read_bounded(
                self._originals.read(
                    workspace_id=workspace_id,
                    environment_id=environment_id,
                    attachment=attachment,
                    max_bytes=decision.max_attachment_bytes,
                    policy_version=decision.policy_version,
                    authority_digest=authority_digest,
                ),
                decision.max_attachment_bytes,
            )
        except EmailAttachmentOriginalMissing:
            return await self._persist_disposition(
                attachment,
                decision=decision,
                authority_digest=authority_digest,
                disposition=EmailAttachmentProcessingDisposition.MISSING,
            )
        except Exception:
            raise EmailAttachmentIngestionUnavailable() from None
        if (
            len(content) != attachment.byte_length
            or hashlib.sha256(content).hexdigest() != attachment.content_sha256
        ):
            return await self._persist_disposition(
                attachment,
                decision=decision,
                authority_digest=authority_digest,
                disposition=EmailAttachmentProcessingDisposition.MISSING,
            )

        current_parent, current_decision = await self._resolve_authority(attachment)
        if current_parent != parent or current_decision != decision:
            if (
                current_parent is not None
                and current_decision is not None
                and current_decision.allowed
            ):
                # An allowed authority snapshot changed while bytes were being
                # read. Retry against that snapshot instead of sealing a false
                # policy denial under its digest.
                raise EmailAttachmentIngestionUnavailable()
            current_authority_digest = _authority_digest(
                current_parent, current_decision
            )
            return await self._persist_disposition(
                attachment,
                decision=current_decision or decision,
                authority_digest=current_authority_digest,
                disposition=EmailAttachmentProcessingDisposition.POLICY_DENIED,
            )

        request = _document_request(attachment)

        async def chunks() -> AsyncIterable[bytes]:
            yield content

        try:
            result = await self._ingestion.ingest(request=request, chunks=chunks())
        except Exception:
            # The downstream command is deterministic. A retry resumes its one
            # logical effect even when this coordinator did not receive a result.
            raise EmailAttachmentIngestionUnavailable() from None
        return await self._persist_result(
            attachment, decision, authority_digest, result
        )

    async def process_many(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        attachment_artifact_ids: tuple[UUID, ...],
    ) -> EmailAttachmentBatchProcessingResult:
        if len(set(attachment_artifact_ids)) != len(attachment_artifact_ids):
            raise ValueError("attachment artifact ids must be unique")
        results = []
        retryable = []
        for artifact_id in attachment_artifact_ids:
            try:
                results.append(
                    await self.process(
                        workspace_id=workspace_id,
                        environment_id=environment_id,
                        attachment_artifact_id=artifact_id,
                    )
                )
            except EmailAttachmentIngestionUnavailable:
                retryable.append(artifact_id)
        return EmailAttachmentBatchProcessingResult(
            completed=tuple(results), retryable_attachment_ids=tuple(retryable)
        )

    async def _resolve_authority(self, attachment: EmailAttachmentArtifact):
        try:
            parent = await self._parent_authority.resolve(
                workspace_id=attachment.workspace_id,
                environment_id=attachment.environment_id,
                source_object_id=attachment.source_object_id,
                source_revision_id=attachment.source_revision_id,
            )
            decision = await self._attachment_authority.resolve(
                workspace_id=attachment.workspace_id,
                environment_id=attachment.environment_id,
                attachment=attachment,
            )
        except Exception:
            raise EmailAttachmentIngestionUnavailable() from None
        if not parent.eligible and parent.reason in {
            OrdinaryProcessingEligibilityReason.CURRENT_AUTHORITY_UNAVAILABLE,
            OrdinaryProcessingEligibilityReason.ACCEPTANCE_UNAVAILABLE,
            OrdinaryProcessingEligibilityReason.PROCESSING_DISPOSITION_UNAVAILABLE,
        }:
            raise EmailAttachmentIngestionUnavailable()
        if (
            not parent.eligible
            or parent.canonical_acceptance_id != attachment.canonical_acceptance_id
        ):
            return None, decision
        return parent, decision

    async def _persist_result(
        self,
        attachment: EmailAttachmentArtifact,
        decision: EmailAttachmentProcessingDecision,
        authority_digest: str,
        result: DocumentIngestionResult,
    ) -> EmailAttachmentProcessingPersistenceResult:
        if (
            result.source.workspace_id != attachment.workspace_id
            or result.source.environment_id != attachment.environment_id
            or result.revision.workspace_id != attachment.workspace_id
            or result.revision.environment_id != attachment.environment_id
            or result.revision.source_object_id != result.source.source_object_id
            or result.revision.original_sha256 != attachment.content_sha256
            or result.revision.byte_length != attachment.byte_length
            or result.revision.media_type != attachment.media_type
        ):
            raise EmailAttachmentIngestionUnavailable()
        status_map = {
            DocumentIngestionStatus.SUCCEEDED: EmailAttachmentProcessingDisposition.PROCESSED,
            DocumentIngestionStatus.PARTIAL: EmailAttachmentProcessingDisposition.PARTIAL,
            DocumentIngestionStatus.UNSUPPORTED: EmailAttachmentProcessingDisposition.UNSUPPORTED,
            DocumentIngestionStatus.QUARANTINED: EmailAttachmentProcessingDisposition.DANGEROUS,
            DocumentIngestionStatus.REJECTED: EmailAttachmentProcessingDisposition.POLICY_DENIED,
            DocumentIngestionStatus.FAILED: EmailAttachmentProcessingDisposition.FAILED,
        }
        disposition = status_map[result.status]
        parsed = result.artifact
        processing = result.processing_receipt
        if parsed is not None and (
            parsed.workspace_id != attachment.workspace_id
            or parsed.environment_id != attachment.environment_id
            or parsed.source_object_id != result.source.source_object_id
            or parsed.source_revision_id != result.revision.revision_id
        ):
            raise EmailAttachmentIngestionUnavailable()
        if processing is not None and (
            processing.workspace_id != attachment.workspace_id
            or processing.environment_id != attachment.environment_id
            or processing.source_object_id != result.source.source_object_id
            or processing.source_revision_id != result.revision.revision_id
        ):
            raise EmailAttachmentIngestionUnavailable()
        if (
            parsed is not None
            and processing is not None
            and (
                processing.output_reference != parsed.artifact_id
                or processing.output_digest != parsed.artifact_digest
            )
        ):
            raise EmailAttachmentIngestionUnavailable()
        if disposition in {
            EmailAttachmentProcessingDisposition.PROCESSED,
            EmailAttachmentProcessingDisposition.PARTIAL,
        }:
            if parsed is None or processing is None:
                raise EmailAttachmentIngestionUnavailable()
        return await self._persist_disposition(
            attachment,
            decision=decision,
            authority_digest=authority_digest,
            disposition=disposition,
            child_source_object_id=result.source.source_object_id,
            child_source_revision_id=result.revision.revision_id,
            child_processing_receipt_id=(
                processing.processing_receipt_id if processing is not None else None
            ),
            parsed_artifact_id=parsed.artifact_id if parsed is not None else None,
            parsed_artifact_digest=(
                parsed.artifact_digest if parsed is not None else None
            ),
        )

    async def _persist_disposition(
        self,
        attachment: EmailAttachmentArtifact,
        *,
        decision: EmailAttachmentProcessingDecision | None,
        authority_digest: str,
        disposition: EmailAttachmentProcessingDisposition,
        child_source_object_id: UUID | None = None,
        child_source_revision_id: UUID | None = None,
        child_processing_receipt_id: UUID | None = None,
        parsed_artifact_id: UUID | None = None,
        parsed_artifact_digest: str | None = None,
    ) -> EmailAttachmentProcessingPersistenceResult:
        policy_version = (
            decision.policy_version if decision else "policy-unavailable:v1"
        )
        receipt_id = uuid5(
            NAMESPACE_URL,
            "spine:email-attachment-processing:v1:"
            f"{attachment.workspace_id}:{attachment.environment_id}:"
            f"{attachment.artifact_id}:{PROCESSOR_PROFILE}:{authority_digest}",
        )
        provisional = EmailAttachmentProcessingReceipt.model_construct(
            processing_receipt_id=receipt_id,
            workspace_id=attachment.workspace_id,
            environment_id=attachment.environment_id,
            parent_source_object_id=attachment.source_object_id,
            parent_source_revision_id=attachment.source_revision_id,
            parent_source_revision_digest=attachment.source_revision_digest,
            parent_canonical_acceptance_id=attachment.canonical_acceptance_id,
            attachment_artifact_id=attachment.artifact_id,
            attachment_artifact_digest=attachment.artifact_sha256,
            mime_part_path=attachment.mime_part_path,
            attachment_content_sha256=attachment.content_sha256,
            attachment_byte_length=attachment.byte_length,
            media_type=attachment.media_type,
            policy_version=policy_version,
            authority_digest=authority_digest,
            processor_profile=PROCESSOR_PROFILE,
            disposition=disposition,
            original_disposition=(
                "retained"
                if disposition
                in {
                    EmailAttachmentProcessingDisposition.PROCESSED,
                    EmailAttachmentProcessingDisposition.PARTIAL,
                }
                else "retained_fenced"
            ),
            child_source_object_id=child_source_object_id,
            child_source_revision_id=child_source_revision_id,
            child_processing_receipt_id=child_processing_receipt_id,
            parsed_artifact_id=parsed_artifact_id,
            parsed_artifact_digest=parsed_artifact_digest,
            completed_at=self._clock(),
            receipt_sha256="0" * 64,
        )
        receipt = EmailAttachmentProcessingReceipt.model_validate(
            provisional.model_copy(
                update={
                    "receipt_sha256": email_attachment_processing_receipt_digest(
                        provisional
                    )
                }
            ).model_dump()
        )
        return await self._receipts.persist(receipt)


def _document_request(attachment: EmailAttachmentArtifact) -> DocumentUploadCommand:
    key_payload = {
        "attachment_artifact_id": str(attachment.artifact_id),
        "attachment_artifact_digest": attachment.artifact_sha256,
        "processor_profile": PROCESSOR_PROFILE,
    }
    key = hashlib.sha256(
        json.dumps(key_payload, sort_keys=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()
    return DocumentUploadCommand(
        workspace_id=attachment.workspace_id,
        environment_id=attachment.environment_id,
        command_identity="email.attachment.process",
        idempotency_key=f"email-attachment-process:{key}",
        upload_identity=f"email-attachment-{attachment.artifact_id.hex}",
        purpose="email.attachment.processing",
        declared_media_type=attachment.media_type,
        # Provider filenames are evidence only. Format routing uses the verified
        # MIME media type and never turns an untrusted name into a path.
        declared_filename=None,
        expected_sha256=attachment.content_sha256,
        expected_byte_length=attachment.byte_length,
        source_created_at=attachment.write_receipt.finalized_at,
    )


def _authority_digest(parent: object | None, decision: object | None) -> str:
    payload = {
        "parent": (
            parent.model_dump(mode="json")
            if isinstance(parent, DefinitionModel)
            else None
        ),
        "attachment": (
            decision.model_dump(mode="json")
            if isinstance(decision, DefinitionModel)
            else None
        ),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()


async def _read_bounded(stream: AsyncIterable[bytes], max_bytes: int) -> bytes:
    content = bytearray()
    async for chunk in stream:
        if not isinstance(chunk, bytes):
            raise TypeError("attachment reader yielded a non-byte chunk")
        content.extend(chunk)
        if len(content) > max_bytes:
            raise ValueError("attachment read exceeded its bound")
    return bytes(content)


__all__ = [
    "EmailAttachmentIngestionCoordinator",
    "EmailAttachmentIngestionUnavailable",
    "EmailAttachmentOriginalMissing",
    "EmailAttachmentBatchProcessingResult",
    "EmailAttachmentProcessingConflict",
    "EmailAttachmentProcessingDecision",
    "EmailAttachmentProcessingAuthority",
    "EmailAttachmentOriginalReader",
    "EmailAttachmentDocumentIngestion",
    "EmailAttachmentProcessingRepository",
    "EmailAttachmentProcessingPersistenceResult",
    "PROCESSOR_PROFILE",
]
