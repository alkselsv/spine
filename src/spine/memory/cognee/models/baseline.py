"""Synthetic R1 ontology and evidence-linked projection fixtures.

The definitions in this module are adapter inputs, not canonical PostgreSQL
records. They deliberately contain no Cognee types and no protected source
payloads. A fixture can describe an invalid projection candidate so that the
validator can exercise fail-closed behavior at the ContextProjection seam.
"""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Final, Literal
from uuid import UUID

from pydantic import Field, model_validator

from spine.domain.common import DefinitionModel


BASELINE_ONTOLOGY_SCHEMA: Final = "ontology-definition:r1"
BASELINE_EXTRACTION_CONFIG_SCHEMA: Final = "extraction-config:r1"
BASELINE_FIXTURE_SCHEMA: Final = "context-projection-fixture:r1"
STABLE_LOCATOR_SCHEMA: Final = "stable-locator:r1"
BASELINE_ONTOLOGY_VERSION: Final = "r1"
BASELINE_EXTRACTION_CONFIG_VERSION: Final = "r1"
BASELINE_ONTOLOGY_DIGEST: Final = (
    "90b2bf6ad1835c48d1ee46b250c47feabeb1ecb7d9e5bb049da2fccc4422ecee"
)
BASELINE_EXTRACTION_CONFIG_DIGEST: Final = (
    "e90df3e1eb73c4a8a722dd393ecdf21d3a88c1c3e852f65262efa23998f4202a"
)
_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
_IDENTIFIER_PATTERN = r"^[a-z][a-z0-9_.:-]{0,63}$"

EntityDisposition = Literal["accepted", "forbidden", "ambiguous"]
RelationDisposition = Literal["accepted", "forbidden", "ambiguous"]
RevisionState = Literal["current", "stale", "deleted"]
EvidenceQuality = Literal["accepted", "ambiguous", "missing", "stale"]


def _digest(model: DefinitionModel, *, exclude: set[str]) -> str:
    payload = model.model_dump(mode="json", exclude=exclude)
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


class OntologyEntityType(DefinitionModel):
    """One entity type accepted by the R1 extraction boundary."""

    name: str = Field(min_length=1, max_length=64, pattern=_IDENTIFIER_PATTERN)
    aliases: tuple[str, ...] = ()


class OntologyRelationType(DefinitionModel):
    """One relation and the endpoint types it permits."""

    name: str = Field(min_length=1, max_length=64, pattern=_IDENTIFIER_PATTERN)
    source_entity_type: str = Field(min_length=1, max_length=64, pattern=_IDENTIFIER_PATTERN)
    target_entity_type: str = Field(min_length=1, max_length=64, pattern=_IDENTIFIER_PATTERN)
    aliases: tuple[str, ...] = ()


class OntologyDefinition(DefinitionModel):
    """Versioned baseline definition consumed by a projection adapter.

    This is intentionally not an Issue #7 persistence record. It is the
    deterministic payload that such a record would pin.
    """

    schema_version: Literal[BASELINE_ONTOLOGY_SCHEMA] = BASELINE_ONTOLOGY_SCHEMA
    version: str = Field(min_length=1, max_length=32, pattern=_IDENTIFIER_PATTERN)
    entity_types: tuple[OntologyEntityType, ...] = Field(min_length=1)
    relation_types: tuple[OntologyRelationType, ...] = Field(min_length=1)
    digest: str = ""

    @model_validator(mode="after")
    def validate_and_pin_digest(self) -> "OntologyDefinition":
        entity_names = {item.name for item in self.entity_types}
        if len(entity_names) != len(self.entity_types):
            raise ValueError("ontology entity type names must be unique")
        relation_names = {item.name for item in self.relation_types}
        if len(relation_names) != len(self.relation_types):
            raise ValueError("ontology relation type names must be unique")
        for relation in self.relation_types:
            if relation.source_entity_type not in entity_names:
                raise ValueError("ontology relation source type is not declared")
            if relation.target_entity_type not in entity_names:
                raise ValueError("ontology relation target type is not declared")
        expected = _digest(self, exclude={"digest"})
        if self.digest not in ("", expected):
            raise ValueError("ontology digest does not match its definition")
        object.__setattr__(self, "digest", expected)
        return self

    def relation(self, name: str) -> OntologyRelationType | None:
        return next((item for item in self.relation_types if item.name == name), None)


