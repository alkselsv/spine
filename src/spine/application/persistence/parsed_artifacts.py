"""Application contracts for immutable parsed-document persistence."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Protocol
from uuid import UUID

from spine.domain.artifacts import Locator, ParseCoverage, ParsedDocument, ParsedSegment

_COMMAND_ID = re.compile(r"^[a-z][a-z0-9_.:-]{0,63}$")


@dataclass(frozen=True, slots=True)
class ParsedArtifactCommand:
    document: ParsedDocument
    command_identity: str
    command_digest: str

    def __post_init__(self) -> None:
        if _COMMAND_ID.fullmatch(self.command_identity) is None:
            raise ValueError("command_identity must be a bounded identifier")
        if not re.fullmatch(r"[0-9a-f]{64}", self.command_digest):
            raise ValueError("command_digest must be a SHA-256 digest")

    @classmethod
    def create(cls, document: ParsedDocument, *, command_identity: str) -> "ParsedArtifactCommand":
        payload = {
            "schema": "parsed-artifact-command:v1",
            "command_identity": command_identity,
            "artifact_digest": document.artifact_digest,
            "source_revision_id": str(document.source_revision_id),
            "source_revision_digest": document.source_revision_digest,
            "original_sha256": document.original_sha256,
            "parser_version": document.parser_version,
            "parser_config_reference": document.parser_config_reference,
            "locator_contract_version": document.locator_contract_version,
        }
        digest = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
        ).hexdigest()
        return cls(document=document, command_identity=command_identity, command_digest=digest)


@dataclass(frozen=True, slots=True)
class ParsedArtifactPersistenceResult:
    document: ParsedDocument
    replay: bool


@dataclass(frozen=True, slots=True)
class ParsedArtifactMetadata:
    """Bounded parsed-artifact identity returned without materializing segments."""

    artifact_id: UUID
    artifact_digest: str
    workspace_id: UUID
    environment_id: UUID
    source_object_id: UUID
    source_revision_id: UUID
    source_revision_digest: str
    original_sha256: str
    parser_name: str
    parser_version: str
    parser_config_reference: str
    locator_contract_version: str
    media_type: str
    coverage: ParseCoverage

    @classmethod
    def from_document(cls, document: ParsedDocument) -> "ParsedArtifactMetadata":
        return cls(
            artifact_id=document.artifact_id,
            artifact_digest=document.artifact_digest,
            workspace_id=document.workspace_id,
            environment_id=document.environment_id,
            source_object_id=document.source_object_id,
            source_revision_id=document.source_revision_id,
            source_revision_digest=document.source_revision_digest,
            original_sha256=document.original_sha256,
            parser_name=document.parser_name,
            parser_version=document.parser_version,
            parser_config_reference=document.parser_config_reference,
            locator_contract_version=document.locator_contract_version,
            media_type=document.media_type,
            coverage=document.coverage,
        )


@dataclass(frozen=True, slots=True)
class ParsedArtifactSegmentPage:
    metadata: ParsedArtifactMetadata
    segments: tuple[ParsedSegment, ...]
    has_more: bool


class ParsedArtifactRepository(Protocol):
    async def persist(self, command: ParsedArtifactCommand) -> ParsedArtifactPersistenceResult: ...

    async def get(self, *, workspace_id: UUID, environment_id: UUID, artifact_id: UUID) -> ParsedDocument | None: ...

    async def resolve_segment(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        source_revision_id: UUID,
        source_revision_digest: str,
        original_sha256: str,
        parser_version: str,
        locator_contract_version: str,
        locator: Locator,
    ) -> ParsedSegment | None: ...

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

    async def list_segments(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        source_object_id: UUID,
        source_revision_id: UUID,
        source_revision_digest: str,
        original_sha256: str,
        artifact_id: UUID,
        artifact_digest: str,
        parser_name: str,
        parser_version: str,
        parser_config_reference: str,
        locator_contract_version: str,
        selector: Locator | None,
        after_segment_id: UUID | None,
        limit: int,
    ) -> ParsedArtifactSegmentPage | None: ...
