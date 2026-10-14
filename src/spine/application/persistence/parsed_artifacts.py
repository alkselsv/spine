"""Application contracts for immutable parsed-document persistence."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Protocol
from uuid import UUID

from spine.domain.artifacts import Locator, ParsedDocument, ParsedSegment

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
