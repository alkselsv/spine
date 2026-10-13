from __future__ import annotations

from uuid import UUID

import pytest

from spine.memory.cognee.extraction import (
    EntityCandidate,
    ExtractionBatch,
    ExtractionStatus,
    RelationCandidate,
    extract_projection,
)
from spine.memory.cognee.models.baseline import (
    BASELINE_EXTRACTION_CONFIG,
    BASELINE_ONTOLOGY,
    ExtractionConclusion,
    build_baseline_fixtures,
)


WORKSPACE_ID = UUID("11111111-1111-4111-8111-111111111111")
ENVIRONMENT_ID = UUID("22222222-2222-4222-8222-222222222222")


def extraction_batch(
    *,
    entities: tuple[EntityCandidate, ...],
    relations: tuple[RelationCandidate, ...] = (),
    conclusions: tuple[ExtractionConclusion, ...] = (),
    source_revisions=None,
    chunks=None,
) -> ExtractionBatch:
    fixture = build_baseline_fixtures()["positive"]
    return ExtractionBatch(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        ontology=BASELINE_ONTOLOGY,
        extraction_config=BASELINE_EXTRACTION_CONFIG,
        source_revisions=fixture.source_revisions if source_revisions is None else source_revisions,
        chunks=fixture.chunks if chunks is None else chunks,
        entities=entities,
        relations=relations,
        conclusions=conclusions,
    )


def test_extract_projection_maps_versioned_entities_and_relations_with_lineage() -> None:
    fixture = build_baseline_fixtures()["positive"]
    current_revision_id = fixture.source_revisions[0].source_revision_id
    batch = extraction_batch(
        entities=(
            EntityCandidate(
                candidate_id="entity-acme",
                source_label="organization",
                canonical_name="Acme",
                source_revision_id=current_revision_id,
                chunk_ids=("chunk-current",),
                evidence=(fixture.entities[0].evidence[0],),
            ),
            EntityCandidate(
                candidate_id="entity-owner",
                source_label="person",
                canonical_name="Ada",
                source_revision_id=current_revision_id,
                chunk_ids=("chunk-current",),
                evidence=(fixture.entities[1].evidence[0],),
            ),
        ),
        relations=(
            RelationCandidate(
                candidate_id="relation-owner",
                source_label="works_for",
                source_revision_id=current_revision_id,
                source_entity_candidate_id="entity-owner",
                target_entity_candidate_id="entity-acme",
                evidence=(fixture.relations[0].evidence[0],),
            ),
        ),
    )

    result = extract_projection(batch)

    assert result.status is ExtractionStatus.ACCEPTED
    assert result.ontology_version == "r1"
    assert result.extraction_config_version == "r1"
    assert [entity.entity_id for entity in result.entities] == [
        "entity-acme",
        "entity-owner",
    ]
    assert result.entities[0].ontology_digest == BASELINE_ONTOLOGY.digest
    assert result.entities[0].extraction_config_digest == BASELINE_EXTRACTION_CONFIG.digest
    assert result.entities[0].source_revision_id == current_revision_id
    assert result.entities[0].chunk_ids == ("chunk-current",)
    assert result.entities[0].evidence[0].evidence_id == "evidence-acme"
    assert result.relations[0].relation_type == "works_for"
    assert result.relations[0].subject_entity_id == "entity-owner"
    assert result.relations[0].object_entity_id == "entity-acme"
    assert result.rejections == ()


def test_extract_projection_keeps_model_conclusions_out_of_accepted_facts() -> None:
    fixture = build_baseline_fixtures()["positive"]
    current_revision_id = fixture.source_revisions[0].source_revision_id
    conclusion = fixture.conclusions[0]
    batch = extraction_batch(
        entities=(),
        conclusions=(conclusion,),
    )

    result = extract_projection(batch)

    assert result.entities == ()
    assert result.relations == ()
    assert result.conclusions == (conclusion,)
    assert result.status is ExtractionStatus.ACCEPTED
    assert current_revision_id == conclusion.source_revision_id


