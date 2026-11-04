"""Exact, fail-closed citation resolution for immutable email body artifacts."""

from __future__ import annotations

import hashlib
from typing import Literal, Protocol
from uuid import UUID

from spine.application.projection_locator import canonical_projection_locator_digest
from spine.domain.artifacts.email_html import EmailHtmlDisposition
from spine.domain.artifacts.email_message import (
    EmailMessageArtifact,
    EmailMessageCitationLocator,
    decode_email_message_locator,
)
from spine.domain.artifacts.email_mime import MimeParseDisposition, MimePart
from spine.domain.capabilities import Citation
from spine.domain.disclosure import CitationExcerpt
from spine.memory.contracts import StableLocator


class EmailMessageArtifactReader(Protocol):
    async def get(
        self, *, workspace_id: UUID, environment_id: UUID, artifact_id: UUID
    ) -> EmailMessageArtifact | None: ...


class EmailMessageCitationExcerptResolver:
    """Resolve a pinned email locator without consulting raw provider content."""

    def __init__(self, repository: EmailMessageArtifactReader) -> None:
        self._repository = repository

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
            if canonical_projection_locator_digest(
                locator_contract_version=evidence.locator_contract_version,
                locator=stable,
            ) != evidence.locator_digest:
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


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


__all__ = [
    "EmailMessageArtifactReader",
    "EmailMessageCitationExcerptResolver",
    "build_email_message_citation_locator",
]
