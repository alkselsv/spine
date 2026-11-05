"""Exact, fail-closed citation resolution for immutable email body artifacts."""

from __future__ import annotations

import hashlib
import json
from typing import Literal, Protocol
from uuid import UUID

from spine.application.projection_locator import canonical_projection_locator_digest
from spine.domain.artifacts.email_html import EmailHtmlDisposition
from spine.domain.artifacts.email_attachment import (
    EmailAttachmentArtifact,
    EmailAttachmentCitationLocator,
    EmailAttachmentProcessingDisposition,
    EmailAttachmentProcessingReceipt,
    decode_email_attachment_locator,
    encode_email_attachment_locator,
)
from spine.domain.artifacts.email_message import (
    EmailMessageArtifact,
    EmailMessageCitationLocator,
    decode_email_message_locator,
)
from spine.domain.artifacts.email_mime import MimeParseDisposition, MimePart
from spine.domain.artifacts import ParsedDocument, ParsedSegment
from spine.domain.capabilities import Citation
from spine.domain.disclosure import CitationExcerpt
from spine.memory.contracts import StableLocator


class EmailMessageArtifactReader(Protocol):
    async def get(
        self, *, workspace_id: UUID, environment_id: UUID, artifact_id: UUID
    ) -> EmailMessageArtifact | None: ...


class EmailAttachmentArtifactReader(Protocol):
    async def get(
        self, *, workspace_id: UUID, environment_id: UUID, artifact_id: UUID
    ) -> EmailAttachmentArtifact | None: ...


class EmailAttachmentProcessingReceiptReader(Protocol):
    async def resolve_by_id(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        processing_receipt_id: UUID,
    ) -> EmailAttachmentProcessingReceipt | None: ...


class EmailAttachmentProjectionProcessingReader(Protocol):
    async def resolve_by_child_processing_receipt_id(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        child_processing_receipt_id: UUID,
    ) -> EmailAttachmentProcessingReceipt | None: ...


class EmailMessageRevisionArtifactReader(Protocol):
    async def list_for_revision(
        self, *, workspace_id: UUID, environment_id: UUID, source_revision_id: UUID
    ) -> tuple[EmailMessageArtifact, ...]: ...


class EmailAttachmentParsedArtifactReader(Protocol):
    async def get(
        self, *, workspace_id: UUID, environment_id: UUID, artifact_id: UUID
    ) -> ParsedDocument | None: ...

    async def resolve_stable_segment(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        source_object_id: UUID,
        source_revision_id: UUID,
        source_revision_digest: str,
        original_sha256: str,
        locator_contract_version: str,
        locator_kind: str,
        locator_value: str,
    ) -> tuple[ParsedDocument, ParsedSegment] | None: ...


class EmailAttachmentProjectionLocatorProjector:
    """Upgrade parsed child locators to immutable parent/attachment lineage."""

    def __init__(
        self,
        *,
        messages: EmailMessageRevisionArtifactReader,
        attachments: EmailAttachmentArtifactReader,
        processing: EmailAttachmentProjectionProcessingReader,
    ) -> None:
        self._messages = messages
        self._attachments = attachments
        self._processing = processing

    async def project(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        child_processing_receipt_id: UUID,
        attachment_required: bool,
        parsed_artifact: ParsedDocument,
        child_locator: StableLocator,
    ) -> tuple[str, StableLocator] | None:
        receipt = await self._processing.resolve_by_child_processing_receipt_id(
            workspace_id=workspace_id,
            environment_id=environment_id,
            child_processing_receipt_id=child_processing_receipt_id,
        )
        if receipt is None:
            if attachment_required:
                raise ValueError("email attachment projection lineage is unavailable")
            return None
        attachment = await self._attachments.get(
            workspace_id=workspace_id,
            environment_id=environment_id,
            artifact_id=receipt.attachment_artifact_id,
        )
        parents = await self._messages.list_for_revision(
            workspace_id=workspace_id,
            environment_id=environment_id,
            source_revision_id=receipt.parent_source_revision_id,
        )
        matching_parents = tuple(
            parent
            for parent in parents
            if parent.source_object_id == receipt.parent_source_object_id
            and parent.source_revision_digest == receipt.parent_source_revision_digest
            and parent.canonical_acceptance_id
            == receipt.parent_canonical_acceptance_id
        )
        if attachment is None or len(matching_parents) != 1:
            raise ValueError("email attachment projection lineage is unavailable")
        try:
            locator = build_email_attachment_citation_locator(
                parent=matching_parents[0],
                attachment=attachment,
                receipt=receipt,
                parsed_artifact=parsed_artifact,
                child_locator=child_locator,
            )
        except ValueError as error:
            raise ValueError(
                "email attachment projection lineage is unavailable"
            ) from error
        return locator.schema_version, StableLocator(
            kind="custom",
            value=encode_email_attachment_locator(locator),
        )


