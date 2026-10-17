from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from spine.evals import dataset_cli
from spine.evals.qa_r1 import load_r1_dataset
from spine.evals.real_qa_r1 import RealDatasetUnavailableError, RealDatasetValidationReport


def test_dataset_cli_requires_explicit_kind() -> None:
    with pytest.raises(SystemExit):
        dataset_cli.main([])


def test_dataset_cli_real_missing_data_never_falls_back_to_synthetic(
    tmp_path, monkeypatch, capsys
) -> None:
    def unexpected_synthetic_load():
        raise AssertionError("real selection must never load the synthetic fixture")

    monkeypatch.setattr(dataset_cli, "load_r1_dataset", unexpected_synthetic_load)
    monkeypatch.setattr(dataset_cli, "load_real_r1_dataset", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RealDatasetUnavailableError("missing")
    ))

    result = dataset_cli.main([
        "--dataset-kind", "real", "--dataset-dir", str(tmp_path / "private"),
        "--source-root", str(tmp_path / "incoming"),
    ])

    assert result == 2
    assert "validation failed" in capsys.readouterr().err


def test_dataset_cli_synthetic_emits_metadata_only_report(capsys) -> None:
    result = dataset_cli.main(["--dataset-kind", "synthetic"])

    report = json.loads(capsys.readouterr().out)
    assert result == 0
    assert report["dataset_kind"] == "synthetic"
    assert report["dataset_id"] == load_r1_dataset().dataset_id
    assert report["evaluation_status"] == "not_run"
    assert report["answered_case_count"] + report["abstained_case_count"] + report["denied_case_count"] == report["case_count"]
    assert report["answered_case_count"] > 0
    assert report["validator_version"]
    assert "not an evaluation" in report["note"]


def test_dataset_cli_reports_csv_drift_and_returns_nonzero(tmp_path, monkeypatch, capsys) -> None:
    validation_report = RealDatasetValidationReport(
        validation_status="blocked",
        dataset_id="qa-r1-real-draft",
        version="0.3.6-user-approved",
        digest="d" * 64,
        validator_version="1.0.0",
        csv_consistency_status="failed",
        csv_mismatch_case_count=4,
        csv_mismatch_fields=("scenario_tags",),
        evidence_integrity_status="failed",
        locator_mismatch_count=53,
        locator_mismatch_by_kind={"xlsx_cell_range": 53},
        case_count=136,
        answered_case_count=136,
        abstained_case_count=0,
        denied_case_count=0,
        source_count=30,
        source_files_verified=30,
        evidence_count=171,
        claim_count=136,
    )
    monkeypatch.setattr(
        dataset_cli,
        "load_real_r1_dataset",
        lambda *_args, **_kwargs: SimpleNamespace(validation_report=validation_report),
    )

    result = dataset_cli.main([
        "--dataset-kind", "real", "--dataset-dir", str(tmp_path / "private"),
        "--source-root", str(tmp_path / "incoming"),
    ])

    report = json.loads(capsys.readouterr().out)
    assert result == 2
    assert report["validation_status"] == "blocked"
    assert report["csv_mismatch_case_count"] == 4
    assert report["csv_mismatch_fields"] == ["scenario_tags"]
    assert report["answered_case_count"] == 136
    assert report["abstained_case_count"] == 0
    assert report["denied_case_count"] == 0
    assert report["evaluation_status"] == "not_run"