def test_extract_projection_rejects_forbidden_entity_and_relation_types() -> None:
    fixture = build_baseline_fixtures()["positive"]
    current_revision_id = fixture.source_revisions[0].source_revision_id
    valid_entity = EntityCandidate(
        candidate_id="entity-acme",
        source_label="organization",
        canonical_name="Acme",
        source_revision_id=current_revision_id,
        chunk_ids=("chunk-current",),
        evidence=(fixture.entities[0].evidence[0],),
    )
    forbidden_entity = valid_entity.model_copy(
        update={"candidate_id": "entity-secret", "source_label": "secret_project"}
    )
    forbidden_relation = RelationCandidate(
        candidate_id="relation-forbidden",
        source_label="owns",
        source_revision_id=current_revision_id,
        source_entity_candidate_id="entity-acme",
        target_entity_candidate_id="entity-acme",
        evidence=(fixture.relations[0].evidence[0],),
    )

    result = extract_projection(
        extraction_batch(
            entities=(valid_entity, forbidden_entity),
            relations=(forbidden_relation,),
        )
    )

    assert result.status is ExtractionStatus.DEGRADED
    assert [entity.entity_id for entity in result.entities] == ["entity-acme"]
    assert result.relations == ()
    assert {(item.candidate_id, item.code) for item in result.rejections} == {
        ("entity-secret", "forbidden_entity_type"),
        ("relation-forbidden", "forbidden_relation_type"),
    }
    assert {
        (item.workspace_id, item.environment_id)
        for item in result.rejections
    } == {(WORKSPACE_ID, ENVIRONMENT_ID)}


def test_extract_projection_fails_closed_for_cross_revision_evidence() -> None:
    fixture = build_baseline_fixtures()["positive"]
    current_revision_id = fixture.source_revisions[0].source_revision_id
    stale_revision_id = fixture.source_revisions[1].source_revision_id
    candidate = EntityCandidate(
        candidate_id="entity-acme",
        source_label="organization",
        canonical_name="Acme",
        source_revision_id=current_revision_id,
        chunk_ids=("chunk-current",),
        evidence=(fixture.entities[0].evidence[0].model_copy(
            update={"source_revision_id": stale_revision_id}
        ),),
    )

    result = extract_projection(extraction_batch(entities=(candidate,)))

    assert result.status is ExtractionStatus.REJECTED
    assert result.entities == ()
    assert result.rejections[0].code == "cross_revision_evidence"
    assert result.rejections[0].source_revision_id == current_revision_id


def test_extract_projection_deduplicates_identical_candidates_deterministically() -> None:
    fixture = build_baseline_fixtures()["positive"]
    current_revision_id = fixture.source_revisions[0].source_revision_id
    candidate = EntityCandidate(
        candidate_id="entity-acme",
        source_label="organization",
        canonical_name="Acme",
        source_revision_id=current_revision_id,
        chunk_ids=("chunk-current",),
        evidence=(fixture.entities[0].evidence[0],),
    )
    batch = extraction_batch(entities=(candidate, candidate))

    first = extract_projection(batch)
    second = extract_projection(batch)

    assert first == second
    assert first.result_digest == second.result_digest
    assert [entity.entity_id for entity in first.entities] == ["entity-acme"]
    assert first.rejections == ()


def test_extract_projection_reports_conflicting_duplicate_candidates() -> None:
    fixture = build_baseline_fixtures()["positive"]
    current_revision_id = fixture.source_revisions[0].source_revision_id
    first = EntityCandidate(
        candidate_id="entity-acme",
        source_label="organization",
        canonical_name="Acme",
        source_revision_id=current_revision_id,
        chunk_ids=("chunk-current",),
        evidence=(fixture.entities[0].evidence[0],),
    )
    conflicting = first.model_copy(update={"canonical_name": "Other Acme"})

    result = extract_projection(extraction_batch(entities=(first, conflicting)))

    assert result.status is ExtractionStatus.REJECTED
    assert result.entities == ()
    assert result.rejections[0].code == "duplicate_candidate"