class EntityTypeMapping(DefinitionModel):
    """Maps a provider label to an ontology type or a safe non-acceptance."""

    source_label: str = Field(min_length=1, max_length=64, pattern=_IDENTIFIER_PATTERN)
    target_entity_type: str = Field(min_length=1, max_length=64, pattern=_IDENTIFIER_PATTERN)
    disposition: EntityDisposition


class RelationTypeMapping(DefinitionModel):
    """Maps a provider relation label to a typed ontology relation."""

    source_label: str = Field(min_length=1, max_length=64, pattern=_IDENTIFIER_PATTERN)
    target_relation_type: str = Field(min_length=1, max_length=64, pattern=_IDENTIFIER_PATTERN)
    disposition: RelationDisposition


class ExtractionConfiguration(DefinitionModel):
    """Versioned, provider-neutral extraction inputs for the baseline."""

    schema_version: Literal[BASELINE_EXTRACTION_CONFIG_SCHEMA] = (
        BASELINE_EXTRACTION_CONFIG_SCHEMA
    )
    version: str = Field(min_length=1, max_length=32, pattern=_IDENTIFIER_PATTERN)
    ontology_version: str = Field(min_length=1, max_length=32, pattern=_IDENTIFIER_PATTERN)
    ontology_digest: str = Field(pattern=_DIGEST_PATTERN)
    locator_schema_version: Literal[STABLE_LOCATOR_SCHEMA] = STABLE_LOCATOR_SCHEMA
    extractor_contract_version: str = Field(
        min_length=1, max_length=64, pattern=_IDENTIFIER_PATTERN
    )
    minimum_entity_confidence: float = Field(ge=0.0, le=1.0)
    minimum_relation_confidence: float = Field(ge=0.0, le=1.0)
    entity_mappings: tuple[EntityTypeMapping, ...] = Field(min_length=1)
    relation_mappings: tuple[RelationTypeMapping, ...] = Field(min_length=1)
    digest: str = ""

    @model_validator(mode="after")
    def validate_and_pin_digest(self) -> "ExtractionConfiguration":
        if len({item.source_label for item in self.entity_mappings}) != len(
            self.entity_mappings
        ):
            raise ValueError("extraction entity mapping labels must be unique")
        if len({item.source_label for item in self.relation_mappings}) != len(
            self.relation_mappings
        ):
            raise ValueError("extraction relation mapping labels must be unique")
        expected = _digest(self, exclude={"digest"})
        if self.digest not in ("", expected):
            raise ValueError("extraction configuration digest does not match")
        object.__setattr__(self, "digest", expected)
        return self

    def entity_mapping(self, source_label: str) -> EntityTypeMapping | None:
        return next(
            (item for item in self.entity_mappings if item.source_label == source_label),
            None,
        )

    def relation_mapping(self, source_label: str) -> RelationTypeMapping | None:
        return next(
            (item for item in self.relation_mappings if item.source_label == source_label),
            None,
        )


class StableLocator(DefinitionModel):
    """A citation-grade locator with one explicit, stable shape."""

    schema_version: Literal[STABLE_LOCATOR_SCHEMA] = STABLE_LOCATOR_SCHEMA
    kind: Literal["pdf_page", "text_range", "xlsx_row", "message"]
    page: int | None = Field(default=None, ge=1)
    start: int | None = Field(default=None, ge=0)
    end: int | None = Field(default=None, gt=0)
    sheet: str | None = Field(default=None, min_length=1, max_length=128)
    row: int | None = Field(default=None, ge=1)
    message_id: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_shape(self) -> "StableLocator":
        populated = {
            key
            for key, value in {
                "page": self.page,
                "start": self.start,
                "end": self.end,
                "sheet": self.sheet,
                "row": self.row,
                "message_id": self.message_id,
            }.items()
            if value is not None
        }
        expected_fields = {
            "pdf_page": {"page"},
            "text_range": {"start", "end"},
            "xlsx_row": {"sheet", "row"},
            "message": {"message_id"},
        }
        if populated != expected_fields[self.kind]:
            raise ValueError("stable locator fields do not match its kind")
        if self.kind == "text_range" and self.end <= self.start:  # type: ignore[operator]
            raise ValueError("stable locator text range must be non-empty")
        return self


