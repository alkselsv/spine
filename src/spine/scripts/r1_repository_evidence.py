"""Collect the metadata-only R1 repository evidence artifact."""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
from pathlib import Path
import sys
from xml.etree import ElementTree

from pydantic import ValidationError

from spine.evaluations.r1_repository_evidence import collect_r1_repository_evidence
from spine.domain.evaluations.r1_repository_qualification import (
    R1SkipPolicy,
    R1WorkflowExecution,
)
from spine.evaluations.release_gates.r1_repository_qualification import (
    evaluate_r1_repository_evidence,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Collect canonical metadata-only R1 repository evidence."
    )
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--clean-target", type=Path, required=True)
    parser.add_argument("--upgrade-target", type=Path, required=True)
    parser.add_argument("--junit", type=Path, required=True)
    parser.add_argument("--skip-policy", type=Path, required=True)
    parser.add_argument("--workflow-run-id", required=True)
    parser.add_argument("--workflow-run-attempt", type=int, required=True)
    parser.add_argument("--started-at", type=_parse_datetime, required=True)
    parser.add_argument("--completed-at", type=_parse_datetime, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)

    try:
        evidence = collect_r1_repository_evidence(
            head_sha=arguments.head_sha,
            clean_target_path=arguments.clean_target,
            upgrade_target_path=arguments.upgrade_target,
            junit_path=arguments.junit,
            skip_policy_path=arguments.skip_policy,
            workflow_execution=R1WorkflowExecution(
                workflow_name="test",
                run_id=arguments.workflow_run_id,
                job_id="r1-regression-gate",
                run_attempt=arguments.workflow_run_attempt,
                started_at=arguments.started_at,
                completed_at=arguments.completed_at,
            ),
        )
        arguments.output.write_text(
            evidence.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
        policy_bytes = arguments.skip_policy.read_bytes()
        reasons = evaluate_r1_repository_evidence(
            evidence=evidence,
            skip_policy=R1SkipPolicy.model_validate_json(policy_bytes),
            skip_policy_digest=hashlib.sha256(policy_bytes).hexdigest(),
        )
    except (ElementTree.ParseError, OSError, ValidationError, ValueError):
        print("R1 evidence collection refused invalid input.", file=sys.stderr)
        return 2
    return 1 if reasons else 0


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


if __name__ == "__main__":
    raise SystemExit(main())