class EmailMessageCitationExcerptResolver:
    """Resolve a pinned email locator without consulting raw provider content."""

    def __init__(self, repository: EmailMessageArtifactReader) -> None:
        self._repository = repository

    def dependency_source_identities(
        self, citation: Citation
    ) -> tuple[tuple[UUID, UUID], ...] | None:
        evidence = citation.evidence_reference
        if evidence.locator_contract_version != "email-message-locator:v1":
            return None
        return ((evidence.source_object_id, evidence.source_revision_id),)

    async def resolve(
        self, citation: Citation, *, historical: bool
    ) -> CitationExcerpt | None:
        del historical
        try:
            evidence = citation.evidence_reference
            if (
                evidence.locator_contract_version != "email-message-locator:v1"
                or evidence.locator.kind != "message"
                or evidence.locator_digest is None
                or evidence.original_sha256 is None
                or evidence.revision_digest is None
            ):
                return None
            stable = StableLocator(
                kind="message",
                value=evidence.locator.value,
            )
            if (
                canonical_projection_locator_digest(
                    locator_contract_version=evidence.locator_contract_version,
                    locator=stable,
                )
                != evidence.locator_digest
            ):
                return None
            locator = decode_email_message_locator(evidence.locator.value)
            if locator is None or not _locator_matches_evidence(locator, citation):
                return None
            artifact = await self._repository.get(
                workspace_id=citation.workspace_id,
                environment_id=citation.environment_id,
                artifact_id=locator.artifact_id,
            )
            if artifact is None or not _artifact_matches(
                artifact, citation=citation, locator=locator
            ):
                return None
            text = _resolve_representation(artifact, locator)
            if text is None or _digest(text) != locator.representation_digest:
                return None
            if locator.end_codepoint > len(text):
                return None
            excerpt = text[locator.start_codepoint : locator.end_codepoint]
            if not excerpt or _digest(excerpt) != locator.excerpt_digest:
                return None
            return CitationExcerpt(
                source_object_id=artifact.source_object_id,
                source_revision_id=artifact.source_revision_id,
                original_sha256=artifact.raw_sha256,
                revision_digest=artifact.source_revision_digest,
                revision_number=evidence.revision_number,
                locator_schema=locator.schema_version,
                locator_reference=evidence.locator.value,
                locator_digest=evidence.locator_digest,
                excerpt=excerpt,
            )
        except Exception:
            return None