class SourceRevisionFixture(DefinitionModel):
    """Synthetic source revision used only by offline projection fixtures."""

    source_revision_id: UUID
    source_object_id: UUID
    workspace_id: UUID
    environment_id: UUID
    revision_digest: str = Field(pattern=_DIGEST_PATTERN)
    revision_number: int = Field(ge=1)
    kind: Literal["content", "tombstone"]
    state: RevisionState
    content: str | None = None
    deletion_reason: str | None = Field(
        default=None, min_length=1, max_length=64, pattern=_IDENTIFIER_PATTERN
    )

    @model_validator(mode="after")
    def validate_revision_kind(self) -> "SourceRevisionFixture":
        if self.kind == "content":
            if self.content is None or self.deletion_reason is not None or self.state == "deleted":
                raise ValueError("content fixture requires non-deleted content")
        elif self.content is not None or self.deletion_reason is None or self.state != "deleted":
            raise ValueError("tombstone fixture requires only deletion provenance")
        return self


class EvidenceReference(DefinitionModel):
    """Evidence attached to a projected object, never an unscoped citation."""

    evidence_id: str = Field(min_length=1, max_length=64, pattern=_IDENTIFIER_PATTERN)
    source_revision_id: UUID
    locator: StableLocator
    quality: EvidenceQuality = "accepted"


class ChunkFixture(DefinitionModel):
    chunk_id: str = Field(min_length=1, max_length=64, pattern=_IDENTIFIER_PATTERN)
    source_revision_id: UUID
    evidence: tuple[EvidenceReference, ...] = ()
    text: str = Field(min_length=1, max_length=512)


class EntityFixture(DefinitionModel):
    entity_id: str = Field(min_length=1, max_length=64, pattern=_IDENTIFIER_PATTERN)
    entity_type: str = Field(min_length=1, max_length=64, pattern=_IDENTIFIER_PATTERN)
    source_revision_id: UUID
    canonical_name: str = Field(min_length=1, max_length=128)
    chunk_ids: tuple[str, ...] = ()
    evidence: tuple[EvidenceReference, ...] = ()


class RelationFixture(DefinitionModel):
    relation_id: str = Field(min_length=1, max_length=64, pattern=_IDENTIFIER_PATTERN)
    relation_type: str = Field(min_length=1, max_length=64, pattern=_IDENTIFIER_PATTERN)
    source_revision_id: UUID
    source_entity_id: str = Field(min_length=1, max_length=64, pattern=_IDENTIFIER_PATTERN)
    target_entity_id: str = Field(min_length=1, max_length=64, pattern=_IDENTIFIER_PATTERN)
    evidence: tuple[EvidenceReference, ...] = ()


class ExtractionConclusion(DefinitionModel):
    """Probabilistic model output; it is not a canonical source fact."""

    conclusion_id: str = Field(min_length=1, max_length=64, pattern=_IDENTIFIER_PATTERN)
    source_revision_id: UUID
    proposed_entity_type: str | None = None
    proposed_relation_type: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: tuple[EvidenceReference, ...] = ()

    @model_validator(mode="after")
    def validate_one_conclusion_kind(self) -> "ExtractionConclusion":
        if (self.proposed_entity_type is None) == (self.proposed_relation_type is None):
            raise ValueError("conclusion must propose exactly one entity or relation")
        return self


class ProjectionFixture(DefinitionModel):
    """One valid or intentionally invalid candidate for offline qualification."""

    schema_version: Literal[BASELINE_FIXTURE_SCHEMA] = BASELINE_FIXTURE_SCHEMA
    case_id: str = Field(min_length=1, max_length=64, pattern=_IDENTIFIER_PATTERN)
    ontology: OntologyDefinition
    extraction_config: ExtractionConfiguration
    source_revisions: tuple[SourceRevisionFixture, ...] = Field(min_length=1)
    chunks: tuple[ChunkFixture, ...] = ()
    entities: tuple[EntityFixture, ...] = ()
    relations: tuple[RelationFixture, ...] = ()
    conclusions: tuple[ExtractionConclusion, ...] = ()


