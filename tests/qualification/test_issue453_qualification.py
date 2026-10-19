from __future__ import annotations

import json
from pathlib import Path

from spine.evals.real_benchmark_qualification import RuntimePrerequisite


ROOT = Path(__file__).parents[2]
REPORT = ROOT / "docs/qualification/issue-453-r1-benchmark-qualification.json"
RUNBOOK = ROOT / "docs/runbooks/r1-real-benchmark-qualification.md"


def test_issue453_report_records_offline_evidence_without_claiming_real_qualification() -> None:
    report = json.loads(REPORT.read_text(encoding="utf-8"))

    assert report["schema_version"] == "r1-benchmark-qualification-evidence:v1"
    assert report["ticket"] == 453
    assert report["deterministic_contract"]["status"] == "implemented"
    assert set(report["deterministic_contract"]["prerequisites"]) == {
        prerequisite.value for prerequisite in RuntimePrerequisite
    }
    assert report["real_opt_in_qualification"]["status"] == "not_exercised"
    assert report["real_opt_in_qualification"]["ci_execution"] is False
    assert report["real_opt_in_qualification"]["synthetic_or_fake_counts_as_real"] is False
    assert report["status_boundaries"]["release_qualification"] == "not_run"
    assert report["status_boundaries"]["issue_17_thresholds_asserted"] is False
    assert report["qualification_record_scope"] == [
        "trusted workspace_id",
        "trusted environment_id",
    ]
    for item in report["deterministic_contract"]["evidence"]:
        assert (ROOT / item).is_file()


def test_issue453_runbook_documents_explicit_local_invocation_and_status_boundaries() -> None:
    runbook = RUNBOOK.read_text(encoding="utf-8")
    workflow = (ROOT / ".github/workflows/test.yml").read_text(encoding="utf-8")

    assert "--dataset-kind real" in runbook
    assert "RealBenchmarkQualificationRuntime" in runbook
    assert "release qualification" in runbook.lower()
    assert "data owner" in runbook.lower()
    assert "spine-eval-real-benchmark" not in workflow
