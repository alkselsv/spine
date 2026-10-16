from __future__ import annotations

import ast
import json
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
REPORT = PROJECT_ROOT / "docs/qualification/issue-357-grounded-answers-report.json"


def test_issue357_report_is_versioned_and_keeps_provider_qualification_separate() -> None:
    report = json.loads(REPORT.read_text(encoding="utf-8"))

    assert report["schema_version"] == "grounded-answer-qualification:v1"
    assert report["issue"] == 357
    assert report["observation_seam"] == "r1-production-contract-observation:v1"
    assert report["safety_seam"] == "g03-grounded-answer-safety:v1"
    assert report["qualification_scope"] == "offline-contract-only"
    assert report["status"] == "qualified_offline"
    assert report["production_approval"] is False
    assert report["external_qualification"] == {
        "real_cognee": "not_run",
        "real_llm": "not_run",
        "production_provider": "not_run",
    }


def test_issue357_report_covers_each_acceptance_criterion_with_real_test_nodes() -> None:
    report = json.loads(REPORT.read_text(encoding="utf-8"))
    coverage = report["coverage"]

    assert tuple(coverage) == tuple(str(index) for index in range(1, 10))
    assert all(coverage.values())
    for evidence_nodes in coverage.values():
        for evidence in evidence_nodes:
            path_text, node_name = evidence.split("::", maxsplit=1)
            test_path = PROJECT_ROOT / path_text
            assert test_path.is_file()
            tree = ast.parse(test_path.read_text(encoding="utf-8"))
            names = {
                node.name
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            assert node_name in names, evidence