class FixtureValidationResult(DefinitionModel):
    """Safe validation output containing identifiers and no source text."""

    case_id: str
    accepted_entity_ids: tuple[str, ...] = ()
    accepted_relation_ids: tuple[str, ...] = ()


class FixtureValidationError(ValueError):
    """Typed, disclosure-safe failure for a malformed projection fixture."""

    def __init__(self, code: str, case_id: str) -> None:
        super().__init__(f"{code}:{case_id}")
        self.code = code
        self.case_id = case_id
        self.protected_content: None = None


def _validate_evidence(
    *,
    case_id: str,
    source_revision_id: UUID,
    evidence: tuple[EvidenceReference, ...],
    revisions: dict[UUID, SourceRevisionFixture],
) -> None:
    if not evidence:
        raise FixtureValidationError("missing_evidence", case_id)
    for reference in evidence:
        if reference.source_revision_id != source_revision_id:
            raise FixtureValidationError("cross_revision_evidence", case_id)
        revision = revisions.get(reference.source_revision_id)
        if revision is None:
            raise FixtureValidationError("unknown_revision", case_id)
        if revision.kind == "tombstone" or revision.state == "deleted":
            raise FixtureValidationError("tombstone_revision", case_id)
        if revision.state == "stale":
            raise FixtureValidationError("stale_revision", case_id)
        if reference.quality == "ambiguous":
            raise FixtureValidationError("ambiguous_evidence", case_id)
        if reference.quality != "accepted":
            raise FixtureValidationError("missing_evidence", case_id)


def validate_fixture(fixture: ProjectionFixture) -> FixtureValidationResult:
    """Reject evidence-free, stale, deleted, ambiguous, or cross-revision facts."""

    revisions = {item.source_revision_id: item for item in fixture.source_revisions}
    if len(revisions) != len(fixture.source_revisions):
        raise FixtureValidationError("duplicate_revision", fixture.case_id)
    if len(
        {(item.workspace_id, item.environment_id) for item in fixture.source_revisions}
    ) != 1:
        raise FixtureValidationError("cross_scope", fixture.case_id)
    if fixture.extraction_config.ontology_digest != fixture.ontology.digest:
        raise FixtureValidationError("configuration_digest_mismatch", fixture.case_id)

    chunks = {item.chunk_id: item for item in fixture.chunks}
    entities = {item.entity_id: item for item in fixture.entities}
    relations = {item.relation_id: item for item in fixture.relations}
    if (
        len(entities) != len(fixture.entities)
        or len(chunks) != len(fixture.chunks)
        or len(relations) != len(fixture.relations)
    ):
        raise FixtureValidationError("duplicate_fixture_id", fixture.case_id)

    for chunk in fixture.chunks:
        _validate_evidence(
            case_id=fixture.case_id,
            source_revision_id=chunk.source_revision_id,
            evidence=chunk.evidence,
            revisions=revisions,
        )

    accepted_entities: list[str] = []
    for entity in fixture.entities:
        revision = revisions.get(entity.source_revision_id)
        if revision is None:
            raise FixtureValidationError("unknown_revision", fixture.case_id)
        if revision.kind == "tombstone" or revision.state == "deleted":
            raise FixtureValidationError("tombstone_revision", fixture.case_id)
        if revision.state == "stale":
            raise FixtureValidationError("stale_revision", fixture.case_id)
        mapping = fixture.extraction_config.entity_mapping(entity.entity_type)
        if mapping is None or mapping.disposition == "forbidden":
            raise FixtureValidationError("forbidden_entity_type", fixture.case_id)
        if mapping.disposition == "ambiguous":
            raise FixtureValidationError("ambiguous_mapping", fixture.case_id)
        if mapping.target_entity_type != entity.entity_type:
            raise FixtureValidationError("forbidden_entity_type", fixture.case_id)
        if entity.entity_type not in {
            item.name for item in fixture.ontology.entity_types
        }:
            raise FixtureValidationError("forbidden_entity_type", fixture.case_id)
        _validate_evidence(
            case_id=fixture.case_id,
            source_revision_id=entity.source_revision_id,
            evidence=entity.evidence,
            revisions=revisions,
        )
        for chunk_id in entity.chunk_ids:
            chunk = chunks.get(chunk_id)
            if chunk is None or chunk.source_revision_id != entity.source_revision_id:
                raise FixtureValidationError("cross_revision_evidence", fixture.case_id)
        accepted_entities.append(entity.entity_id)

    accepted_relations: list[str] = []
    for relation in fixture.relations:
        source = entities.get(relation.source_entity_id)
        target = entities.get(relation.target_entity_id)
        ontology_relation = fixture.ontology.relation(relation.relation_type)
        mapping = fixture.extraction_config.relation_mapping(relation.relation_type)
        if mapping is not None and mapping.disposition == "ambiguous":
            raise FixtureValidationError("ambiguous_relation_mapping", fixture.case_id)
        if (
            source is None
            or target is None
            or ontology_relation is None
            or mapping is None
            or mapping.disposition != "accepted"
            or mapping.target_relation_type != relation.relation_type
            or source.entity_type != ontology_relation.source_entity_type
            or target.entity_type != ontology_relation.target_entity_type
            or source.source_revision_id != relation.source_revision_id
            or target.source_revision_id != relation.source_revision_id
        ):
            raise FixtureValidationError("invalid_relation", fixture.case_id)
        _validate_evidence(
            case_id=fixture.case_id,
            source_revision_id=relation.source_revision_id,
            evidence=relation.evidence,
            revisions=revisions,
        )
        accepted_relations.append(relation.relation_id)

    return FixtureValidationResult(
        case_id=fixture.case_id,
        accepted_entity_ids=tuple(accepted_entities),
        accepted_relation_ids=tuple(accepted_relations),
    )