@pytest.mark.parametrize(
    ("source_label", "expected_code"),
    [
        ("company", "ambiguous_mapping"),
        ("unknown_type", "forbidden_entity_type"),
    ],
)
def test_extract_projection_rejects_ambiguous_or_unknown_entity_mappings(
    source_label: str, expected_code: str
) -> None:
    fixture = build_baseline_fixtures()["positive"]
    candidate = EntityCandidate(
        candidate_id="entity-candidate",
        source_label=source_label,
        canonical_name="Candidate",
        source_revision_id=fixture.source_revisions[0].source_revision_id,
        chunk_ids=("chunk-current",),
        evidence=(fixture.entities[0].evidence[0],),
    )

    result = extract_projection(extraction_batch(entities=(candidate,)))

    assert result.status is ExtractionStatus.REJECTED
    assert result.entities == ()
    assert result.rejections[0].code == expected_code


def test_extract_projection_rejects_relation_with_wrong_typed_endpoints() -> None:
    fixture = build_baseline_fixtures()["positive"]
    revision_id = fixture.source_revisions[0].source_revision_id
    organization = EntityCandidate(
        candidate_id="entity-acme",
        source_label="organization",
        canonical_name="Acme",
        source_revision_id=revision_id,
        chunk_ids=("chunk-current",),
        evidence=(fixture.entities[0].evidence[0],),
    )
    person = EntityCandidate(
        candidate_id="entity-owner",
        source_label="person",
        canonical_name="Ada",
        source_revision_id=revision_id,
        chunk_ids=("chunk-current",),
        evidence=(fixture.entities[1].evidence[0],),
    )
    reversed_relation = RelationCandidate(
        candidate_id="relation-invalid",
        source_label="works_for",
        source_revision_id=revision_id,
        source_entity_candidate_id="entity-acme",
        target_entity_candidate_id="entity-owner",
        evidence=(fixture.relations[0].evidence[0],),
    )

    result = extract_projection(
        extraction_batch(entities=(organization, person), relations=(reversed_relation,))
    )

    assert result.status is ExtractionStatus.DEGRADED
    assert len(result.entities) == 2
    assert result.relations == ()
    assert result.rejections[0].code == "invalid_relation"


@pytest.mark.parametrize(
    ("evidence_update", "expected_code"),
    [
        ({"quality": "ambiguous"}, "ambiguous_evidence"),
        ({"quality": "missing"}, "missing_evidence"),
    ],
)
def test_extract_projection_rejects_ambiguous_or_missing_evidence(
    evidence_update: dict[str, str], expected_code: str
) -> None:
    fixture = build_baseline_fixtures()["positive"]
    candidate = EntityCandidate(
        candidate_id="entity-acme",
        source_label="organization",
        canonical_name="Acme",
        source_revision_id=fixture.source_revisions[0].source_revision_id,
        chunk_ids=("chunk-current",),
        evidence=(fixture.entities[0].evidence[0].model_copy(update=evidence_update),),
    )

    result = extract_projection(extraction_batch(entities=(candidate,)))

    assert result.status is ExtractionStatus.REJECTED
    assert result.rejections[0].code == expected_code