class EmailAttachmentCitationExcerptResolver:
    """Resolve a child range only after its complete immutable lineage matches."""

    def __init__(
        self,
        *,
        messages: EmailMessageArtifactReader,
        attachments: EmailAttachmentArtifactReader,
        processing: EmailAttachmentProcessingReceiptReader,
        parsed_artifacts: EmailAttachmentParsedArtifactReader,
    ) -> None:
        self._messages = messages
        self._attachments = attachments
        self._processing = processing
        self._parsed_artifacts = parsed_artifacts

    def dependency_source_identities(
        self, citation: Citation
    ) -> tuple[tuple[UUID, UUID], ...] | None:
        evidence = citation.evidence_reference
        if evidence.locator_contract_version != "email-attachment-locator:v1":
            return None
        locator = decode_email_attachment_locator(evidence.locator.value)
        if locator is None:
            return ()
        return (
            (
                locator.parent_source_object_id,
                locator.parent_source_revision_id,
            ),
            (evidence.source_object_id, evidence.source_revision_id),
        )

    async def resolve(
        self, citation: Citation, *, historical: bool
    ) -> CitationExcerpt | None:
        del historical
        try:
            evidence = citation.evidence_reference
            if (
                evidence.locator_contract_version != "email-attachment-locator:v1"
                or evidence.locator.kind != "custom"
                or evidence.locator_digest is None
                or evidence.original_sha256 is None
                or evidence.revision_digest is None
            ):
                return None
            stable = StableLocator(kind="custom", value=evidence.locator.value)
            if (
                canonical_projection_locator_digest(
                    locator_contract_version=evidence.locator_contract_version,
                    locator=stable,
                )
                != evidence.locator_digest
            ):
                return None
            locator = decode_email_attachment_locator(evidence.locator.value)
            if locator is None:
                return None

            parent = await self._messages.get(
                workspace_id=citation.workspace_id,
                environment_id=citation.environment_id,
                artifact_id=locator.parent_message_artifact_id,
            )
            if parent is None or not _attachment_parent_matches(
                citation=citation,
                locator=locator,
                parent=parent,
            ):
                return None
            attachment = await self._attachments.get(
                workspace_id=citation.workspace_id,
                environment_id=citation.environment_id,
                artifact_id=locator.attachment_artifact_id,
            )
            if attachment is None or not _attachment_matches_parent(
                citation=citation,
                locator=locator,
                parent=parent,
                attachment=attachment,
            ):
                return None
            receipt = await self._processing.resolve_by_id(
                workspace_id=citation.workspace_id,
                environment_id=citation.environment_id,
                processing_receipt_id=locator.processing_receipt_id,
            )
            if receipt is None or not _receipt_matches_attachment(
                citation=citation,
                locator=locator,
                parent=parent,
                attachment=attachment,
                receipt=receipt,
            ):
                return None
            pinned_document = await self._parsed_artifacts.get(
                workspace_id=citation.workspace_id,
                environment_id=citation.environment_id,
                artifact_id=locator.parsed_artifact_id,
            )
            if (
                pinned_document is None
                or pinned_document.artifact_digest != receipt.parsed_artifact_digest
                or pinned_document.source_object_id != receipt.child_source_object_id
                or pinned_document.source_revision_id
                != receipt.child_source_revision_id
                or pinned_document.original_sha256 != attachment.content_sha256
            ):
                return None
            resolved = await self._parsed_artifacts.resolve_stable_segment(
                workspace_id=citation.workspace_id,
                environment_id=citation.environment_id,
                source_object_id=evidence.source_object_id,
                source_revision_id=evidence.source_revision_id,
                source_revision_digest=evidence.revision_digest,
                original_sha256=evidence.original_sha256,
                locator_contract_version=pinned_document.locator_contract_version,
                locator_kind=locator.child_locator_kind,
                locator_value=locator.child_locator_value,
            )
            if resolved is None:
                return None
            document, segment = resolved
            excerpt = segment.text
            if (
                document != pinned_document
                or document.artifact_id != locator.parsed_artifact_id
                or document.artifact_digest != receipt.parsed_artifact_digest
                or document.source_object_id != receipt.child_source_object_id
                or document.source_revision_id != receipt.child_source_revision_id
                or document.original_sha256 != attachment.content_sha256
                or not excerpt
            ):
                return None
            return CitationExcerpt(
                source_object_id=document.source_object_id,
                source_revision_id=document.source_revision_id,
                original_sha256=document.original_sha256,
                revision_digest=document.source_revision_digest,
                revision_number=evidence.revision_number,
                locator_schema="email-attachment-locator:v1",
                locator_reference=evidence.locator.value,
                locator_digest=evidence.locator_digest,
                excerpt=excerpt,
            )
        except Exception:
            return None