def _revision(
    revision_id: UUID,
    source_object_id: UUID,
    workspace_id: UUID,
    environment_id: UUID,
    *,
    number: int,
    state: RevisionState = "current",
    content: str | None = "Synthetic source content.",
    kind: Literal["content", "tombstone"] = "content",
    deletion_reason: str | None = None,
) -> SourceRevisionFixture:
    digest = sha256(
        (content if content is not None else f"tombstone:{revision_id}").encode("utf-8")
    ).hexdigest()
    return SourceRevisionFixture(
        source_revision_id=revision_id,
        source_object_id=source_object_id,
        workspace_id=workspace_id,
        environment_id=environment_id,
        revision_digest=digest,
        revision_number=number,
        kind=kind,
        state=state,
        content=content,
        deletion_reason=deletion_reason,
    )


def _evidence(
    evidence_id: str,
    revision_id: UUID,
    *,
    quality: EvidenceQuality = "accepted",
) -> EvidenceReference:
    return EvidenceReference(
        evidence_id=evidence_id,
        source_revision_id=revision_id,
        locator=StableLocator(kind="pdf_page", page=1),
        quality=quality,
    )


def build_baseline_fixtures() -> dict[str, ProjectionFixture]:
    """Return deterministic positive and negative fixtures for qualification."""

    workspace_id = UUID("11111111-1111-4111-8111-111111111111")
    environment_id = UUID("22222222-2222-4222-8222-222222222222")
    source_object_id = UUID("33333333-3333-4333-8333-333333333333")
    current_revision_id = UUID("44444444-4444-4444-8444-444444444444")
    stale_revision_id = UUID("55555555-5555-4555-8555-555555555555")
    tombstone_revision_id = UUID("66666666-6666-4666-8666-666666666666")

    current = _revision(
        current_revision_id,
        source_object_id,
        workspace_id,
        environment_id,
        number=2,
        content="Acme employs Ada.",
    )
    stale = _revision(
        stale_revision_id,
        source_object_id,
        workspace_id,
        environment_id,
        number=1,
        state="stale",
        content="Old synthetic content.",
    )
    tombstone = _revision(
        tombstone_revision_id,
        source_object_id,
        workspace_id,
        environment_id,
        number=3,
        state="deleted",
        kind="tombstone",
        content=None,
        deletion_reason="source_deleted",
    )
    acme_evidence = _evidence("evidence-acme", current_revision_id)
    owner_evidence = _evidence("evidence-owner", current_revision_id)
    relation_evidence = _evidence("evidence-relation", current_revision_id)
    acme = EntityFixture(
        entity_id="entity-acme",
        entity_type="organization",
        source_revision_id=current_revision_id,
        canonical_name="Acme",
        chunk_ids=("chunk-current",),
        evidence=(acme_evidence,),
    )
    owner = EntityFixture(
        entity_id="entity-owner",
        entity_type="person",
        source_revision_id=current_revision_id,
        canonical_name="Ada",
        chunk_ids=("chunk-current",),
        evidence=(owner_evidence,),
    )
    chunk = ChunkFixture(
        chunk_id="chunk-current",
        source_revision_id=current_revision_id,
        evidence=(acme_evidence, owner_evidence, relation_evidence),
        text="Synthetic chunk for evidence-link tests.",
    )
    relation = RelationFixture(
        relation_id="relation-owner",
        relation_type="works_for",
        source_revision_id=current_revision_id,
        source_entity_id="entity-owner",
        target_entity_id="entity-acme",
        evidence=(relation_evidence,),
    )

    def fixture(
        case_id: str,
        *,
        revisions: tuple[SourceRevisionFixture, ...] = (current, stale),
        chunks: tuple[ChunkFixture, ...] = (chunk,),
        entities: tuple[EntityFixture, ...] = (acme, owner),
        relations: tuple[RelationFixture, ...] = (relation,),
        conclusions: tuple[ExtractionConclusion, ...] = (),
    ) -> ProjectionFixture:
        return ProjectionFixture(
            case_id=case_id,
            ontology=BASELINE_ONTOLOGY,
            extraction_config=BASELINE_EXTRACTION_CONFIG,
            source_revisions=revisions,
            chunks=chunks,
            entities=entities,
            relations=relations,
            conclusions=conclusions,
        )

    forbidden = acme.model_copy(update={"entity_type": "secret_project"})
    invalid_relation = relation.model_copy(update={"relation_type": "owns"})
    missing_evidence = acme.model_copy(update={"evidence": ()})
    ambiguous_evidence = acme.model_copy(
        update={
            "evidence": (
                _evidence(
                    "evidence-ambiguous",
                    current_revision_id,
                    quality="ambiguous",
                ),
            )
        }
    )
    ambiguous_mapping = acme.model_copy(update={"entity_type": "company"})
    cross_revision_evidence = acme.model_copy(
        update={"evidence": (_evidence("evidence-stale", stale_revision_id),)}
    )
    stale_entity = acme.model_copy(update={"source_revision_id": stale_revision_id})
    tombstone_entity = acme.model_copy(update={"source_revision_id": tombstone_revision_id})
    probabilistic_conclusion = ExtractionConclusion(
        conclusion_id="conclusion-unpromoted",
        source_revision_id=current_revision_id,
        proposed_entity_type="secret_project",
        confidence=0.99,
        evidence=(acme_evidence,),
    )

    return {
        "positive": fixture("positive", conclusions=(probabilistic_conclusion,)),
        "forbidden": fixture("forbidden", entities=(forbidden, owner)),
        "invalid_relation": fixture("invalid-relation", relations=(invalid_relation,)),
        "missing_evidence": fixture("missing-evidence", entities=(missing_evidence, owner)),
        "ambiguous_evidence": fixture(
            "ambiguous-evidence", entities=(ambiguous_evidence, owner)
        ),
        "ambiguous_mapping": fixture(
            "ambiguous-mapping", entities=(ambiguous_mapping, owner)
        ),
        "cross_revision_evidence": fixture(
            "cross-revision-evidence", entities=(cross_revision_evidence, owner)
        ),
        "stale_revision": fixture("stale-revision", entities=(stale_entity, owner)),
        "tombstone_revision": fixture(
            "tombstone-revision",
            revisions=(current, tombstone),
            entities=(tombstone_entity, owner),
        ),
    }