def test_extract_projection_rejects_stale_and_tombstoned_source_revisions() -> None:
    fixtures = build_baseline_fixtures()
    positive = fixtures["positive"]
    stale_id = positive.source_revisions[1].source_revision_id
    stale_candidate = EntityCandidate(
        candidate_id="entity-stale",
        source_label="organization",
        canonical_name="Old Acme",
        source_revision_id=stale_id,
        chunk_ids=("chunk-current",),
        evidence=(positive.entities[0].evidence[0],),
    )
    tombstone_fixture = fixtures["tombstone_revision"]
    tombstone_id = tombstone_fixture.source_revisions[1].source_revision_id
    tombstone_candidate = EntityCandidate(
        candidate_id="entity-deleted",
        source_label="organization",
        canonical_name="Deleted Acme",
        source_revision_id=tombstone_id,
        chunk_ids=("chunk-current",),
        evidence=(positive.entities[0].evidence[0],),
    )

    stale_result = extract_projection(extraction_batch(entities=(stale_candidate,)))
    tombstone_result = extract_projection(
        extraction_batch(
            entities=(tombstone_candidate,),
            source_revisions=tombstone_fixture.source_revisions,
            chunks=tombstone_fixture.chunks,
        )
    )

    assert stale_result.rejections[0].code == "stale_revision"
    assert tombstone_result.rejections[0].code == "tombstone_revision"


def test_extract_projection_rejects_an_evidence_less_referenced_chunk() -> None:
    fixture = build_baseline_fixtures()["positive"]
    candidate = EntityCandidate(
        candidate_id="entity-acme",
        source_label="organization",
        canonical_name="Acme",
        source_revision_id=fixture.source_revisions[0].source_revision_id,
        chunk_ids=("chunk-current",),
        evidence=(fixture.entities[0].evidence[0],),
    )
    chunk_without_evidence = fixture.chunks[0].model_copy(update={"evidence": ()})

    result = extract_projection(
        extraction_batch(entities=(candidate,), chunks=(chunk_without_evidence,))
    )

    assert result.status is ExtractionStatus.REJECTED
    assert result.entities == ()
    assert result.rejections[0].code == "missing_evidence"


def test_extract_projection_rejects_evidence_less_model_conclusions() -> None:
    fixture = build_baseline_fixtures()["positive"]
    conclusion = fixture.conclusions[0].model_copy(update={"evidence": ()})

    result = extract_projection(extraction_batch(entities=(), conclusions=(conclusion,)))

    assert result.status is ExtractionStatus.REJECTED
    assert result.conclusions == ()
    assert result.rejections[0].kind.value == "conclusion"
    assert result.rejections[0].code == "missing_evidence"


def test_extract_projection_isolates_a_candidate_from_another_workspace() -> None:
    fixtures = build_baseline_fixtures()
    positive = fixtures["positive"]
    foreign_values = positive.source_revisions[1].model_dump(mode="python")
    foreign_values["workspace_id"] = UUID("77777777-7777-4777-8777-777777777777")
    foreign_revision = type(positive.source_revisions[1]).model_validate(foreign_values)
    candidate = EntityCandidate(
        candidate_id="entity-foreign",
        source_label="organization",
        canonical_name="Foreign Acme",
        source_revision_id=foreign_revision.source_revision_id,
        chunk_ids=("chunk-current",),
        evidence=(positive.entities[0].evidence[0],),
    )

    result = extract_projection(
        extraction_batch(
            entities=(candidate,),
            source_revisions=(positive.source_revisions[0], foreign_revision),
        )
    )

    assert result.status is ExtractionStatus.REJECTED
    assert result.entities == ()
    assert result.rejections[0].code == "cross_scope"


def test_extract_projection_rejects_configuration_pinned_to_another_ontology_version() -> None:
    fixture = build_baseline_fixtures()["positive"]
    config_values = BASELINE_EXTRACTION_CONFIG.model_dump(mode="python")
    config_values.update({"ontology_version": "other", "digest": ""})
    config = type(BASELINE_EXTRACTION_CONFIG).model_validate(config_values)

    result = extract_projection(
        ExtractionBatch(
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
            ontology=BASELINE_ONTOLOGY,
            extraction_config=config,
            source_revisions=fixture.source_revisions,
            chunks=fixture.chunks,
        )
    )

    assert result.status is ExtractionStatus.REJECTED
    assert result.rejections[0].code == "configuration_ontology_version_mismatch"
