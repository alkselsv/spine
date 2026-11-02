from __future__ import annotations

from pathlib import Path

import yaml


def _r1_steps() -> list[dict[str, object]]:
    workflow = yaml.safe_load(
        (Path(__file__).parents[2] / ".github/workflows/test.yml").read_text(
            encoding="utf-8"
        )
    )
    return workflow["jobs"]["r1-regression-gate"]["steps"]


def test_skip_policy_evaluation_precedes_attestation_and_upload() -> None:
    steps = _r1_steps()
    names = [step.get("name") for step in steps]

    assert names.index("Collect canonical R1 repository evidence") < names.index(
        "Attest R1 repository evidence from main"
    )
    assert names.index("Collect canonical R1 repository evidence") < names.index(
        "Upload metadata-only R1 repository evidence"
    )


def test_r1_timestamp_is_recorded_in_the_same_job_as_collection() -> None:
    steps = _r1_steps()
    names = [step.get("name") for step in steps]

    assert "Record R1 evidence start time" in names
    assert names.index("Record R1 evidence start time") < names.index(
        "Collect canonical R1 repository evidence"
    )
