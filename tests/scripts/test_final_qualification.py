"""Operator boundary tests for final qualification recomputation."""

from __future__ import annotations

import json

from spine.scripts.final_qualification import main

from tests.evals.test_final_qualification_gate import _criterion, _package


def test_cli_writes_reproducible_no_go_report(tmp_path) -> None:
    package = _package(_criterion(13, status="blocked"))
    package_path = tmp_path / "package.json"
    report_path = tmp_path / "report.json"
    index_path = tmp_path / "index.json"
    package_path.write_text(package.model_dump_json(indent=2), encoding="utf-8")
    entries = []
    seen = set()
    for criterion in package.criteria:
        for evidence in criterion.evidence:
            if evidence.evidence_id in seen:
                continue
            seen.add(evidence.evidence_id)
            artifact_path = f"artifact-{len(entries)}.txt"
            (tmp_path / artifact_path).write_text(
                evidence.evidence_id, encoding="ascii"
            )
            entries.append(
                {
                    "evidence_id": evidence.evidence_id,
                    "artifact_path": artifact_path,
                    "digest": evidence.digest,
                }
            )
    index_path.write_text(
        json.dumps({"schema_version": "r0-r1-evidence-index:v1", "entries": entries}),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "--package",
            str(package_path),
            "--evidence-index",
            str(index_path),
            "--evidence-root",
            str(tmp_path),
            "--report",
            str(report_path),
        ]
    )

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert exit_code == 1
    assert report["decision"] == "no_go"
    assert report["criteria"][-1]["criterion_id"] == "R17"
    assert report["criteria"][-1]["status"] == "fail"


def test_cli_refuses_invalid_package_without_writing_report(tmp_path) -> None:
    package_path = tmp_path / "package.json"
    report_path = tmp_path / "report.json"
    index_path = tmp_path / "index.json"
    package_path.write_text("{}", encoding="utf-8")
    index_path.write_text("{}", encoding="utf-8")

    exit_code = main(
        [
            "--package",
            str(package_path),
            "--evidence-index",
            str(index_path),
            "--evidence-root",
            str(tmp_path),
            "--report",
            str(report_path),
        ]
    )

    assert exit_code == 2
    assert not report_path.exists()
