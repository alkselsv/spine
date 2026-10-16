from __future__ import annotations

import json
from pathlib import Path

from spine.evals.unauthorized_retrieval import (
    UNAUTHORIZED_RETRIEVAL_SAFETY_CASE_IDS,
    UNAUTHORIZED_RETRIEVAL_SAFETY_SEAM,
)


PROJECT_ROOT = Path(__file__).parents[2]
REPORT = PROJECT_ROOT / "docs/qualification/issue-356-unauthorized-retrieval-disclosure-report.json"
TRACEABILITY = PROJECT_ROOT / "docs/qualification/issue-356-unauthorized-retrieval-disclosure.md"


def test_issue356_report_is_machine_readable_and_disclosure_safe() -> None:
    report = json.loads(REPORT.read_text())

    assert report["schema_version"] == "unauthorized-retrieval-disclosure-qualification:v1"
    assert report["observation_seam"] == "r1-production-contract-observation:v1"
    assert report["safety_seam"] == UNAUTHORIZED_RETRIEVAL_SAFETY_SEAM
    assert report["status"] == "passed"
    assert report["safety_violation_count"] == 0
    assert report["protected_payload_disclosure_count"] == 0
    assert tuple(report["safety_case_ids"]) == UNAUTHORIZED_RETRIEVAL_SAFETY_CASE_IDS
    assert "confidential" not in json.dumps(report).lower()


def test_issue356_traceability_names_offline_boundary_and_external_limits() -> None:
    content = TRACEABILITY.read_text()

    assert "G01" in content
    assert "deterministic" in content
    assert "not claimed" in content
    for case_id in UNAUTHORIZED_RETRIEVAL_SAFETY_CASE_IDS:
        assert case_id in content