def build_email_attachment_citation_locator(
    *,
    parent: EmailMessageArtifact,
    attachment: EmailAttachmentArtifact,
    receipt: EmailAttachmentProcessingReceipt,
    parsed_artifact: ParsedDocument,
    child_locator: StableLocator,
) -> EmailAttachmentCitationLocator:
    """Build a locator only when the complete parent/child chain agrees."""

    if (
        receipt.disposition
        not in {
            EmailAttachmentProcessingDisposition.PROCESSED,
            EmailAttachmentProcessingDisposition.PARTIAL,
        }
        or receipt.parsed_artifact_id is None
        or receipt.parsed_artifact_digest is None
        or parent.workspace_id != attachment.workspace_id
        or parent.environment_id != attachment.environment_id
        or receipt.workspace_id != parent.workspace_id
        or receipt.environment_id != parent.environment_id
        or parsed_artifact.workspace_id != parent.workspace_id
        or parsed_artifact.environment_id != parent.environment_id
        or parent.source_object_id != attachment.source_object_id
        or parent.source_revision_id != attachment.source_revision_id
        or parent.source_revision_digest != attachment.source_revision_digest
        or parent.canonical_acceptance_id != attachment.canonical_acceptance_id
        or receipt.parent_source_object_id != parent.source_object_id
        or receipt.parent_source_revision_id != parent.source_revision_id
        or receipt.parent_source_revision_digest != parent.source_revision_digest
        or receipt.parent_canonical_acceptance_id != parent.canonical_acceptance_id
        or receipt.attachment_artifact_id != attachment.artifact_id
        or receipt.attachment_artifact_digest != attachment.artifact_sha256
        or receipt.mime_part_path != attachment.mime_part_path
        or receipt.attachment_content_sha256 != attachment.content_sha256
        or receipt.parsed_artifact_id != parsed_artifact.artifact_id
        or receipt.parsed_artifact_digest != parsed_artifact.artifact_digest
        or receipt.child_source_object_id != parsed_artifact.source_object_id
        or receipt.child_source_revision_id != parsed_artifact.source_revision_id
        or parsed_artifact.original_sha256 != attachment.content_sha256
    ):
        raise ValueError("email attachment citation lineage is unavailable")
    parent_digest = email_attachment_parent_locator_digest(
        parent, part_path=attachment.mime_part_path
    )
    return EmailAttachmentCitationLocator(
        parent_source_object_id=parent.source_object_id,
        parent_source_revision_id=parent.source_revision_id,
        parent_message_artifact_id=parent.artifact_id,
        parent_mime_part_path=attachment.mime_part_path,
        parent_locator_digest=parent_digest,
        attachment_artifact_id=attachment.artifact_id,
        attachment_artifact_digest=attachment.artifact_sha256,
        processing_receipt_id=receipt.processing_receipt_id,
        processing_receipt_digest=receipt.receipt_sha256,
        parsed_artifact_id=parsed_artifact.artifact_id,
        child_locator_kind=child_locator.kind,
        child_locator_value=child_locator.value,
    )


