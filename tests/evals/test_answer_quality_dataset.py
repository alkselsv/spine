from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from spine.evals.answer_quality_dataset import (
    APPROVED_ANSWER_QUALITY_DATASET,
    AnswerQualityDatasetIdentity,
    AnswerQualityDatasetValidationError,
    answer_quality_observation_input,
    load_answer_quality_dataset,
)


def test_answer_quality_dataset_identity_pins_user_approved_population() -> None:
    assert APPROVED_ANSWER_QUALITY_DATASET.model_dump(mode="json") == {
        "version": "0.4.0-user-approved-answer-quality",
        "sha256": "76d1e58241629c220e6bb8a5d532baf0901400a3f8b220aeec76e99c4e0aba01",
        "source_manifest_sha256": "376739b6fd8d4934edbfd1bcc9e2f2a44a58f65dcf452e91f7229e732921d168",
        "case_count": 197,
        "source_bound_case_count": 186,
        "unscoped_case_count": 11,
        "unique_document_count": 28,
    }


def _write_dataset(path: Path) -> str:
    payload = [
        {
            "id": 1,
            "document": ["policy.txt"],
            "question": "What is the approved period?",
            "answer": "The approved period is three days.",
        },
        {
            "id": 2,
            "document": [],
            "question": "Summarize the policy.",
            "answer": "The policy defines the approved period.",
        },
    ]
    encoded = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    path.write_bytes(encoded)
    return hashlib.sha256(encoded).hexdigest()


def _identity(digest: str) -> AnswerQualityDatasetIdentity:
    return AnswerQualityDatasetIdentity(
        version="test-answer-quality",
        sha256=digest,
        source_manifest_sha256="1" * 64,
        case_count=2,
        source_bound_case_count=1,
        unscoped_case_count=1,
        unique_document_count=1,
    )


def _write_source_manifest(path: Path) -> str:
    source_digest = hashlib.sha256("Approved source.".encode("utf-8")).hexdigest()
    payload = {
        "schema_version": "answer-quality-source-manifest:v1",
        "documents": [
            {
                "file_name": "policy.txt",
                "source_id": "source-policy",
                "source_revision_id": "revision-policy-v1",
                "original_sha256": source_digest,
            }
        ],
    }
    encoded = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    path.write_bytes(encoded)
    return hashlib.sha256(encoded).hexdigest()


def test_approved_answer_quality_dataset_loads_with_content_free_report(
    tmp_path: Path,
) -> None:
    dataset_path = tmp_path / "golden_dataset.json"
    digest = _write_dataset(dataset_path)
    source_root = tmp_path / "incoming"
    source_root.mkdir()
    (source_root / "policy.txt").write_text("Approved source.", encoding="utf-8")
    manifest_path = tmp_path / "source_manifest.json"
    manifest_digest = _write_source_manifest(manifest_path)

    loaded = load_answer_quality_dataset(
        dataset_path,
        source_root=source_root,
        source_manifest_path=manifest_path,
        identity=_identity(digest).model_copy(
            update={"source_manifest_sha256": manifest_digest}
        ),
    )

    assert loaded.validation_report.model_dump(mode="json") == {
        "schema_version": "answer-quality-dataset-validation:v1",
        "validation_status": "passed",
        "dataset_version": "test-answer-quality",
        "dataset_sha256": digest,
        "case_count": 2,
        "source_bound_case_count": 1,
        "unscoped_case_count": 1,
        "unique_document_count": 1,
        "source_files_verified": 1,
    }
    assert loaded.dataset is not None
    assert tuple(case.case_id for case in loaded.dataset.cases) == ("1", "2")
    assert loaded.dataset.cases[0].source_ids == ("source-policy",)
    assert loaded.dataset.cases[0].source_revision_ids == ("revision-policy-v1",)
    assert loaded.dataset.cases[1].source_ids == ()
    assert loaded.dataset.cases[1].retrieval_source_ids == ("source-policy",)
    assert loaded.dataset.cases[1].retrieval_source_revision_ids == (
        "revision-policy-v1",
    )

    observation_input = answer_quality_observation_input(
        loaded.dataset.cases[0],
        dataset=loaded.dataset,
    )

    assert observation_input.model_dump(mode="json") == {
        "schema_version": "r1-real-case-observation-input:v1",
        "dataset_version": "test-answer-quality",
        "dataset_sha256": digest,
        "case_id": "1",
        "question": "What is the approved period?",
        "language": "ru",
        "source_ids": ["source-policy"],
        "source_revision_ids": ["revision-policy-v1"],
        "available_source_revision_ids": ["revision-policy-v1"],
    }
    assert "expected_answer" not in observation_input.model_dump()
    assert "document_refs" not in observation_input.model_dump()


def test_answer_quality_dataset_rejects_content_changed_after_approval(
    tmp_path: Path,
) -> None:
    dataset_path = tmp_path / "golden_dataset.json"
    digest = _write_dataset(dataset_path)
    source_root = tmp_path / "incoming"
    source_root.mkdir()
    (source_root / "policy.txt").write_text("Approved source.", encoding="utf-8")
    manifest_path = tmp_path / "source_manifest.json"
    manifest_digest = _write_source_manifest(manifest_path)
    dataset_path.write_text("[]\n", encoding="utf-8")

    with pytest.raises(
        AnswerQualityDatasetValidationError,
        match="dataset digest does not match approved identity",
    ):
        load_answer_quality_dataset(
            dataset_path,
            source_root=source_root,
            source_manifest_path=manifest_path,
            identity=_identity(digest).model_copy(
                update={"source_manifest_sha256": manifest_digest}
            ),
        )


def test_answer_quality_dataset_rejects_source_symlink_outside_approved_root(
    tmp_path: Path,
) -> None:
    dataset_path = tmp_path / "golden_dataset.json"
    digest = _write_dataset(dataset_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("Approved source.", encoding="utf-8")
    source_root = tmp_path / "incoming"
    source_root.mkdir()
    (source_root / "policy.txt").symlink_to(outside)
    manifest_path = tmp_path / "source_manifest.json"
    manifest_digest = _write_source_manifest(manifest_path)

    with pytest.raises(
        AnswerQualityDatasetValidationError,
        match="source manifest does not match source root",
    ):
        load_answer_quality_dataset(
            dataset_path,
            source_root=source_root,
            source_manifest_path=manifest_path,
            identity=_identity(digest).model_copy(
                update={"source_manifest_sha256": manifest_digest}
            ),
        )
