from __future__ import annotations

import json
from pathlib import Path


def test_opaque_current_safety_case_pins_every_content_bearing_seam() -> None:
    path = (
        Path(__file__).parents[2]
        / "src"
        / "spine"
        / "evals"
        / "data"
        / "opaque_exclusion_v1.json"
    )
    dataset = json.loads(path.read_text(encoding="utf-8"))

    assert dataset["schema_version"] == "opaque-exclusion-eval:v1"
    assert dataset["version"] == "1.0.0"
    case = dataset["cases"][0]
    assert case == {
        "case_id": "opaque-current-never-retrieved",
        "acceptance_mode": "opaque_acceptance",
        "current_revision": True,
        "expected_projection_request": False,
        "expected_parser_calls": 0,
        "expected_projection_adapter_calls": 0,
        "expected_retrieval_references": 0,
        "expected_disclosure": "abstained",
    }
