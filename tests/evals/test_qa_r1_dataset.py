from __future__ import annotations

from pathlib import Path

import pytest

from spine.evals.qa_r1 import (
    QA_R1_SCHEMA_VERSION,
    QADataset,
    load_r1_dataset,
    serialize_dataset,
)


def test_r1_dataset_manifest_is_ordered_for_replay() -> None:
    dataset = load_r1_dataset()

    assert [case.case_id for case in dataset.cases] == [
        "allowed-acl",
        "absent-evidence-abstention",
        "changed-revision",
        "denied-acl",
        "duplicate-ingestion",
        "mixed-acl",
        "multi-document-multi-hop",
        "multi-chunk-synthesis",
        "pdf-single-page",
        "russian-xlsx",
        "workspace-isolation",
        "tombstone",
    ]


def test_r1_dataset_has_immutable_identity_and_stable_digest() -> None:
    dataset = load_r1_dataset()

    assert isinstance(dataset, QADataset)
    assert dataset.dataset_id == "qa-r1-synthetic"
    assert dataset.version == "1.0.0"
    assert dataset.schema_version == QA_R1_SCHEMA_VERSION
    assert len(dataset.dataset_digest) == 64
    assert serialize_dataset(dataset) == serialize_dataset(load_r1_dataset())

    with pytest.raises((TypeError, ValueError)):
        dataset.version = "2.0.0"


def test_r1_dataset_cases_cover_required_scenarios() -> None:
    dataset = load_r1_dataset()
    tags = {tag for case in dataset.cases for tag in case.scenario_tags}

    assert {
        "single_page_fact",
        "pdf_table",
        "xlsx_table",
        "multi_chunk_synthesis",
        "multi_document",
        "multi_hop",
        "changed_revision",
        "tombstone",
        "duplicate_ingestion",
        "absent_evidence",
        "allowed_acl",
        "denied_acl",
        "mixed_acl",
        "workspace_isolation",
        "russian",
        "mixed_language",
    } <= tags


def test_r1_dataset_evidence_and_workspace_references_resolve() -> None:
    dataset = load_r1_dataset()
    revisions = {revision.revision_id: revision for revision in dataset.source_revisions}
    evidence = {
        item.evidence_id: (revision, item)
        for revision in dataset.source_revisions
        for item in revision.evidence
    }

    for case in dataset.cases:
        assert case.workspace_id != case.environment_id
        for revision_id in case.available_revision_ids:
            revision = revisions[revision_id]
            assert revision.workspace_id == case.workspace_id
            assert revision.environment_id == case.environment_id
        for evidence_id in case.expected_evidence_ids:
            revision, item = evidence[evidence_id]
            assert revision.workspace_id == case.workspace_id
            assert revision.environment_id == case.environment_id
            assert item.source_revision_id == revision.revision_id

        for revision_id in case.excluded_revision_ids:
            excluded = revisions[revision_id]
            assert revision_id not in case.available_revision_ids
            assert (excluded.workspace_id, excluded.environment_id) != (
                case.workspace_id,
                case.environment_id,
            )
        for label in case.relevance_labels:
            revision, item = evidence[label.evidence_id]
            assert revision.revision_id in case.available_revision_ids
            assert revision.workspace_id == case.workspace_id
            assert revision.environment_id == case.environment_id
            assert item.source_revision_id == revision.revision_id

    assert len({case.workspace_id for case in dataset.cases}) >= 2


def test_r1_dataset_has_citation_grade_pdf_and_xlsx_locators() -> None:
    dataset = load_r1_dataset()
    locators = [
        item.locator
        for revision in dataset.source_revisions
        for item in revision.evidence
    ]

    assert any(locator.kind == "pdf_page" for locator in locators)
    assert any(locator.kind == "xlsx_cell" for locator in locators)
    assert all(locator.schema_version for locator in locators)

    for revision in dataset.source_revisions:
        if revision.content is None:
            continue
        for item in revision.evidence:
            if item.locator.kind == "text_range":
                assert revision.content[item.locator.start : item.locator.end] == item.excerpt


def test_r1_dataset_records_idempotent_duplicate_observations() -> None:
    dataset = load_r1_dataset()
    observations = {item.observation_id: item for item in dataset.observations}
    duplicate_ids = next(case.observation_ids for case in dataset.cases if "duplicate_ingestion" in case.scenario_tags)
    duplicate_observations = [observations[item] for item in duplicate_ids]

    assert len(duplicate_observations) == 2
    assert {item.source_revision_id for item in duplicate_observations} == {
        duplicate_observations[0].source_revision_id
    }
    assert len({item.idempotency_key for item in duplicate_observations}) == 1
    assert len({item.observed_digest for item in duplicate_observations}) == 1


def test_r1_dataset_keeps_tombstone_predecessor_history() -> None:
    dataset = load_r1_dataset()
    revisions_by_object: dict[object, list[object]] = {}
    for revision in dataset.source_revisions:
        revisions_by_object.setdefault(revision.source_object_id, []).append(revision)

    tombstones = [revision for revision in dataset.source_revisions if revision.kind == "tombstone"]
    assert tombstones
    for tombstone in tombstones:
        assert any(revision.kind == "content" for revision in revisions_by_object[tombstone.source_object_id])


def test_r1_dataset_serialization_is_stable_from_independent_loads() -> None:
    path = Path(__file__).parents[2] / "src" / "spine" / "evals" / "data" / "qa_r1.json"
    first = QADataset.model_validate_json(path.read_bytes())
    second = QADataset.model_validate_json(path.read_bytes())

    assert serialize_dataset(first) == serialize_dataset(second)


def test_r1_dataset_is_synthetic_and_does_not_define_release_thresholds() -> None:
    dataset = load_r1_dataset()

    assert all(case.synthetic for case in dataset.cases)
    assert dataset.release_thresholds == ()
    assert all("threshold" not in assumption.lower() for assumption in dataset.assumptions)
