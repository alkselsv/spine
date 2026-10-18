"""Local, gold-blind preparation of an approved real R1 corpus.

Only the approved mapping manifest may select logical source identities.  The
module feeds bytes to the canonical document-ingestion seam; projection work
continues through its durable outbox request and is proven separately by
``r1-corpus-readiness:v1``.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterable
from enum import Enum
from pathlib import Path
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from spine.auth import AuthorizedRequestContext, RequestedAuthorizationScope
from spine.application.document_ingestion import (
    DocumentIngestionOrchestrator,
    DocumentIngestionStatus,
    DocumentUploadCommand,
)
from spine.application.corpus_readiness import (
    CorpusReadinessRequest,
    CorpusRevisionExpectation,
)
from spine.evals.real_qa_r1 import (
    REAL_R1_DATASET_DIGEST,
    REAL_R1_DATASET_VERSION,
    RealRuntimeMappingManifest,
    RealRuntimeRevisionBinding,
    runtime_mapping_manifest_digest,
)


CORPUS_PREPARATION_REPORT_SCHEMA = "r1-corpus-preparation:v1"
_SHA256 = r"^[0-9a-f]{64}$"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CorpusPreparationStatus(str, Enum):
    COMPLETED = "completed"
    INCOMPLETE = "incomplete"
    UNAVAILABLE = "unavailable"


class ApprovedCorpusArtifact(_Model):
    """One locally held original selected by opaque approved identities."""

    dataset_source_id: str = Field(min_length=1, max_length=256)
    dataset_revision_id: str = Field(min_length=1, max_length=256)
    original_sha256: str = Field(pattern=_SHA256)
    byte_length: int = Field(ge=0)
    media_type: str = Field(min_length=1, max_length=255)
    filename: str = Field(min_length=1, max_length=255)
    content: bytes = Field(repr=False)

    @model_validator(mode="after")
    def validate_length(self) -> "ApprovedCorpusArtifact":
        if len(self.content) != self.byte_length:
            raise ValueError("approved corpus artifact length does not match")
        return self


class ApprovedCorpusArtifactLocation(_Model):
    dataset_source_id: str = Field(min_length=1, max_length=256)
    dataset_revision_id: str = Field(min_length=1, max_length=256)
    relative_path: str = Field(min_length=1, max_length=1024)
    expected_sha256: str = Field(pattern=_SHA256)
    media_type: str = Field(min_length=1, max_length=255)


class ApprovedCorpusArtifactPort(Protocol):
    async def resolve(
        self,
        *,
        dataset_source_id: str,
        dataset_revision_id: str,
    ) -> ApprovedCorpusArtifact | None: ...


class CanonicalDocumentIngestionResult(Protocol):
    status: DocumentIngestionStatus
    source: "CanonicalSourceIdentity"
    revision: "CanonicalRevisionIdentity"
    replay: bool


class CanonicalSourceIdentity(Protocol):
    source_object_id: UUID


class CanonicalRevisionIdentity(Protocol):
    revision_id: UUID


class CanonicalDocumentIngestionPort(Protocol):
    async def ingest(
        self, *, request: DocumentUploadCommand, chunks: AsyncIterable[bytes]
    ) -> CanonicalDocumentIngestionResult: ...


class FilesystemApprovedCorpusArtifactStore:
    """Read an explicitly registered, root-confined approved local corpus."""

    def __init__(
        self, *, root: Path, artifacts: tuple[ApprovedCorpusArtifactLocation, ...]
    ) -> None:
        self._root = root.expanduser().resolve()
        self._artifacts = {
            (item.dataset_source_id, item.dataset_revision_id): item for item in artifacts
        }
        if len(self._artifacts) != len(artifacts):
            raise ValueError("approved corpus artifacts must be unique")

    async def resolve(
        self, *, dataset_source_id: str, dataset_revision_id: str
    ) -> ApprovedCorpusArtifact | None:
        location = self._artifacts.get((dataset_source_id, dataset_revision_id))
        if location is None:
            return None
        path = (self._root / location.relative_path).resolve()
        if self._root not in path.parents or not path.is_file():
            return None
        content = await asyncio.to_thread(path.read_bytes)
        observed_sha256 = hashlib.sha256(content).hexdigest()
        if observed_sha256 != location.expected_sha256:
            raise ValueError("approved corpus artifact digest does not match registration")
        return ApprovedCorpusArtifact(
            dataset_source_id=location.dataset_source_id,
            dataset_revision_id=location.dataset_revision_id,
            original_sha256=observed_sha256,
            byte_length=len(content),
            media_type=location.media_type,
            filename=path.name,
            content=content,
        )


class CorpusRevisionPreparation(_Model):
    dataset_source_id: str
    dataset_revision_id: str
    upload_identity: str
    ingestion_intent_id: str
    status: Literal[
        "prepared",
        "missing",
        "digest_mismatch",
        "rejected",
        "quarantined",
        "unsupported",
        "failed",
        "unavailable",
    ]
    source_object_id: UUID | None = None
    source_revision_id: UUID | None = None
    projection_request_status: Literal["requested", "not_requested"]
    replay: bool = False


class CorpusPreparationReport(_Model):
    schema_version: Literal["r1-corpus-preparation:v1"] = CORPUS_PREPARATION_REPORT_SCHEMA
    manifest_sha256: str = Field(pattern=_SHA256)
    workspace_id: UUID
    environment_id: UUID
    status: CorpusPreparationStatus
    structural_validation_status: Literal["passed"] = "passed"
    readiness_status: Literal["not_checked"] = "not_checked"
    production_observation_status: Literal["not_run"] = "not_run"
    evaluation_status: Literal["not_run"] = "not_run"
    release_qualification_status: Literal["not_run"] = "not_run"
    revisions: tuple[CorpusRevisionPreparation, ...]


class ApprovedCorpusPreparationService:
    """Submit every approved revision through canonical ingestion, idempotently."""

    def __init__(
        self,
        *,
        artifacts: ApprovedCorpusArtifactPort,
        ingestion: DocumentIngestionOrchestrator | CanonicalDocumentIngestionPort,
    ) -> None:
        self._artifacts = artifacts
        self._ingestion = ingestion

    async def prepare(
        self,
        *,
        manifest: RealRuntimeMappingManifest,
        trusted_context: AuthorizedRequestContext[object],
    ) -> CorpusPreparationReport:
        if type(manifest) is not RealRuntimeMappingManifest:
            raise TypeError("approved corpus preparation requires a validated mapping manifest")
        if not isinstance(trusted_context, AuthorizedRequestContext):
            raise ValueError("approved corpus preparation requires trusted authorization")
        scope = trusted_context.scope
        if (
            type(scope) is not RequestedAuthorizationScope
            or trusted_context.acting_subject_id.int == 0
            or trusted_context.authorization_generation < 1
        ):
            raise ValueError("approved corpus preparation requires trusted authorization")
        workspace_id = scope.workspace_id
        environment_id = scope.environment_id
        if (
            manifest.dataset_version != REAL_R1_DATASET_VERSION
            or manifest.dataset_sha256
            != REAL_R1_DATASET_DIGEST
            or len(manifest.sources) != 30
        ):
            raise ValueError("mapping manifest is not the approved 30-source corpus")
        manifest_sha256 = runtime_mapping_manifest_digest(manifest)
        prepared: list[CorpusRevisionPreparation] = []
        for source in manifest.sources:
            for revision in source.revisions:
                prepared.append(
                    await self._prepare_revision(
                        manifest_sha256=manifest_sha256,
                        workspace_id=workspace_id,
                        environment_id=environment_id,
                        dataset_source_id=source.dataset_source_id,
                        dataset_revision_id=revision.dataset_revision_id,
                        upload_identity=source.upload_identity,
                        ingestion_intent_id=revision.ingestion_intent_id,
                        expected_sha256=revision.expected_original_sha256,
                    )
                )
        statuses = {item.status for item in prepared}
        status = (
            CorpusPreparationStatus.COMPLETED
            if statuses == {"prepared"}
            else CorpusPreparationStatus.UNAVAILABLE
            if "unavailable" in statuses
            else CorpusPreparationStatus.INCOMPLETE
        )
        return CorpusPreparationReport(
            manifest_sha256=manifest_sha256,
            workspace_id=workspace_id,
            environment_id=environment_id,
            status=status,
            revisions=tuple(prepared),
        )

    async def _prepare_revision(
        self,
        *,
        manifest_sha256: str,
        workspace_id: UUID,
        environment_id: UUID,
        dataset_source_id: str,
        dataset_revision_id: str,
        upload_identity: str,
        ingestion_intent_id: str,
        expected_sha256: str,
    ) -> CorpusRevisionPreparation:
        common = {
            "dataset_source_id": dataset_source_id,
            "dataset_revision_id": dataset_revision_id,
            "upload_identity": upload_identity,
            "ingestion_intent_id": ingestion_intent_id,
        }
        try:
            artifact = await self._artifacts.resolve(
                dataset_source_id=dataset_source_id,
                dataset_revision_id=dataset_revision_id,
            )
        except Exception:
            return CorpusRevisionPreparation(
                **common, status="unavailable", projection_request_status="not_requested"
            )
        if artifact is None:
            return CorpusRevisionPreparation(
                **common, status="missing", projection_request_status="not_requested"
            )
        actual_sha256 = hashlib.sha256(artifact.content).hexdigest()
        if (
            artifact.dataset_source_id != dataset_source_id
            or artifact.dataset_revision_id != dataset_revision_id
            or artifact.original_sha256 != expected_sha256
            or actual_sha256 != expected_sha256
            or len(artifact.content) != artifact.byte_length
        ):
            return CorpusRevisionPreparation(
                **common,
                status="digest_mismatch",
                projection_request_status="not_requested",
            )
        idempotency_key = "r1-corpus:" + hashlib.sha256(
            "|".join(
                (manifest_sha256, dataset_source_id, dataset_revision_id, ingestion_intent_id)
            ).encode("utf-8")
        ).hexdigest()
        request = DocumentUploadCommand(
            workspace_id=workspace_id,
            environment_id=environment_id,
            command_identity="r1.corpus.prepare",
            idempotency_key=idempotency_key,
            upload_identity=upload_identity,
            purpose="r1.corpus.prepare",
            declared_media_type=artifact.media_type,
            declared_filename=artifact.filename,
            expected_sha256=expected_sha256,
            expected_byte_length=artifact.byte_length,
        )
        try:
            result = await self._ingestion.ingest(
                request=request,
                chunks=_one_chunk(artifact.content),
            )
        except Exception:
            return CorpusRevisionPreparation(
                **common, status="unavailable", projection_request_status="not_requested"
            )
        status = result.status
        mapped = {
            DocumentIngestionStatus.SUCCEEDED: "prepared",
            DocumentIngestionStatus.PARTIAL: "prepared",
            DocumentIngestionStatus.REJECTED: "rejected",
            DocumentIngestionStatus.QUARANTINED: "quarantined",
            DocumentIngestionStatus.UNSUPPORTED: "unsupported",
            DocumentIngestionStatus.FAILED: "failed",
        }.get(status, "unavailable")
        source_id = result.source.source_object_id
        revision_id = result.revision.revision_id
        if mapped == "prepared" and (
            not isinstance(source_id, UUID)
            or source_id.int == 0
            or not isinstance(revision_id, UUID)
            or revision_id.int == 0
        ):
            mapped = "unavailable"
        return CorpusRevisionPreparation(
            **common,
            status=mapped,
            source_object_id=source_id if isinstance(source_id, UUID) else None,
            source_revision_id=revision_id if isinstance(revision_id, UUID) else None,
            projection_request_status="requested" if mapped == "prepared" else "not_requested",
            replay=result.replay,
        )


def corpus_readiness_request_from_bindings(
    bindings: tuple[RealRuntimeRevisionBinding, ...],
    *,
    projection_kind: str,
) -> CorpusReadinessRequest:
    """Adapt #448 verified bindings to the canonical #449 readiness seam."""

    if not bindings or any(type(item) is not RealRuntimeRevisionBinding for item in bindings):
        raise ValueError("corpus readiness requires verified runtime bindings")
    first = bindings[0]
    if len({item.dataset_source_id for item in bindings}) != 30:
        raise ValueError("corpus readiness requires all 30 approved source bindings")
    if any(
        item.manifest_sha256 != first.manifest_sha256
        or item.owner_approval_ref != first.owner_approval_ref
        or item.workspace_id != first.workspace_id
        or item.environment_id != first.environment_id
        for item in bindings
    ):
        raise ValueError("corpus readiness bindings must share manifest and scope")
    return CorpusReadinessRequest(
        workspace_id=first.workspace_id,
        environment_id=first.environment_id,
        projection_kind=projection_kind,
        revisions=tuple(
            CorpusRevisionExpectation(
                dataset_source_id=item.dataset_source_id,
                dataset_revision_id=item.dataset_revision_id,
                workspace_id=item.workspace_id,
                environment_id=item.environment_id,
                source_object_id=item.source_object_id,
                source_revision_id=item.source_revision_id,
                canonical_acceptance_id=item.canonical_acceptance_id,
            )
            for item in bindings
        ),
    )


async def _one_chunk(content: bytes):
    yield content


__all__ = [
    "CORPUS_PREPARATION_REPORT_SCHEMA",
    "ApprovedCorpusArtifact",
    "ApprovedCorpusArtifactLocation",
    "ApprovedCorpusArtifactPort",
    "ApprovedCorpusPreparationService",
    "CanonicalDocumentIngestionPort",
    "CorpusPreparationReport",
    "CorpusPreparationStatus",
    "CorpusRevisionPreparation",
    "FilesystemApprovedCorpusArtifactStore",
    "corpus_readiness_request_from_bindings",
]
