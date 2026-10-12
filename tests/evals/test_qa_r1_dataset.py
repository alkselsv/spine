from __future__ import annotations

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


def test_r1_dataset_is_synthetic_and_does_not_define_release_thresholds() -> None:
    dataset = load_r1_dataset()

    assert all(case.synthetic for case in dataset.cases)
    assert dataset.release_thresholds == ()
    assert all("threshold" not in assumption.lower() for assumption in dataset.assumptions)