BASELINE_ONTOLOGY = OntologyDefinition(
    version=BASELINE_ONTOLOGY_VERSION,
    digest=BASELINE_ONTOLOGY_DIGEST,
    entity_types=(
        OntologyEntityType(name="person", aliases=("human",)),
        OntologyEntityType(name="organization", aliases=("company",)),
        OntologyEntityType(name="document"),
        OntologyEntityType(name="location"),
    ),
    relation_types=(
        OntologyRelationType(
            name="works_for",
            source_entity_type="person",
            target_entity_type="organization",
            aliases=("employed_by",),
        ),
        OntologyRelationType(
            name="located_in",
            source_entity_type="organization",
            target_entity_type="location",
        ),
    ),
)

BASELINE_EXTRACTION_CONFIG = ExtractionConfiguration(
    version=BASELINE_EXTRACTION_CONFIG_VERSION,
    ontology_version=BASELINE_ONTOLOGY.version,
    ontology_digest=BASELINE_ONTOLOGY.digest,
    digest=BASELINE_EXTRACTION_CONFIG_DIGEST,
    extractor_contract_version="provider-neutral:v1",
    minimum_entity_confidence=0.75,
    minimum_relation_confidence=0.8,
    entity_mappings=(
        EntityTypeMapping(
            source_label="person",
            target_entity_type="person",
            disposition="accepted",
        ),
        EntityTypeMapping(
            source_label="organization",
            target_entity_type="organization",
            disposition="accepted",
        ),
        EntityTypeMapping(
            source_label="secret_project",
            target_entity_type="secret_project",
            disposition="forbidden",
        ),
        EntityTypeMapping(
            source_label="company",
            target_entity_type="organization",
            disposition="ambiguous",
        ),
    ),
    relation_mappings=(
        RelationTypeMapping(
            source_label="works_for",
            target_relation_type="works_for",
            disposition="accepted",
        ),
        RelationTypeMapping(
            source_label="owns",
            target_relation_type="owns",
            disposition="forbidden",
        ),
        RelationTypeMapping(
            source_label="related_to",
            target_relation_type="related_to",
            disposition="ambiguous",
        ),
    ),
)