def email_attachment_parent_locator_digest(
    artifact: EmailMessageArtifact, *, part_path: str
) -> str:
    """Commit the exact I05a parent artifact/profile/MIME-part identity."""

    if artifact.mime.disposition is not MimeParseDisposition.COMPLETE:
        raise ValueError("email attachment parent is unavailable")
    root = artifact.mime.root
    part = None if root is None else _find_exact_part(root, part_path)
    if part is None or part.content_disposition not in {"inline", "attachment"}:
        raise ValueError("email attachment parent part is unavailable")
    payload = {
        "schema": "email-attachment-parent-locator:v1",
        "source_object_id": str(artifact.source_object_id),
        "source_revision_id": str(artifact.source_revision_id),
        "source_revision_digest": artifact.source_revision_digest,
        "artifact_id": str(artifact.artifact_id),
        "artifact_digest": artifact.artifact_sha256,
        "parser_profile": artifact.parser_profile,
        "sanitizer_profile": artifact.sanitizer_profile,
        "mime_part_path": part_path,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def build_email_message_citation_locator(
    artifact: EmailMessageArtifact,
    *,
    part_path: str,
    representation: Literal["plain_text", "sanitized_html_text"],
    start_codepoint: int,
    end_codepoint: int,
) -> EmailMessageCitationLocator:
    """Build a locator only for an exact, reproducible range in the artifact."""

    locator = EmailMessageCitationLocator(
        source_revision_id=artifact.source_revision_id,
        source_revision_digest=artifact.source_revision_digest,
        artifact_id=artifact.artifact_id,
        artifact_digest=artifact.artifact_sha256,
        parser_profile=artifact.parser_profile,
        sanitizer_profile=artifact.sanitizer_profile,
        part_path=part_path,
        representation=representation,
        start_codepoint=start_codepoint,
        end_codepoint=end_codepoint,
        representation_digest="0" * 64,
        excerpt_digest="0" * 64,
    )
    if artifact.mime.disposition is not MimeParseDisposition.COMPLETE:
        raise ValueError("email message body representation is unavailable")
    text = _resolve_representation(artifact, locator)
    if text is None or end_codepoint > len(text):
        raise ValueError("email message body range is unavailable")
    excerpt = text[start_codepoint:end_codepoint]
    if not excerpt:
        raise ValueError("email message body range is empty")
    return locator.model_copy(
        update={
            "representation_digest": _digest(text),
            "excerpt_digest": _digest(excerpt),
        }
    )


def _locator_matches_evidence(
    locator: EmailMessageCitationLocator, citation: Citation
) -> bool:
    evidence = citation.evidence_reference
    return (
        locator.source_revision_id == evidence.source_revision_id
        and locator.source_revision_digest == evidence.revision_digest
    )


def _artifact_matches(
    artifact: EmailMessageArtifact,
    *,
    citation: Citation,
    locator: EmailMessageCitationLocator,
) -> bool:
    evidence = citation.evidence_reference
    return (
        artifact.workspace_id == citation.workspace_id
        and artifact.environment_id == citation.environment_id
        and artifact.source_object_id == evidence.source_object_id
        and artifact.source_revision_id == locator.source_revision_id
        and artifact.source_revision_digest == locator.source_revision_digest
        and artifact.raw_sha256 == evidence.original_sha256
        and artifact.artifact_id == locator.artifact_id
        and artifact.artifact_sha256 == locator.artifact_digest
        and artifact.parser_profile == locator.parser_profile
        and artifact.sanitizer_profile == locator.sanitizer_profile
        and artifact.mime.disposition is MimeParseDisposition.COMPLETE
    )


def _resolve_representation(
    artifact: EmailMessageArtifact,
    locator: EmailMessageCitationLocator,
) -> str | None:
    root = artifact.mime.root
    if root is None:
        return None
    part = _find_body_part(root, locator.part_path)
    if part is None or part.content_disposition == "attachment":
        return None
    if locator.representation == "plain_text":
        if part.media_type != "text/plain":
            return None
        return part.text
    if part.media_type != "text/html":
        return None
    derivative = next(
        (
            item
            for item in artifact.sanitized_html
            if item.source_part_path == locator.part_path
        ),
        None,
    )
    if (
        derivative is None
        or derivative.disposition is not EmailHtmlDisposition.COMPLETE
        or derivative.source_sha256 != artifact.raw_sha256
        or derivative.sanitizer_profile != artifact.sanitizer_profile
    ):
        return None
    return derivative.plain_text


def _find_body_part(root: MimePart, part_path: str) -> MimePart | None:
    stack = [root]
    while stack:
        part = stack.pop()
        if part.part_path == part_path:
            return part
        if (
            part.content_disposition != "attachment"
            and part.media_type != "message/rfc822"
        ):
            stack.extend(part.children)
    return None


def _find_exact_part(root: MimePart, part_path: str) -> MimePart | None:
    stack = [root]
    while stack:
        part = stack.pop()
        if part.part_path == part_path:
            return part
        if part.media_type != "message/rfc822":
            stack.extend(part.children)
    return None


def _attachment_parent_matches(
    *,
    citation: Citation,
    locator: EmailAttachmentCitationLocator,
    parent: EmailMessageArtifact,
) -> bool:
    try:
        return (
            parent.workspace_id == citation.workspace_id
            and parent.environment_id == citation.environment_id
            and parent.source_object_id == locator.parent_source_object_id
            and parent.source_revision_id == locator.parent_source_revision_id
            and parent.artifact_id == locator.parent_message_artifact_id
            and email_attachment_parent_locator_digest(
                parent, part_path=locator.parent_mime_part_path
            )
            == locator.parent_locator_digest
        )
    except Exception:
        return False


def _attachment_matches_parent(
    *,
    citation: Citation,
    locator: EmailAttachmentCitationLocator,
    parent: EmailMessageArtifact,
    attachment: EmailAttachmentArtifact,
) -> bool:
    evidence = citation.evidence_reference
    return (
        attachment.workspace_id == citation.workspace_id
            and attachment.environment_id == citation.environment_id
            and attachment.source_object_id == parent.source_object_id
            and attachment.source_revision_id == parent.source_revision_id
            and attachment.source_revision_digest == parent.source_revision_digest
            and attachment.canonical_acceptance_id == parent.canonical_acceptance_id
            and attachment.mime_part_path == locator.parent_mime_part_path
            and attachment.artifact_id == locator.attachment_artifact_id
            and attachment.artifact_sha256 == locator.attachment_artifact_digest
            and attachment.content_sha256 == evidence.original_sha256
    )


def _receipt_matches_attachment(
    *,
    citation: Citation,
    locator: EmailAttachmentCitationLocator,
    parent: EmailMessageArtifact,
    attachment: EmailAttachmentArtifact,
    receipt: EmailAttachmentProcessingReceipt,
) -> bool:
    evidence = citation.evidence_reference
    return (
        receipt.workspace_id == citation.workspace_id
            and receipt.environment_id == citation.environment_id
            and receipt.processing_receipt_id == locator.processing_receipt_id
            and receipt.receipt_sha256 == locator.processing_receipt_digest
            and receipt.disposition
            in {
                EmailAttachmentProcessingDisposition.PROCESSED,
                EmailAttachmentProcessingDisposition.PARTIAL,
            }
            and receipt.parent_source_object_id == parent.source_object_id
            and receipt.parent_source_revision_id == parent.source_revision_id
            and receipt.parent_source_revision_digest == parent.source_revision_digest
            and receipt.parent_canonical_acceptance_id
            == parent.canonical_acceptance_id
            and receipt.attachment_artifact_id == attachment.artifact_id
            and receipt.attachment_artifact_digest == attachment.artifact_sha256
            and receipt.mime_part_path == attachment.mime_part_path
            and receipt.attachment_content_sha256 == attachment.content_sha256
            and receipt.child_source_object_id == evidence.source_object_id
            and receipt.child_source_revision_id == evidence.source_revision_id
            and receipt.parsed_artifact_id == locator.parsed_artifact_id
    )


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


__all__ = [
    "EmailAttachmentArtifactReader",
    "EmailAttachmentCitationExcerptResolver",
    "EmailAttachmentParsedArtifactReader",
    "EmailAttachmentProjectionLocatorProjector",
    "EmailAttachmentProjectionProcessingReader",
    "EmailAttachmentProcessingReceiptReader",
    "EmailMessageArtifactReader",
    "EmailMessageCitationExcerptResolver",
    "EmailMessageRevisionArtifactReader",
    "build_email_attachment_citation_locator",
    "build_email_message_citation_locator",
    "email_attachment_parent_locator_digest",
]
