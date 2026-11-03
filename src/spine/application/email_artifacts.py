"""Governed orchestration and immutable persistence contracts for email bodies."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Literal, Protocol
from uuid import UUID, uuid5, NAMESPACE_URL

from pydantic import ConfigDict, Field, ValidationError

from spine.application.processing_disposition import CurrentProcessingDispositionAuthority
from spine.application.persistence.outbox import (
    OutboxEventRegistry,
    OutboxIntent,
    OpaqueObjectReference,
)
from spine.domain.common import DefinitionModel
from spine.application.storage.authorized_read import AuthorizedOriginalReadGateway
from spine.domain.artifacts.email_message import EmailMessageArtifact, email_message_artifact_digest
from spine.domain.artifacts.email_mime import MimeParseDisposition
from spine.domain.artifacts.email_html import EmailHtmlDisposition
from spine.domain.artifacts.email_html import SanitizedEmailHtml
from spine.domain.artifacts.email_mime import MimeArtifact, MimePart
from spine.ingestion.email_html import EmailHtmlSanitizer
from spine.ingestion.email_mime import MimeParser


class EmailArtifactUnavailable(PermissionError):
    """Content-free failure for inaccessible or ineligible email revisions."""

    def __init__(self) -> None:
        super().__init__("email content is unavailable")


class EmailArtifactConflict(RuntimeError):
    """An immutable email artifact identity was reused for different evidence."""

    def __init__(self) -> None:
        super().__init__("email artifact conflicts with immutable history")


EMAIL_ARTIFACT_PROJECTION_REQUESTED = "email.projection.requested"


class EmailArtifactProjectionRequest(DefinitionModel):
    """Content-free signal to project one immutable processed email artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["email-artifact-projection-request:v1"] = (
        "email-artifact-projection-request:v1"
    )
    source_object_id: UUID
    source_revision_id: UUID
    canonical_acceptance_id: UUID
    artifact_id: UUID
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    parser_profile: str = Field(pattern=r"^mime-parser:v1:[0-9a-f]{64}$")
    sanitizer_profile: str = Field(
        pattern=r"^email-html-sanitizer:v1:[0-9a-f]{64}$"
    )
    selected_representation: Literal["plain_text", "sanitized_html_text", "none"]


@dataclass(frozen=True, slots=True)
class EmailArtifactCommand:
    artifact: EmailMessageArtifact
    command_identity: str
    command_digest: str
    projection_intent: OutboxIntent

    @property
    def projection_intent_id(self) -> UUID:
        intent = self.projection_intent
        if (
            intent.event_id is None
            or intent.workspace_id != self.artifact.workspace_id
            or intent.environment_id != self.artifact.environment_id
            or intent.aggregate is None
            or intent.aggregate.object_id != self.artifact.artifact_id
            or intent.event_type != EMAIL_ARTIFACT_PROJECTION_REQUESTED
            or intent.producer_deduplication_id
            != f"email-artifact:{self.artifact.artifact_id}"
            or type(intent.payload) is not EmailArtifactProjectionRequest
            or intent.payload.artifact_id != self.artifact.artifact_id
            or intent.payload.artifact_sha256 != self.artifact.artifact_sha256
            or intent.payload.source_object_id != self.artifact.source_object_id
            or intent.payload.source_revision_id != self.artifact.source_revision_id
            or intent.payload.canonical_acceptance_id
            != self.artifact.canonical_acceptance_id
        ):
            raise EmailArtifactConflict()
        return intent.event_id


@dataclass(frozen=True, slots=True)
class EmailArtifactPersistenceResult:
    artifact: EmailMessageArtifact
    projection_intent_id: UUID
    replay: bool


class EmailArtifactRepository(Protocol):
    async def persist(self, command: EmailArtifactCommand) -> EmailArtifactPersistenceResult: ...

    async def get(
        self, *, workspace_id: UUID, environment_id: UUID, artifact_id: UUID
    ) -> EmailMessageArtifact | None: ...


