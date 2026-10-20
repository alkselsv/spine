"""Provider-neutral ontology constrained extraction.

This module accepts model-produced candidates and returns only deterministic,
evidence-linked projection facts. It intentionally has no Cognee dependency;
the graph writer protocol is the seam for a later physical adapter.
"""

from __future__ import annotations

from enum import Enum
from hashlib import sha256
import json
from typing import TYPE_CHECKING, Literal, Protocol, TypeVar
from uuid import UUID

from pydantic import Field, model_validator

from spine.domain.common import DefinitionModel, VersionIdentifier
from spine.memory.contracts import (
    CanonicalBoundary,
    ProjectionCandidate,
    ProjectionConfigVersion,
    ProjectionNamespace,
    ProjectionOperation,
    ProjectionSourceRevision,
)
from spine.memory.cognee.models.baseline import (
    ChunkFixture,
    EntityTypeMapping,
    EvidenceReference,
    ExtractionConclusion,
    ExtractionConfiguration,
    OntologyDefinition,
    RelationTypeMapping,
    SourceRevisionFixture,
)

if TYPE_CHECKING:
    from spine.memory.cognee.graph import GraphProjectionResult


_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
_IDENTIFIER_PATTERN = r"^[a-z][a-z0-9_.:-]{0,63}$"


def _require_non_zero(value: UUID, field_name: str) -> None:
    if value.int == 0:
        raise ValueError(f"{field_name} must be a non-zero UUID")


def _digest(model: DefinitionModel, *, exclude: set[str]) -> str:
    payload = model.model_dump(mode="json", exclude=exclude)
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


class ExtractionStatus(str, Enum):
    ACCEPTED = "accepted"
    DEGRADED = "degraded"
    REJECTED = "rejected"


class EntityCandidate(DefinitionModel):
    """One provider-neutral entity candidate before ontology mapping."""

    candidate_id: str = Field(min_length=1, max_length=64, pattern=_IDENTIFIER_PATTERN)
    source_label: str = Field(min_length=1, max_length=64, pattern=_IDENTIFIER_PATTERN)
    canonical_name: str = Field(min_length=1, max_length=128)
    source_revision_id: UUID
    chunk_ids: tuple[str, ...] = ()
    evidence: tuple[EvidenceReference, ...] = ()


class RelationCandidate(DefinitionModel):
    """One provider-neutral relation candidate before ontology mapping."""

    candidate_id: str = Field(min_length=1, max_length=64, pattern=_IDENTIFIER_PATTERN)
    source_label: str = Field(min_length=1, max_length=64, pattern=_IDENTIFIER_PATTERN)
    source_revision_id: UUID
    source_entity_candidate_id: str = Field(
        min_length=1, max_length=64, pattern=_IDENTIFIER_PATTERN
    )
    target_entity_candidate_id: str = Field(
        min_length=1, max_length=64, pattern=_IDENTIFIER_PATTERN
    )
    evidence: tuple[EvidenceReference, ...] = ()


class ExtractionBatch(DefinitionModel):
    """Immutable extraction input tied to one tenant and environment."""

    schema_version: Literal["ontology-extraction-batch:v1"] = (
        "ontology-extraction-batch:v1"
    )
    workspace_id: UUID
    environment_id: UUID
    ontology: OntologyDefinition
    extraction_config: ExtractionConfiguration
    source_revisions: tuple[SourceRevisionFixture, ...] = Field(min_length=1)
    chunks: tuple[ChunkFixture, ...] = ()
    entities: tuple[EntityCandidate, ...] = ()
    relations: tuple[RelationCandidate, ...] = ()
    conclusions: tuple[ExtractionConclusion, ...] = ()

    @model_validator(mode="after")
    def validate_identity(self) -> "ExtractionBatch":
        _require_non_zero(self.workspace_id, "workspace_id")
        _require_non_zero(self.environment_id, "environment_id")
        return self


