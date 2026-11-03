"""Governed extraction and immutable storage of exact email MIME parts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID

from pydantic import ConfigDict, Field, StrictInt

from spine.application.processing_disposition import CurrentProcessingDispositionAuthority
from spine.application.storage.authorized_read import AuthorizedOriginalReadGateway
from spine.domain.common import DefinitionModel
from spine.domain.artifacts.email_attachment import (
    EmailAttachmentArtifact,
    EmailAttachmentWriteReceipt,
    email_attachment_artifact_digest,
    email_attachment_id,
)
from spine.domain.artifacts.email_mime import MimeParseDisposition, MimePart
from spine.ingestion.email_mime import MimeAttachmentContent, MimeParser


class EmailAttachmentUnavailable(PermissionError):
    """Content-free failure for an attachment that cannot be safely stored."""

    def __init__(self) -> None:
        super().__init__("email attachment is unavailable")


class EmailAttachmentConflict(RuntimeError):
    """An immutable MIME-part identity was reused for different evidence."""

    def __init__(self) -> None:
        super().__init__("email attachment conflicts with immutable history")


class EmailAttachmentExtractionProfile(DefinitionModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_version: Literal["email-attachment-extractor-profile:v1"] = "email-attachment-extractor-profile:v1"
    max_attachment_bytes: StrictInt = Field(gt=0)
    max_attachment_parts: StrictInt = Field(gt=0)

    @property
    def reference(self) -> str:
        payload = json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode("ascii")
        return "email-attachment-extractor:v1:" + hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class AcceptedEmailAttachmentParent:
    workspace_id: UUID
    environment_id: UUID
    source_object_id: UUID
    source_revision_id: UUID
    source_revision_digest: str
    original_sha256: str
    raw_byte_length: int


class AcceptedEmailAttachmentParentReader(Protocol):
    async def resolve_accepted_email_parent(
        self, *, workspace_id: UUID, environment_id: UUID, source_object_id: UUID,
        source_revision_id: UUID,
    ) -> AcceptedEmailAttachmentParent | None: ...


class EmailAttachmentOriginalWriter(Protocol):
    async def store(
        self, *, workspace_id: UUID, environment_id: UUID, source_revision_id: UUID,
        part_path: str, extraction_profile: str, media_type: str,
        filename: str | None, content: bytes, sha256: str,
    ) -> EmailAttachmentWriteReceipt: ...


@dataclass(frozen=True, slots=True)
class EmailAttachmentPersistenceResult:
    artifact: EmailAttachmentArtifact
    replay: bool


class EmailAttachmentRepository(Protocol):
    async def persist_many(
        self, artifacts: tuple[EmailAttachmentArtifact, ...]
    ) -> tuple[EmailAttachmentPersistenceResult, ...]: ...

    async def get(
        self, *, workspace_id: UUID, environment_id: UUID, artifact_id: UUID
    ) -> EmailAttachmentArtifact | None: ...

    async def list_for_revision(
        self, *, workspace_id: UUID, environment_id: UUID, source_revision_id: UUID
    ) -> tuple[EmailAttachmentArtifact, ...]: ...


class EmailAttachmentProcessor:
    """Extract accepted MIME parts, verify physical receipts, and persist lineage."""

    def __init__(
        self, *, gateway: AuthorizedOriginalReadGateway,
        eligibility: CurrentProcessingDispositionAuthority,
        parents: AcceptedEmailAttachmentParentReader, repository: EmailAttachmentRepository,
        parser: MimeParser, profile: EmailAttachmentExtractionProfile,
        originals: EmailAttachmentOriginalWriter,
    ) -> None:
        self._gateway = gateway
        self._eligibility = eligibility
        self._parents = parents
        self._repository = repository
        self._parser = parser
        self._profile = profile
        self._originals = originals

    async def process(
        self, *, workspace_id: UUID, environment_id: UUID, source_object_id: UUID,
        source_revision_id: UUID, max_bytes: int,
        expected_part_paths: tuple[str, ...] | None = None,
    ) -> tuple[EmailAttachmentPersistenceResult, ...]:
        try:
            eligibility = await self._eligibility.resolve(
                workspace_id=workspace_id, environment_id=environment_id,
                source_object_id=source_object_id, source_revision_id=source_revision_id,
            )
            parent = await self._parents.resolve_accepted_email_parent(
                workspace_id=workspace_id, environment_id=environment_id,
                source_object_id=source_object_id, source_revision_id=source_revision_id,
            )
        except Exception:
            raise EmailAttachmentUnavailable() from None
        if (
            not eligibility.eligible or eligibility.canonical_acceptance_id is None
            or parent is None
            or (parent.workspace_id, parent.environment_id, parent.source_object_id, parent.source_revision_id)
            != (workspace_id, environment_id, source_object_id, source_revision_id)
        ):
            raise EmailAttachmentUnavailable()
        try:
            raw = b"".join([
                chunk async for chunk in self._gateway.read_original(
                    workspace_id=workspace_id, environment_id=environment_id,
                    source_revision_id=source_revision_id, purpose="process_content",
                    max_bytes=max_bytes,
                )
            ])
        except Exception:
            raise EmailAttachmentUnavailable() from None
        if (
            len(raw) > max_bytes or len(raw) != parent.raw_byte_length
            or hashlib.sha256(raw).hexdigest() != parent.original_sha256
        ):
            raise EmailAttachmentUnavailable()
        try:
            current = await self._eligibility.resolve(
                workspace_id=workspace_id, environment_id=environment_id,
                source_object_id=source_object_id, source_revision_id=source_revision_id,
            )
        except Exception:
            raise EmailAttachmentUnavailable() from None
        if current != eligibility:
            raise EmailAttachmentUnavailable()

        parsed = self._parser.parse_with_attachments(raw)
        if parsed.artifact.disposition is not MimeParseDisposition.COMPLETE:
            raise EmailAttachmentUnavailable()
        if len(parsed.attachments) > self._profile.max_attachment_parts:
            raise EmailAttachmentUnavailable()
        part_by_path = {part.part_path: part for part in _walk(parsed.artifact.root)} if parsed.artifact.root else {}
        if len(part_by_path) < len(parsed.attachments):
            raise EmailAttachmentUnavailable()
        paths = [part.part_path for part in parsed.attachments]
        if len(paths) != len(set(paths)):
            raise EmailAttachmentUnavailable()
        if expected_part_paths is not None:
            if (
                len(expected_part_paths) != len(set(expected_part_paths))
                or any(path not in paths for path in expected_part_paths)
            ):
                raise EmailAttachmentUnavailable()
            selected_paths = set(expected_part_paths)
            candidates = tuple(
                part for part in parsed.attachments if part.part_path in selected_paths
            )
        else:
            candidates = parsed.attachments

        verified_parts: list[MimeAttachmentContent] = []
        for part in candidates:
            node = part_by_path.get(part.part_path)
            if (
                node is None or node.decoded_byte_length != len(part.content)
                or node.payload_sha256 != part.sha256
                or hashlib.sha256(part.content).hexdigest() != part.sha256
                or len(part.content) > self._profile.max_attachment_bytes
                or part.disposition not in {"inline", "attachment"}
            ):
                raise EmailAttachmentUnavailable()
            verified_parts.append(part)

        artifacts: list[EmailAttachmentArtifact] = []
        for part in verified_parts:
            try:
                receipt = await self._originals.store(
                    workspace_id=workspace_id, environment_id=environment_id,
                    source_revision_id=source_revision_id, part_path=part.part_path,
                    extraction_profile=self._profile.reference, media_type=part.media_type,
                    filename=part.filename, content=part.content, sha256=part.sha256,
                )
                artifact = _artifact(
                    workspace_id=workspace_id, environment_id=environment_id,
                    source_object_id=source_object_id, source_revision_id=source_revision_id,
                    canonical_acceptance_id=eligibility.canonical_acceptance_id,
                    source_revision_digest=parent.source_revision_digest, part=part,
                    extraction_profile=self._profile.reference, receipt=receipt,
                )
                artifacts.append(artifact)
            except EmailAttachmentConflict:
                raise
            except Exception:
                raise EmailAttachmentUnavailable() from None
        try:
            return await self._repository.persist_many(tuple(artifacts))
        except EmailAttachmentConflict:
            raise
        except Exception:
            raise EmailAttachmentUnavailable() from None


def _artifact(
    *, workspace_id: UUID, environment_id: UUID, source_object_id: UUID,
    source_revision_id: UUID, canonical_acceptance_id: UUID,
    source_revision_digest: str, part: MimeAttachmentContent,
    extraction_profile: str, receipt: EmailAttachmentWriteReceipt,
) -> EmailAttachmentArtifact:
    if (
        receipt.observed_sha256 != part.sha256
        or receipt.observed_byte_length != len(part.content)
        or receipt.object_reference.digest_hex != part.sha256
        or receipt.object_reference.byte_length != len(part.content)
    ):
        raise EmailAttachmentUnavailable()
    identifier = email_attachment_id(
        workspace_id=workspace_id, environment_id=environment_id,
        source_revision_id=source_revision_id, mime_part_path=part.part_path,
        extraction_profile=extraction_profile,
    )
    provisional = EmailAttachmentArtifact.model_construct(
        artifact_id=identifier, workspace_id=workspace_id, environment_id=environment_id,
        source_object_id=source_object_id, source_revision_id=source_revision_id,
        canonical_acceptance_id=canonical_acceptance_id,
        source_revision_digest=source_revision_digest, mime_part_path=part.part_path,
        content_sha256=part.sha256, byte_length=len(part.content),
        media_type=part.media_type, extraction_profile=extraction_profile,
        disposition=part.disposition, filename=part.filename, content_id=part.content_id,
        write_receipt=receipt,
        artifact_sha256="0" * 64,
    )
    return EmailAttachmentArtifact.model_validate(provisional.model_copy(update={
        "artifact_sha256": email_attachment_artifact_digest(provisional)
    }).model_dump())


def _walk(root: MimePart) -> tuple[MimePart, ...]:
    result: list[MimePart] = []
    stack = [root]
    while stack:
        part = stack.pop()
        result.append(part)
        stack.extend(reversed(part.children))
    return tuple(result)


__all__ = [
    "EmailAttachmentExtractionProfile", "EmailAttachmentProcessor",
    "EmailAttachmentUnavailable", "EmailAttachmentOriginalWriter",
]
