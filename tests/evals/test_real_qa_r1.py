from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

import spine.evals.real_qa_r1 as real_qa
from spine.evals.qa_r1 import load_r1_dataset


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _write_fixture(
    root: Path, *, xlsx_excerpt: str = "42", xlsx_row_text: str | None = None
) -> tuple[Path, Path, dict[str, Any]]:
    dataset_dir = root / "r1_gold_draft"
    source_root = root / "incoming"
    anon_root = dataset_dir / "anonymized"
    dataset_dir.mkdir(parents=True)
    source_root.mkdir()
    anon_root.mkdir()

    source_facts = (
        ("src-docx", "rev-docx", "source-a.docx", "docx", "One verified fact."),
        ("src-pdf", "rev-pdf", "source-b.pdf", "pdf", "Second evidence excerpt."),
        ("src-xlsx", "rev-xlsx", "source-c.xlsx", "xlsx", xlsx_excerpt),
    )
    source_objects = []
    source_revisions = []
    manifest_documents = []
    evidence_by_id: dict[str, dict[str, Any]] = {}
    for source_id, revision_id, filename, format_name, excerpt in source_facts:
        original = f"safe {filename} fixture".encode("utf-8")
        (source_root / filename).write_bytes(original)
        source_sha = hashlib.sha256(original).hexdigest()
        if format_name == "docx":
            unit = {"kind": "docx_paragraph", "paragraph": 4, "heading": "Fixture", "text": excerpt}
            locator = {
                "kind": "docx_paragraph", "schema_version": "locator:docx-paragraph:v1",
                "paragraph": 4, "char_start": 0, "char_end": len(excerpt), "heading": "Fixture",
            }
        elif format_name == "pdf":
            unit = {"kind": "pdf_page", "page": 2, "text": excerpt}
            locator = {
                "kind": "pdf_page", "schema_version": "locator:pdf-page-char:v1",
                "page": 2, "char_start": 0, "char_end": len(excerpt),
            }
        else:
            unit = {"kind": "xlsx_row", "sheet": "Sheet1", "row": 1, "text": xlsx_row_text or excerpt,
                    "cells": [{"cell": "A1", "value": "Heading"}, {"cell": "B1", "value": 42}]}
            locator = {
                "kind": "xlsx_cell_range", "schema_version": "locator:xlsx-cell:v1",
                "sheet": "Sheet1", "row": 1, "cell_range": "B1",
            }
        anon = {
            "source_id": source_id, "revision_id": revision_id, "sha256": source_sha,
            "path": filename, "units": [unit],
        }
        anon_bytes = json.dumps(anon, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
        (anon_root / f"{source_id}.json").write_bytes(anon_bytes)
        anon_sha = hashlib.sha256(anon_bytes).hexdigest()
        source_objects.append({
            "source_id": source_id, "source_kind": "document", "display_title": f"Fixture {source_id}",
            "format": format_name, "inventory_ref": source_id,
            "authorization_status": "APPROVED_BY_USER_FOR_LOCAL_DATASET",
            "privacy_review_status": "APPROVED_BY_USER", "anonymization_status": "USER_CONFIRMED_PERFORMED",
        })
        evidence_id = f"ev-{format_name}"
        item = {
            "evidence_id": evidence_id, "source_id": source_id, "source_revision_id": revision_id,
            "excerpt": excerpt, "locator": locator, "redaction_applied": False,
        }
        evidence_by_id[evidence_id] = item
        source_revisions.append({
            "source_id": source_id, "source_revision_id": revision_id, "revision_number": 1,
            "revision_schema_version": "source-revision:v1", "source_sha256": source_sha,
            "revision_digest": source_sha, "anonymized_representation_sha256": anon_sha,
            "media_type": "application/octet-stream", "extraction_status": "extracted",
            "extraction_unit_count": 1, "redaction_status": "automated_candidate", "evidence": [item],
        })
        manifest_documents.append({
            "source_id": source_id, "path": filename, "format": format_name,
            "revision_id": revision_id, "sha256": source_sha,
            "anonymized_sha256": anon_sha, "anonymized_representation_sha256": anon_sha,
            "extraction_status": "extracted",
        })

    all_evidence = list(evidence_by_id)
    claims = [
        {"claim_id": "claim-first", "text": "First fixture claim.", "evidence_ids": ["ev-docx", "ev-pdf"]},
        {"claim_id": "claim-second", "text": "Second fixture claim.", "evidence_ids": ["ev-xlsx"]},
    ]
    citations = [evidence_by_id[evidence_id] for evidence_id in all_evidence]
    raw: dict[str, Any] = {
        "approval_status": "APPROVED_BY_USER", "assumptions": [],
        "authorization_status": "APPROVED_BY_USER_FOR_LOCAL_DATASET",
        "case_schema_version": "qa-case:v1", "cases": [{
            "case_id": "r1-real-fixture-case", "case_schema_version": "qa-case:v1",
            "question": "Which fixture facts are supported?", "language": "ru",
            "expected_outcome": "answered", "expected_answer": "Safe fixture answer.",
            "golden_answer": "Safe fixture answer.", "expected_claims": claims,
            "expected_evidence_ids": all_evidence,
            "citations": citations,
            "source_ids": [item[0] for item in source_facts],
            "source_files": [item[2] for item in source_facts],
            "source_revision_ids": [item[1] for item in source_facts],
            "available_source_revision_ids": [item[1] for item in source_facts],
            "expected_reason": None, "question_type": "multi_chunk_synthesis", "difficulty": "hard",
            "scenario_tags": ["russian", "real_document"],
            "question_review_status": "approved_by_user", "review_status": "approved",
            "semantic_verification_status": "human_approved",
        }],
        "change_log": {}, "compatibility": {}, "created_by": "test-fixture",
        "dataset_id": "qa-r1-real-draft", "dataset_kind": "real_document_draft",
        "dataset_integrity_digest": "0" * 64, "dataset_status": "HUMAN_APPROVED_LOCAL_NOT_INTEGRATED",
        "generated_at_utc": "2026-10-17T00:00:00Z",
        "integrity_digest_algorithm": "sha256(canonical-json-without-dataset_integrity_digest)",
        "language": "ru", "privacy_review_status": "APPROVED_BY_USER",
        "question_approval": {"status": "approved_by_user"},
        "review_only_abstention_candidates": [], "schema_version": "qa-manifest:v1",
        "source_objects": source_objects, "source_revisions": source_revisions,
        "status": "HUMAN-APPROVED LOCAL DATASET — NOT INTEGRATED WITH EVALUATION HARNESS",
        "total_abstention_candidates": 0, "version": real_qa.REAL_R1_DATASET_VERSION,
        "answer_evidence_approval": {"status": "approved_by_user"},
        "anonymization_status": "USER_CONFIRMED_PERFORMED",
        "anonymization_confirmation": {"status": "USER_CONFIRMED_PERFORMED"},
        "authorization_approval": {"status": "APPROVED_BY_USER_FOR_LOCAL_DATASET"},
    }
    raw["dataset_integrity_digest"] = hashlib.sha256(_canonical({k: v for k, v in raw.items() if k != "dataset_integrity_digest"})).hexdigest()
    (dataset_dir / "golden_qa.json").write_bytes(json.dumps(raw, ensure_ascii=False, indent=2).encode("utf-8") + b"\n")
    manifest = {
        "schema_version": "r1-source-inventory:v1", "dataset_id": "r1-real-documents-draft",
        "dataset_version": raw["version"], "dataset_integrity_digest": raw["dataset_integrity_digest"],
        "source_count": len(manifest_documents), "documents_processed": len(manifest_documents),
        "source_root": "data/incoming/", "documents": manifest_documents,
        "total_question_candidates": 1, "total_questions_human_confirmed": 1,
        "total_answered_candidates": 1, "total_abstention_candidates": 0,
        "review_only_abstention_candidates": [], "privacy_review_status": "APPROVED_BY_USER",
    }
    (dataset_dir / "sources_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    columns = [
        "case_id", "question", "golden_answer", "expected_outcome", "question_type", "difficulty",
        "source_files", "source_revision_ids", "evidence_ids", "citations", "expected_reason",
        "scenario_tags", "question_review_status", "review_status", "dataset_version", "dataset_digest",
    ]
    case = raw["cases"][0]
    row = {
        "case_id": case["case_id"], "question": case["question"], "golden_answer": case["golden_answer"],
        "expected_outcome": case["expected_outcome"], "question_type": case["question_type"],
        "difficulty": case["difficulty"], "source_files": ";".join(case["source_files"]),
        "source_revision_ids": ";".join(case["source_revision_ids"]), "evidence_ids": ";".join(case["expected_evidence_ids"]),
        "citations": json.dumps(case["citations"], ensure_ascii=False, separators=(",", ":")),
        "expected_reason": "", "scenario_tags": ";".join(case["scenario_tags"]),
        "question_review_status": case["question_review_status"], "review_status": case["review_status"],
        "dataset_version": raw["version"], "dataset_digest": raw["dataset_integrity_digest"],
    }
    with (dataset_dir / "golden_qa.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerow(row)
    return dataset_dir, source_root, raw


def test_real_dataset_loader_preserves_claims_and_exact_locators(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dataset_dir, source_root, payload = _write_fixture(tmp_path)
    monkeypatch.setattr(real_qa, "REAL_R1_DATASET_DIGEST", payload["dataset_integrity_digest"])

    dataset = real_qa.load_real_r1_dataset(dataset_dir, source_root=source_root)

    assert dataset.dataset.dataset_id == "qa-r1-real-draft"
    assert dataset.dataset.version == real_qa.REAL_R1_DATASET_VERSION
    assert dataset.dataset.cases[0].synthetic is False
    assert len(dataset.dataset.cases[0].expected_claims) == 2
    assert len(dataset.dataset.cases[0].expected_evidence_ids) == 3
    assert {citation.locator.kind for citation in dataset.dataset.cases[0].citations} == {
        "docx_paragraph", "pdf_page", "xlsx_cell_range",
    }
    assert dataset.validation_report.source_files_verified == 3
    assert dataset.validation_report.evaluation_status == "not_run"
    assert dataset.validation_report.validator_version == real_qa.REAL_R1_VALIDATOR_VERSION
    assert dataset.validation_report.evidence_integrity_status == "passed"


def test_real_dataset_loader_rejects_digest_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dataset_dir, source_root, _ = _write_fixture(tmp_path)
    monkeypatch.setattr(real_qa, "REAL_R1_DATASET_DIGEST", "f" * 64)

    with pytest.raises(real_qa.RealDatasetValidationError, match="digest does not match"):
        real_qa.load_real_r1_dataset(dataset_dir, source_root=source_root)


def test_real_dataset_loader_rejects_missing_source_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dataset_dir, source_root, payload = _write_fixture(tmp_path)
    payload["cases"][0]["source_revision_ids"][0] = "rev-missing"
    payload["dataset_integrity_digest"] = hashlib.sha256(
        _canonical({k: v for k, v in payload.items() if k != "dataset_integrity_digest"})
    ).hexdigest()
    (dataset_dir / "golden_qa.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    manifest = json.loads((dataset_dir / "sources_manifest.json").read_text())
    manifest["dataset_integrity_digest"] = payload["dataset_integrity_digest"]
    (dataset_dir / "sources_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(real_qa, "REAL_R1_DATASET_DIGEST", payload["dataset_integrity_digest"])

    with pytest.raises(real_qa.RealDatasetValidationError, match="revision"):
        real_qa.load_real_r1_dataset(dataset_dir, source_root=source_root)


def test_real_dataset_loader_rejects_locator_excerpt_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dataset_dir, source_root, payload = _write_fixture(tmp_path)
    anon_path = dataset_dir / "anonymized" / "src-docx.json"
    anon = json.loads(anon_path.read_text(encoding="utf-8"))
    anon["units"][0]["text"] = "Different text."
    anon_bytes = json.dumps(anon, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    anon_path.write_bytes(anon_bytes)
    payload["source_revisions"][0]["anonymized_representation_sha256"] = hashlib.sha256(anon_bytes).hexdigest()
    for doc in json.loads((dataset_dir / "sources_manifest.json").read_text())["documents"]:
        if doc["source_id"] == "src-docx":
            doc["anonymized_sha256"] = hashlib.sha256(anon_bytes).hexdigest()
            doc["anonymized_representation_sha256"] = hashlib.sha256(anon_bytes).hexdigest()
    manifest = json.loads((dataset_dir / "sources_manifest.json").read_text())
    for doc in manifest["documents"]:
        if doc["source_id"] == "src-docx":
            doc["anonymized_sha256"] = hashlib.sha256(anon_bytes).hexdigest()
            doc["anonymized_representation_sha256"] = hashlib.sha256(anon_bytes).hexdigest()
    (dataset_dir / "sources_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False))
    payload["dataset_integrity_digest"] = hashlib.sha256(
        _canonical({k: v for k, v in payload.items() if k != "dataset_integrity_digest"})
    ).hexdigest()
    (dataset_dir / "golden_qa.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    manifest["dataset_integrity_digest"] = payload["dataset_integrity_digest"]
    (dataset_dir / "sources_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(real_qa, "REAL_R1_DATASET_DIGEST", payload["dataset_integrity_digest"])

    loaded = real_qa.load_real_r1_dataset(dataset_dir, source_root=source_root)

    assert loaded.dataset is None
    assert loaded.validation_report.validation_status == "blocked"
    assert loaded.validation_report.evidence_integrity_status == "failed"
    assert loaded.validation_report.locator_mismatch_count == 1
    assert loaded.validation_report.locator_mismatch_by_kind == {"docx_paragraph": 1}


def test_xlsx_locator_excerpt_must_come_from_exact_cell_range(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dataset_dir, source_root, payload = _write_fixture(
        tmp_path, xlsx_excerpt="Heading", xlsx_row_text="A1=Heading | B1=42"
    )
    monkeypatch.setattr(real_qa, "REAL_R1_DATASET_DIGEST", payload["dataset_integrity_digest"])
    loaded = real_qa.load_real_r1_dataset(dataset_dir, source_root=source_root)

    assert loaded.dataset is None
    assert loaded.validation_report.validation_status == "blocked"
    assert loaded.validation_report.locator_mismatch_by_kind == {"xlsx_cell_range": 1}


@pytest.mark.parametrize("representation", ["source", "anonymized"])
def test_real_dataset_loader_rejects_changed_source_representation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, representation: str
) -> None:
    dataset_dir, source_root, payload = _write_fixture(tmp_path)
    monkeypatch.setattr(real_qa, "REAL_R1_DATASET_DIGEST", payload["dataset_integrity_digest"])
    if representation == "source":
        target = source_root / "source-a.docx"
        target.write_bytes(target.read_bytes() + b" changed")
        expected_error = "source file digest"
    else:
        target = dataset_dir / "anonymized" / "src-docx.json"
        target.write_bytes(target.read_bytes() + b" ")
        expected_error = "anonymized source representation digest"

    with pytest.raises(real_qa.RealDatasetValidationError, match=expected_error):
        real_qa.load_real_r1_dataset(dataset_dir, source_root=source_root)


def test_real_dataset_loader_rejects_csv_metadata_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dataset_dir, source_root, payload = _write_fixture(tmp_path)
    monkeypatch.setattr(real_qa, "REAL_R1_DATASET_DIGEST", payload["dataset_integrity_digest"])
    csv_path = dataset_dir / "golden_qa.csv"
    csv_path.write_text(
        csv_path.read_text(encoding="utf-8-sig").replace("multi_chunk_synthesis", "fact_lookup"),
        encoding="utf-8-sig",
    )

    loaded = real_qa.load_real_r1_dataset(dataset_dir, source_root=source_root)

    assert loaded.dataset is None
    assert loaded.validation_report.validation_status == "blocked"
    assert loaded.validation_report.csv_consistency_status == "failed"
    assert loaded.validation_report.csv_mismatch_case_count == 1
    assert loaded.validation_report.csv_mismatch_fields == ("question_type",)
    assert loaded.validation_report.evidence_integrity_status == "passed"


def test_missing_private_dataset_fails_without_synthetic_fallback(tmp_path: Path) -> None:
    with pytest.raises(real_qa.RealDatasetUnavailableError):
        real_qa.load_real_r1_dataset(tmp_path / "missing", source_root=tmp_path)


def test_real_and_synthetic_dataset_contracts_remain_separate() -> None:
    dataset = load_r1_dataset()

    assert dataset.dataset_id == "qa-r1-synthetic"
    assert dataset.version == "1.0.0"
    assert all(case.synthetic is True for case in dataset.cases)
    assert real_qa.RealQACase is not type(dataset.cases[0])