@dataclass(frozen=True, slots=True)
class AcceptedEmailParent:
    workspace_id: UUID
    environment_id: UUID
    source_object_id: UUID
    source_revision_id: UUID
    source_revision_digest: str
    original_sha256: str
    raw_byte_length: int


class AcceptedEmailParentReader(Protocol):
    async def resolve_accepted_email_parent(
        self, *, workspace_id: UUID, environment_id: UUID, source_object_id: UUID,
        source_revision_id: UUID,
    ) -> AcceptedEmailParent | None: ...


class EmailArtifactProcessor:
    """Read, process and persist only the currently accepted authorized revision."""

    def __init__(
        self,
        *,
        gateway: AuthorizedOriginalReadGateway,
        eligibility: CurrentProcessingDispositionAuthority,
        parents: AcceptedEmailParentReader,
        repository: EmailArtifactRepository,
        parser: MimeParser,
        sanitizer: EmailHtmlSanitizer,
        outbox_events: OutboxEventRegistry,
    ) -> None:
        self._gateway = gateway
        self._eligibility = eligibility
        self._parents = parents
        self._repository = repository
        self._parser = parser
        self._sanitizer = sanitizer
        self._outbox_events = outbox_events
        self._outbox_events.register(
            event_type=EMAIL_ARTIFACT_PROJECTION_REQUESTED,
            schema_version=1,
            payload_type=EmailArtifactProjectionRequest,
        )

    async def process(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        source_object_id: UUID,
        source_revision_id: UUID,
        trace_id: UUID,
        max_bytes: int,
    ) -> EmailArtifactPersistenceResult:
        # No source metadata is returned on a denied path. Check processing
        # disposition before opening the authorized read gateway.
        try:
            eligibility = await self._eligibility.resolve(
                workspace_id=workspace_id,
                environment_id=environment_id,
                source_object_id=source_object_id,
                source_revision_id=source_revision_id,
            )
        except Exception:
            raise EmailArtifactUnavailable() from None
        if not eligibility.eligible or eligibility.canonical_acceptance_id is None:
            raise EmailArtifactUnavailable()
        try:
            parent = await self._parents.resolve_accepted_email_parent(
                workspace_id=workspace_id,
                environment_id=environment_id,
                source_object_id=source_object_id,
                source_revision_id=source_revision_id,
            )
        except Exception:
            raise EmailArtifactUnavailable() from None
        if (
            parent is None
            or parent.workspace_id != workspace_id
            or parent.environment_id != environment_id
            or parent.source_object_id != source_object_id
            or parent.source_revision_id != source_revision_id
        ):
            raise EmailArtifactUnavailable()
        try:
            raw = b"".join([
                chunk async for chunk in self._gateway.read_original(
                    workspace_id=workspace_id,
                    environment_id=environment_id,
                    source_revision_id=source_revision_id,
                    purpose="process_content",
                    max_bytes=max_bytes,
                )
            ])
        except Exception:
            raise EmailArtifactUnavailable() from None
        if (
            len(raw) != parent.raw_byte_length
            or hashlib.sha256(raw).hexdigest() != parent.original_sha256
        ):
            raise EmailArtifactUnavailable()

        # Revalidate after I/O to stop a revocation/lifecycle transition during
        # the read from producing a derivative.
        try:
            current = await self._eligibility.resolve(
                workspace_id=workspace_id,
                environment_id=environment_id,
                source_object_id=source_object_id,
                source_revision_id=source_revision_id,
            )
        except Exception:
            raise EmailArtifactUnavailable() from None
        if current != eligibility:
            raise EmailArtifactUnavailable()

        parsed = self._parser.parse(raw)
        safe_html = self._sanitizer.sanitize_all(parsed)
        selected_text, representation = _select_body(parsed, safe_html)
        raw_digest = hashlib.sha256(raw).hexdigest()
        artifact_id = uuid5(
            NAMESPACE_URL,
            "spine:email-artifact:v1:{}:{}:{}:{}".format(
                workspace_id,
                environment_id,
                source_revision_id,
                self._parser.profile.reference + ":" + self._sanitizer.reference,
            ),
        )
        provisional = EmailMessageArtifact.model_construct(
            artifact_id=artifact_id,
            workspace_id=workspace_id,
            environment_id=environment_id,
            source_object_id=source_object_id,
            source_revision_id=source_revision_id,
            canonical_acceptance_id=eligibility.canonical_acceptance_id,
            source_revision_digest=parent.source_revision_digest,
            raw_sha256=raw_digest,
            raw_byte_length=len(raw),
            parser_profile=self._parser.profile.reference,
            sanitizer_profile=self._sanitizer.reference,
            mime=parsed,
            sanitized_html=safe_html,
            selected_representation=representation,
            selected_text=selected_text,
            artifact_sha256="0" * 64,
        )
        artifact = provisional.model_copy(update={
            "artifact_sha256": email_message_artifact_digest(provisional)
        })
        # Re-validate the complete immutable contract before persisting.
        try:
            artifact = EmailMessageArtifact.model_validate(artifact.model_dump())
        except ValidationError:
            raise EmailArtifactUnavailable() from None
        command_identity = f"email-artifact:{source_revision_id}:{self._parser.profile.reference}:{self._sanitizer.reference}"
        command_digest = hashlib.sha256(
            f"{command_identity}:{artifact.artifact_sha256}".encode()
        ).hexdigest()
        intent_id = uuid5(NAMESPACE_URL, f"spine:email-artifact-projection:{artifact.artifact_id}")
        projection_intent = self._outbox_events.build_intent(
            event_id=intent_id,
            workspace_id=workspace_id,
            environment_id=environment_id,
            event_type=EMAIL_ARTIFACT_PROJECTION_REQUESTED,
            schema_version=1,
            payload=EmailArtifactProjectionRequest(
                source_object_id=source_object_id,
                source_revision_id=source_revision_id,
                canonical_acceptance_id=eligibility.canonical_acceptance_id,
                artifact_id=artifact.artifact_id,
                artifact_sha256=artifact.artifact_sha256,
                parser_profile=artifact.parser_profile,
                sanitizer_profile=artifact.sanitizer_profile,
                selected_representation=artifact.selected_representation,
            ),
            aggregate=OpaqueObjectReference(
                object_type="email_message_artifact",
                object_id=artifact.artifact_id,
                schema_version=1,
            ),
            producer_deduplication_id=f"email-artifact:{artifact.artifact_id}",
            trace_id=trace_id,
            causation_id=eligibility.canonical_acceptance_id,
        )
        try:
            return await self._repository.persist(
                EmailArtifactCommand(
                    artifact, command_identity, command_digest, projection_intent
                )
            )
        except EmailArtifactConflict:
            raise
        except Exception:
            raise EmailArtifactUnavailable() from None


def _select_body(
    parsed: MimeArtifact,
    sanitized: tuple[SanitizedEmailHtml, ...],
) -> tuple[str | None, str]:
    if parsed.disposition is not MimeParseDisposition.COMPLETE or parsed.root is None:
        return None, "none"
    plain_parts = [
        part for part in _walk(parsed.root)
        if part.media_type == "text/plain"
        and part.text is not None
        and part.content_disposition != "attachment"
    ]
    if plain_parts:
        return plain_parts[0].text, "plain_text"
    by_path = {item.source_part_path: item for item in sanitized}
    for part in _walk(parsed.root):
        if part.media_type != "text/html":
            continue
        derivative = by_path.get(part.part_path)
        if derivative is not None and derivative.disposition is EmailHtmlDisposition.COMPLETE:
            return derivative.plain_text, "sanitized_html_text"
    return None, "none"


def _walk(root: MimePart) -> tuple[MimePart, ...]:
    result: list[MimePart] = []
    stack = [root]
    while stack:
        part = stack.pop()
        result.append(part)
        stack.extend(reversed(part.children))
    return tuple(result)


__all__ = ["EmailArtifactProcessor", "EmailArtifactUnavailable", "EmailArtifactRepository"]
