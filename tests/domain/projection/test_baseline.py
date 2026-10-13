from __future__ import annotations

from copy import deepcopy
from uuid import UUID

import pytest

from spine.memory.cognee.models.baseline import (
    BASELINE_EXTRACTION_CONFIG,
    BASELINE_ONTOLOGY,
    FixtureValidationError,
    FixtureValidationResult,
    build_baseline_fixtures,
    validate_fixture,
)


def test_baseline_definitions_have_stable_versioned_digests() -> None:
    assert BASELINE_ONTOLOGY.version == "r1"
    assert BASELINE_ONTOLOGY.digest == (
        "90b2bf6ad1835c48d1ee46b250c47feabeb1ecb7d9e5bb049da2fccc4422ecee"
    )
    assert BASELINE_EXTRACTION_CONFIG.ontology_digest == BASELINE_ONTOLOGY.digest
    assert BASELINE_EXTRACTION_CONFIG.digest == (
        "e90df3e1eb73c4a8a722dd393ecdf21d3a88c1c3e852f65262efa23998f4202a"
    )


def test_positive_fixture_links_entities_and_relations_to_current_revision_evidence() -> None:
    fixture = build_baseline_fixtures()["positive"]

    result = validate_fixture(fixture)

    assert isinstance(result, FixtureValidationResult)
    assert result.accepted_entity_ids == ("entity-acme", "entity-owner")
    assert result.accepted_relation_ids == ("relation-owner",)
    assert fixture.conclusions[0].proposed_entity_type == "secret_project"
    assert "conclusion-unpromoted" not in result.accepted_entity_ids


@pytest.mark.parametrize(
    ("fixture_name", "error_code"),
    [
        ("forbidden", "forbidden_entity_type"),
        ("invalid_relation", "invalid_relation"),
        ("missing_evidence", "missing_evidence"),
        ("ambiguous_evidence", "ambiguous_evidence"),
        ("ambiguous_mapping", "ambiguous_mapping"),
        ("cross_revision_evidence", "cross_revision_evidence"),
        ("stale_revision", "stale_revision"),
        ("tombstone_revision", "tombstone_revision"),
    ],
)
def test_invalid_fixture_is_rejected_with_safe_reason(
    fixture_name: str, error_code: str
) -> None:
    fixture = build_baseline_fixtures()[fixture_name]

    with pytest.raises(FixtureValidationError) as error:
        validate_fixture(fixture)

    assert error.value.code == error_code
    assert error.value.case_id == fixture.case_id
    assert error.value.protected_content is None


def test_baseline_fixtures_are_immutable_and_reproducible() -> None:
    first = build_baseline_fixtures()
    second = build_baseline_fixtures()

    assert first.keys() == second.keys()
    assert {
        name: fixture.model_dump(mode="json")
        for name, fixture in first.items()
    } == {
        name: fixture.model_dump(mode="json")
        for name, fixture in second.items()
    }
    with pytest.raises((TypeError, ValueError)):
        first["positive"].case_id = "changed"  # type: ignore[misc]


def test_validator_does_not_accept_cross_revision_relation_evidence() -> None:
    fixture = deepcopy(build_baseline_fixtures()["positive"].model_dump(mode="python"))
    fixture["relations"][0]["evidence"][0]["source_revision_id"] = fixture[
        "source_revisions"
    ][1]["source_revision_id"]

    with pytest.raises(FixtureValidationError) as error:
        validate_fixture(type(build_baseline_fixtures()["positive"]).model_validate(fixture))

    assert error.value.code == "cross_revision_evidence"


def test_fixture_validator_rejects_mixed_workspace_scope() -> None:
    fixture = build_baseline_fixtures()["positive"]
    values = fixture.model_dump(mode="python")
    values["source_revisions"][1]["workspace_id"] = UUID(
        "77777777-7777-4777-8777-777777777777"
    )
    mixed_scope = type(fixture).model_validate(values)

    with pytest.raises(FixtureValidationError) as error:
        validate_fixture(mixed_scope)

    assert error.value.code == "cross_scope"