class AcceptedEntity(DefinitionModel):
    """A graph entity safe to hand to a physical projection adapter."""

    schema_version: Literal["accepted-entity:v1"] = "accepted-entity:v1"
    entity_id: str = Field(min_length=1, max_length=64, pattern=_IDENTIFIER_PATTERN)
    entity_type: str = Field(min_length=1, max_length=64, pattern=_IDENTIFIER_PATTERN)
    canonical_name: str = Field(min_length=1, max_length=128)
    workspace_id: UUID
    environment_id: UUID
    source_revision_id: UUID
    ontology_version: VersionIdentifier
    ontology_digest: str = Field(pattern=_DIGEST_PATTERN)
    extraction_config_version: str = Field(min_length=1, max_length=32)
    extraction_config_digest: str = Field(pattern=_DIGEST_PATTERN)
    chunk_ids: tuple[str, ...] = ()
    evidence: tuple[EvidenceReference, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_lineage(self) -> "AcceptedEntity":
        _require_non_zero(self.workspace_id, "workspace_id")
        _require_non_zero(self.environment_id, "environment_id")
        if any(
            reference.source_revision_id != self.source_revision_id
            for reference in self.evidence
        ):
            raise ValueError("accepted entity evidence must use its source revision")
        if len(set(self.chunk_ids)) != len(self.chunk_ids):
            raise ValueError("accepted entity chunk IDs must be unique")
        return self


class AcceptedRelation(DefinitionModel):
    """A graph relation with typed endpoints and source evidence."""

    schema_version: Literal["accepted-relation:v1"] = "accepted-relation:v1"
    relation_id: str = Field(min_length=1, max_length=64, pattern=_IDENTIFIER_PATTERN)
    relation_type: str = Field(min_length=1, max_length=64, pattern=_IDENTIFIER_PATTERN)
    subject_entity_id: str = Field(min_length=1, max_length=64, pattern=_IDENTIFIER_PATTERN)
    object_entity_id: str = Field(min_length=1, max_length=64, pattern=_IDENTIFIER_PATTERN)
    workspace_id: UUID
    environment_id: UUID
    source_revision_id: UUID
    ontology_version: VersionIdentifier
    ontology_digest: str = Field(pattern=_DIGEST_PATTERN)
    extraction_config_version: str = Field(min_length=1, max_length=32)
    extraction_config_digest: str = Field(pattern=_DIGEST_PATTERN)
    evidence: tuple[EvidenceReference, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_lineage(self) -> "AcceptedRelation":
        _require_non_zero(self.workspace_id, "workspace_id")
        _require_non_zero(self.environment_id, "environment_id")
        if any(
            reference.source_revision_id != self.source_revision_id
            for reference in self.evidence
        ):
            raise ValueError("accepted relation evidence must use its source revision")
        return self


class RejectionKind(str, Enum):
    ENTITY = "entity"
    RELATION = "relation"
    CONCLUSION = "conclusion"
    BATCH = "batch"


class ExtractionRejection(DefinitionModel):
    """Content-free reason why a candidate was not projected."""

    schema_version: Literal["extraction-rejection:v1"] = "extraction-rejection:v1"
    candidate_id: str = Field(min_length=1, max_length=64, pattern=_IDENTIFIER_PATTERN)
    kind: RejectionKind
    code: str = Field(min_length=1, max_length=64, pattern=_IDENTIFIER_PATTERN)
    workspace_id: UUID
    environment_id: UUID
    source_revision_id: UUID | None = None

    @model_validator(mode="after")
    def validate_revision(self) -> "ExtractionRejection":
        _require_non_zero(self.workspace_id, "workspace_id")
        _require_non_zero(self.environment_id, "environment_id")
        if self.source_revision_id is not None:
            _require_non_zero(self.source_revision_id, "source_revision_id")
        return self


class ExtractionSourceRevision(DefinitionModel):
    """Content-free source identity carried into the physical graph seam."""

    schema_version: Literal["extraction-source-revision:v1"] = (
        "extraction-source-revision:v1"
    )
    source_object_id: UUID
    source_revision_id: UUID
    workspace_id: UUID
    environment_id: UUID
    revision_digest: str = Field(pattern=_DIGEST_PATTERN)
    revision_number: int = Field(ge=1)
    status: Literal["current", "stale", "tombstoned", "missing", "ambiguous", "incomplete"] = (
        "current"
    )

    @model_validator(mode="after")
    def validate_identity(self) -> "ExtractionSourceRevision":
        for field_name in (
            "source_object_id",
            "source_revision_id",
            "workspace_id",
            "environment_id",
        ):
            _require_non_zero(getattr(self, field_name), field_name)
        return self


class ExtractionResult(DefinitionModel):
    """Deterministic extraction output and safe degraded diagnostics."""

    schema_version: Literal["ontology-extraction-result:v1"] = (
        "ontology-extraction-result:v1"
    )
    status: ExtractionStatus
    workspace_id: UUID
    environment_id: UUID
    ontology_version: VersionIdentifier
    ontology_digest: str = Field(pattern=_DIGEST_PATTERN)
    extraction_config_version: str = Field(min_length=1, max_length=32)
    extraction_config_digest: str = Field(pattern=_DIGEST_PATTERN)
    source_revisions: tuple[ExtractionSourceRevision, ...] = ()
    entities: tuple[AcceptedEntity, ...] = ()
    relations: tuple[AcceptedRelation, ...] = ()
    conclusions: tuple[ExtractionConclusion, ...] = ()
    rejections: tuple[ExtractionRejection, ...] = ()
    result_digest: str = Field(default="", pattern=_DIGEST_PATTERN)

    @model_validator(mode="after")
    def validate_and_pin_digest(self) -> "ExtractionResult":
        _require_non_zero(self.workspace_id, "workspace_id")
        _require_non_zero(self.environment_id, "environment_id")
        source_revisions = {
            revision.source_revision_id: revision for revision in self.source_revisions
        }
        if len(source_revisions) != len(self.source_revisions):
            raise ValueError("extraction result source revisions must be unique")
        if any(
            (revision.workspace_id, revision.environment_id)
            != (self.workspace_id, self.environment_id)
            for revision in self.source_revisions
        ):
            raise ValueError("extraction result source revision scope must match result")
        if any(
            entity.source_revision_id not in source_revisions
            for entity in self.entities
        ) or any(
            relation.source_revision_id not in source_revisions
            for relation in self.relations
        ):
            raise ValueError("accepted graph facts require a source revision snapshot")
        entity_ids = {entity.entity_id for entity in self.entities}
        if len(entity_ids) != len(self.entities):
            raise ValueError("extraction result entity IDs must be unique")
        if len({relation.relation_id for relation in self.relations}) != len(self.relations):
            raise ValueError("extraction result relation IDs must be unique")
        for entity in self.entities:
            if (entity.workspace_id, entity.environment_id) != (
                self.workspace_id,
                self.environment_id,
            ):
                raise ValueError("accepted entity scope must match extraction result")
            if (
                entity.ontology_version != self.ontology_version
                or entity.ontology_digest != self.ontology_digest
                or entity.extraction_config_version != self.extraction_config_version
                or entity.extraction_config_digest != self.extraction_config_digest
            ):
                raise ValueError("accepted entity versions must match extraction result")
        for relation in self.relations:
            if (relation.workspace_id, relation.environment_id) != (
                self.workspace_id,
                self.environment_id,
            ):
                raise ValueError("accepted relation scope must match extraction result")
            if (
                relation.ontology_version != self.ontology_version
                or relation.ontology_digest != self.ontology_digest
                or relation.extraction_config_version != self.extraction_config_version
                or relation.extraction_config_digest != self.extraction_config_digest
            ):
                raise ValueError("accepted relation versions must match extraction result")
            if (
                relation.subject_entity_id not in entity_ids
                or relation.object_entity_id not in entity_ids
            ):
                raise ValueError("accepted relation endpoints must be accepted entities")
        expected = _digest(self, exclude={"result_digest"})
        if self.result_digest not in ("", expected):
            raise ValueError("extraction result digest does not match")
        object.__setattr__(self, "result_digest", expected)
        return self


class GraphProjectionRequest(DefinitionModel):
    """Server-scoped input for writing one extraction result physically."""

    schema_version: Literal["graph-projection-request:v2"] = "graph-projection-request:v2"
    operation: Literal[
        ProjectionOperation.PROJECT,
        ProjectionOperation.UPDATE,
        ProjectionOperation.REMOVE,
        ProjectionOperation.REBUILD,
    ]
    workspace_id: UUID
    environment_id: UUID
    projection_run_id: UUID
    command_identity: str = Field(min_length=1, max_length=64, pattern=_IDENTIFIER_PATTERN)
    command_digest: str = Field(pattern=_DIGEST_PATTERN)
    projection_candidate: ProjectionCandidate
    projection_config: ProjectionConfigVersion
    canonical_boundary: CanonicalBoundary
    physical_namespace: ProjectionNamespace
    source_revision: ProjectionSourceRevision
    result: ExtractionResult

    @model_validator(mode="after")
    def validate_scope(self) -> "GraphProjectionRequest":
        _require_non_zero(self.workspace_id, "workspace_id")
        _require_non_zero(self.environment_id, "environment_id")
        _require_non_zero(self.projection_run_id, "projection_run_id")
        if self.projection_candidate.run_id != self.projection_run_id:
            raise ValueError("projection candidate must belong to the graph projection run")
        if (self.result.workspace_id, self.result.environment_id) != (
            self.workspace_id,
            self.environment_id,
        ):
            raise ValueError("extraction result scope must match graph projection request")
        matching_sources = tuple(
            source
            for source in self.result.source_revisions
            if source.source_revision_id == self.source_revision.source_revision_id
        )
        if len(matching_sources) != 1:
            raise ValueError("graph projection source revision must be unambiguous")
        source = matching_sources[0]
        if (
            source.source_object_id != self.source_revision.source_object_id
            or source.revision_digest != self.source_revision.revision_digest
            or source.revision_number != self.source_revision.revision_number
            or source.status != self.source_revision.status
        ):
            raise ValueError("graph projection source lineage must match extraction")
        return self


class ProjectionGraphWriter(Protocol):
    """Physical graph seam; implementations may be backed by Cognee."""

    async def write(self, request: GraphProjectionRequest) -> "GraphProjectionResult": ...


CandidateT = TypeVar("CandidateT", EntityCandidate, RelationCandidate)


def _rejection(
    *,
    candidate_id: str,
    kind: RejectionKind,
    code: str,
    workspace_id: UUID,
    environment_id: UUID,
    source_revision_id: UUID | None = None,
) -> ExtractionRejection:
    return ExtractionRejection(
        candidate_id=candidate_id,
        kind=kind,
        code=code,
        workspace_id=workspace_id,
        environment_id=environment_id,
        source_revision_id=source_revision_id,
    )


def _unique_candidates(
    candidates: tuple[CandidateT, ...],
) -> tuple[tuple[CandidateT, ...], set[str]]:
    unique: dict[str, CandidateT] = {}
    conflicts: set[str] = set()
    for candidate in candidates:
        existing = unique.get(candidate.candidate_id)
        if existing is None:
            unique[candidate.candidate_id] = candidate
        elif existing != candidate:
            conflicts.add(candidate.candidate_id)
    return (
        tuple(candidate for candidate in unique.values() if candidate.candidate_id not in conflicts),
        conflicts,
    )


def _normalise_evidence(
    *,
    source_revision_id: UUID,
    evidence: tuple[EvidenceReference, ...],
    revisions: dict[UUID, SourceRevisionFixture],
) -> tuple[tuple[EvidenceReference, ...] | None, str | None]:
    if not evidence:
        return None, "missing_evidence"
    by_id: dict[str, EvidenceReference] = {}
    for reference in evidence:
        if reference.source_revision_id != source_revision_id:
            return None, "cross_revision_evidence"
        revision = revisions.get(reference.source_revision_id)
        if revision is None:
            return None, "unknown_revision"
        if revision.kind == "tombstone" or revision.state == "deleted":
            return None, "tombstone_revision"
        if revision.state == "stale":
            return None, "stale_revision"
        if reference.quality == "ambiguous":
            return None, "ambiguous_evidence"
        if reference.quality != "accepted":
            return None, "missing_evidence"
        previous = by_id.get(reference.evidence_id)
        if previous is not None and previous != reference:
            return None, "ambiguous_evidence"
        by_id[reference.evidence_id] = reference
    return tuple(by_id[key] for key in sorted(by_id)), None


def _validate_candidate_revision(
    *,
    workspace_id: UUID,
    environment_id: UUID,
    source_revision_id: UUID,
    revisions: dict[UUID, SourceRevisionFixture],
) -> str | None:
    revision = revisions.get(source_revision_id)
    if revision is None:
        return "unknown_revision"
    if (revision.workspace_id, revision.environment_id) != (
        workspace_id,
        environment_id,
    ):
        return "cross_scope"
    if revision.kind == "tombstone" or revision.state == "deleted":
        return "tombstone_revision"
    if revision.state == "stale":
        return "stale_revision"
    return None


def _validate_chunks(
    *,
    source_revision_id: UUID,
    chunk_ids: tuple[str, ...],
    chunks: dict[str, ChunkFixture],
    revisions: dict[UUID, SourceRevisionFixture],
) -> str | None:
    if not chunk_ids:
        return "missing_evidence"
    if len(set(chunk_ids)) != len(chunk_ids):
        return "ambiguous_evidence"
    for chunk_id in chunk_ids:
        chunk = chunks.get(chunk_id)
        if chunk is None:
            return "missing_evidence"
        if chunk.source_revision_id != source_revision_id:
            return "cross_revision_evidence"
        _, evidence_code = _normalise_evidence(
            source_revision_id=source_revision_id,
            evidence=chunk.evidence,
            revisions=revisions,
        )
        if evidence_code is not None:
            return evidence_code
    return None


def _source_revision(revision: SourceRevisionFixture) -> ExtractionSourceRevision:
    status = "tombstoned" if revision.kind == "tombstone" or revision.state == "deleted" else revision.state
    return ExtractionSourceRevision(
        source_object_id=revision.source_object_id,
        source_revision_id=revision.source_revision_id,
        workspace_id=revision.workspace_id,
        environment_id=revision.environment_id,
        revision_digest=revision.revision_digest,
        revision_number=revision.revision_number,
        status=status,
    )


def _entity_mapping(
    *,
    mapping: EntityTypeMapping | None,
    ontology: OntologyDefinition,
) -> tuple[str | None, str | None]:
    if mapping is None or mapping.disposition == "forbidden":
        return None, "forbidden_entity_type"
    if mapping.disposition == "ambiguous":
        return None, "ambiguous_mapping"
    if mapping.target_entity_type not in {item.name for item in ontology.entity_types}:
        return None, "forbidden_entity_type"
    return mapping.target_entity_type, None


def _relation_mapping(
    *,
    mapping: RelationTypeMapping | None,
    ontology: OntologyDefinition,
) -> tuple[str | None, str | None]:
    if mapping is None or mapping.disposition == "forbidden":
        return None, "forbidden_relation_type"
    if mapping.disposition == "ambiguous":
        return None, "ambiguous_relation_mapping"
    if ontology.relation(mapping.target_relation_type) is None:
        return None, "forbidden_relation_type"
    return mapping.target_relation_type, None


def extract_projection(batch: ExtractionBatch) -> ExtractionResult:
    """Map candidates into a deterministic, evidence-bearing graph payload."""

    revisions = {revision.source_revision_id: revision for revision in batch.source_revisions}
    chunks = {chunk.chunk_id: chunk for chunk in batch.chunks}
    rejections: list[ExtractionRejection] = []
    if len(revisions) != len(batch.source_revisions):
        rejections.append(
            _rejection(
                candidate_id="batch",
                kind=RejectionKind.BATCH,
                code="duplicate_revision",
                workspace_id=batch.workspace_id,
                environment_id=batch.environment_id,
            )
        )
    if len(chunks) != len(batch.chunks):
        rejections.append(
            _rejection(
                candidate_id="batch",
                kind=RejectionKind.BATCH,
                code="duplicate_chunk",
                workspace_id=batch.workspace_id,
                environment_id=batch.environment_id,
            )
        )
    if batch.extraction_config.ontology_digest != batch.ontology.digest:
        rejections.append(
            _rejection(
                candidate_id="batch",
                kind=RejectionKind.BATCH,
                code="configuration_digest_mismatch",
                workspace_id=batch.workspace_id,
                environment_id=batch.environment_id,
            )
        )
    if batch.extraction_config.ontology_version != batch.ontology.version:
        rejections.append(
            _rejection(
                candidate_id="batch",
                kind=RejectionKind.BATCH,
                code="configuration_ontology_version_mismatch",
                workspace_id=batch.workspace_id,
                environment_id=batch.environment_id,
            )
        )

    if rejections:
        return ExtractionResult(
            status=ExtractionStatus.REJECTED,
            workspace_id=batch.workspace_id,
            environment_id=batch.environment_id,
            ontology_version=batch.ontology.version,
            ontology_digest=batch.ontology.digest,
            extraction_config_version=batch.extraction_config.version,
            extraction_config_digest=batch.extraction_config.digest,
            source_revisions=tuple(
                _source_revision(revision)
                for revision in batch.source_revisions
                if (revision.workspace_id, revision.environment_id)
                == (batch.workspace_id, batch.environment_id)
            ),
            conclusions=(),
            rejections=tuple(rejections),
        )

    entities, duplicate_entity_ids = _unique_candidates(batch.entities)
    relations, duplicate_relation_ids = _unique_candidates(batch.relations)
    rejections.extend(
        _rejection(
            candidate_id=candidate_id,
            kind=RejectionKind.ENTITY,
            code="duplicate_candidate",
            workspace_id=batch.workspace_id,
            environment_id=batch.environment_id,
        )
        for candidate_id in sorted(duplicate_entity_ids)
    )
    rejections.extend(
        _rejection(
            candidate_id=candidate_id,
            kind=RejectionKind.RELATION,
            code="duplicate_candidate",
            workspace_id=batch.workspace_id,
            environment_id=batch.environment_id,
        )
        for candidate_id in sorted(duplicate_relation_ids)
    )

    valid_conclusions: list[ExtractionConclusion] = []
    for conclusion in sorted(batch.conclusions, key=lambda item: item.conclusion_id):
        code = _validate_candidate_revision(
            workspace_id=batch.workspace_id,
            environment_id=batch.environment_id,
            source_revision_id=conclusion.source_revision_id,
            revisions=revisions,
        )
        if code is not None:
            rejections.append(
                _rejection(
                    candidate_id=conclusion.conclusion_id,
                    kind=RejectionKind.CONCLUSION,
                    code=code,
                    workspace_id=batch.workspace_id,
                    environment_id=batch.environment_id,
                    source_revision_id=conclusion.source_revision_id,
                )
            )
            continue
        normalised_evidence, evidence_code = _normalise_evidence(
            source_revision_id=conclusion.source_revision_id,
            evidence=conclusion.evidence,
            revisions=revisions,
        )
        if evidence_code is not None or normalised_evidence is None:
            rejections.append(
                _rejection(
                    candidate_id=conclusion.conclusion_id,
                    kind=RejectionKind.CONCLUSION,
                    code=evidence_code or "missing_evidence",
                    workspace_id=batch.workspace_id,
                    environment_id=batch.environment_id,
                    source_revision_id=conclusion.source_revision_id,
                )
            )
            continue
        valid_conclusions.append(
            conclusion.model_copy(update={"evidence": normalised_evidence})
        )

    accepted_entities: dict[str, AcceptedEntity] = {}
    for candidate in sorted(entities, key=lambda item: item.candidate_id):
        code = _validate_candidate_revision(
            workspace_id=batch.workspace_id,
            environment_id=batch.environment_id,
            source_revision_id=candidate.source_revision_id,
            revisions=revisions,
        )
        if code is not None:
            rejections.append(
                _rejection(
                    candidate_id=candidate.candidate_id,
                    kind=RejectionKind.ENTITY,
                    code=code,
                    workspace_id=batch.workspace_id,
                    environment_id=batch.environment_id,
                    source_revision_id=candidate.source_revision_id,
                )
            )
            continue
        chunk_code = _validate_chunks(
            source_revision_id=candidate.source_revision_id,
            chunk_ids=candidate.chunk_ids,
            chunks=chunks,
            revisions=revisions,
        )
        if chunk_code is not None:
            rejections.append(
                _rejection(
                    candidate_id=candidate.candidate_id,
                    kind=RejectionKind.ENTITY,
                    code=chunk_code,
                    workspace_id=batch.workspace_id,
                    environment_id=batch.environment_id,
                    source_revision_id=candidate.source_revision_id,
                )
            )
            continue
        entity_type, mapping_code = _entity_mapping(
            mapping=batch.extraction_config.entity_mapping(candidate.source_label),
            ontology=batch.ontology,
        )
        if mapping_code is not None:
            rejections.append(
                _rejection(
                    candidate_id=candidate.candidate_id,
                    kind=RejectionKind.ENTITY,
                    code=mapping_code,
                    workspace_id=batch.workspace_id,
                    environment_id=batch.environment_id,
                    source_revision_id=candidate.source_revision_id,
                )
            )
            continue
        normalised_evidence, evidence_code = _normalise_evidence(
            source_revision_id=candidate.source_revision_id,
            evidence=candidate.evidence,
            revisions=revisions,
        )
        if evidence_code is not None or normalised_evidence is None:
            rejections.append(
                _rejection(
                    candidate_id=candidate.candidate_id,
                    kind=RejectionKind.ENTITY,
                    code=evidence_code or "missing_evidence",
                    workspace_id=batch.workspace_id,
                    environment_id=batch.environment_id,
                    source_revision_id=candidate.source_revision_id,
                )
            )
            continue
        accepted_entities[candidate.candidate_id] = AcceptedEntity(
            entity_id=candidate.candidate_id,
            entity_type=entity_type,  # type: ignore[arg-type]
            canonical_name=candidate.canonical_name,
            workspace_id=batch.workspace_id,
            environment_id=batch.environment_id,
            source_revision_id=candidate.source_revision_id,
            ontology_version=batch.ontology.version,
            ontology_digest=batch.ontology.digest,
            extraction_config_version=batch.extraction_config.version,
            extraction_config_digest=batch.extraction_config.digest,
            chunk_ids=tuple(sorted(candidate.chunk_ids)),
            evidence=normalised_evidence,
        )

    accepted_relations: dict[str, AcceptedRelation] = {}
    for candidate in sorted(relations, key=lambda item: item.candidate_id):
        code = _validate_candidate_revision(
            workspace_id=batch.workspace_id,
            environment_id=batch.environment_id,
            source_revision_id=candidate.source_revision_id,
            revisions=revisions,
        )
        if code is not None:
            rejections.append(
                _rejection(
                    candidate_id=candidate.candidate_id,
                    kind=RejectionKind.RELATION,
                    code=code,
                    workspace_id=batch.workspace_id,
                    environment_id=batch.environment_id,
                    source_revision_id=candidate.source_revision_id,
                )
            )
            continue
        relation_type, mapping_code = _relation_mapping(
            mapping=batch.extraction_config.relation_mapping(candidate.source_label),
            ontology=batch.ontology,
        )
        if mapping_code is not None:
            rejections.append(
                _rejection(
                    candidate_id=candidate.candidate_id,
                    kind=RejectionKind.RELATION,
                    code=mapping_code,
                    workspace_id=batch.workspace_id,
                    environment_id=batch.environment_id,
                    source_revision_id=candidate.source_revision_id,
                )
            )
            continue
        source = accepted_entities.get(candidate.source_entity_candidate_id)
        target = accepted_entities.get(candidate.target_entity_candidate_id)
        ontology_relation = batch.ontology.relation(relation_type or "")
        if (
            source is None
            or target is None
            or ontology_relation is None
            or source.entity_type != ontology_relation.source_entity_type
            or target.entity_type != ontology_relation.target_entity_type
            or source.source_revision_id != candidate.source_revision_id
            or target.source_revision_id != candidate.source_revision_id
        ):
            rejections.append(
                _rejection(
                    candidate_id=candidate.candidate_id,
                    kind=RejectionKind.RELATION,
                    code="invalid_relation",
                    workspace_id=batch.workspace_id,
                    environment_id=batch.environment_id,
                    source_revision_id=candidate.source_revision_id,
                )
            )
            continue
        normalised_evidence, evidence_code = _normalise_evidence(
            source_revision_id=candidate.source_revision_id,
            evidence=candidate.evidence,
            revisions=revisions,
        )
        if evidence_code is not None or normalised_evidence is None:
            rejections.append(
                _rejection(
                    candidate_id=candidate.candidate_id,
                    kind=RejectionKind.RELATION,
                    code=evidence_code or "missing_evidence",
                    workspace_id=batch.workspace_id,
                    environment_id=batch.environment_id,
                    source_revision_id=candidate.source_revision_id,
                )
            )
            continue
        accepted_relations[candidate.candidate_id] = AcceptedRelation(
            relation_id=candidate.candidate_id,
            relation_type=relation_type,  # type: ignore[arg-type]
            subject_entity_id=source.entity_id,
            object_entity_id=target.entity_id,
            workspace_id=batch.workspace_id,
            environment_id=batch.environment_id,
            source_revision_id=candidate.source_revision_id,
            ontology_version=batch.ontology.version,
            ontology_digest=batch.ontology.digest,
            extraction_config_version=batch.extraction_config.version,
            extraction_config_digest=batch.extraction_config.digest,
            evidence=normalised_evidence,
        )

    rejections.sort(key=lambda item: (item.kind.value, item.candidate_id, item.code))
    status = (
        ExtractionStatus.ACCEPTED
        if not rejections
        else ExtractionStatus.DEGRADED
        if accepted_entities or accepted_relations
        else ExtractionStatus.REJECTED
    )
    return ExtractionResult(
        status=status,
        workspace_id=batch.workspace_id,
        environment_id=batch.environment_id,
        ontology_version=batch.ontology.version,
        ontology_digest=batch.ontology.digest,
        extraction_config_version=batch.extraction_config.version,
        extraction_config_digest=batch.extraction_config.digest,
        source_revisions=tuple(
            _source_revision(revision)
            for revision in batch.source_revisions
            if (revision.workspace_id, revision.environment_id)
            == (batch.workspace_id, batch.environment_id)
        ),
        entities=tuple(accepted_entities.values()),
        relations=tuple(accepted_relations.values()),
        conclusions=tuple(valid_conclusions),
        rejections=tuple(rejections),
    )


__all__ = [
    "AcceptedEntity",
    "AcceptedRelation",
    "EntityCandidate",
    "ExtractionBatch",
    "ExtractionRejection",
    "ExtractionResult",
    "ExtractionSourceRevision",
    "ExtractionStatus",
    "GraphProjectionRequest",
    "ProjectionGraphWriter",
    "RelationCandidate",
    "extract_projection",
]