# Names used by downstream adapter and qualification tickets.
OntologyVersionDefinition = OntologyDefinition
ProjectionConfigVersionDefinition = ExtractionConfiguration
OntologyVersion = OntologyDefinition
ProjectionConfigVersion = ExtractionConfiguration
SourceRevision = SourceRevisionFixture
Chunk = ChunkFixture
Entity = EntityFixture
Relation = RelationFixture
EvidenceRef = EvidenceReference


__all__ = [
    "BASELINE_EXTRACTION_CONFIG",
    "BASELINE_EXTRACTION_CONFIG_DIGEST",
    "BASELINE_EXTRACTION_CONFIG_SCHEMA",
    "BASELINE_EXTRACTION_CONFIG_VERSION",
    "BASELINE_FIXTURE_SCHEMA",
    "BASELINE_ONTOLOGY",
    "BASELINE_ONTOLOGY_DIGEST",
    "BASELINE_ONTOLOGY_SCHEMA",
    "BASELINE_ONTOLOGY_VERSION",
    "ChunkFixture",
    "Chunk",
    "EntityFixture",
    "Entity",
    "EntityTypeMapping",
    "EvidenceReference",
    "EvidenceRef",
    "ExtractionConclusion",
    "ExtractionConfiguration",
    "FixtureValidationError",
    "FixtureValidationResult",
    "OntologyDefinition",
    "OntologyEntityType",
    "OntologyRelationType",
    "OntologyVersion",
    "OntologyVersionDefinition",
    "ProjectionConfigVersion",
    "ProjectionConfigVersionDefinition",
    "ProjectionFixture",
    "RelationFixture",
    "Relation",
    "RelationTypeMapping",
    "SourceRevisionFixture",
    "SourceRevision",
    "StableLocator",
    "build_baseline_fixtures",
    "validate_fixture",
]
