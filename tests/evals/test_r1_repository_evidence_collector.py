from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

from spine.domain.evaluations.r1_repository_qualification import (
    MigrationTargetReceipt,
    R1SkipPolicy,
    R1SkipPolicyEntry,
    R1WorkflowExecution,
)
from spine.evaluations.r1_repository_evidence import collect_r1_repository_evidence

HEAD_SHA = "a" * 40
POLICY_VERSION = "r1-skip-policy:v1"


def _write_json(path: Path, value: object) -> None:
    path.write_text(value.model_dump_json(indent=2) + "\n", encoding="utf-8")  # type: ignore[attr-defined]


def test_junit_skip_is_classified_by_exact_versioned_policy(tmp_path: Path) -> None:
    clean_path = tmp_path / "clean.json"
    upgrade_path = tmp_path / "upgrade.json"
    _write_json(
        clean_path,
        MigrationTargetReceipt(
            role="clean",
            target_fingerprint="1" * 64,
            postgresql_major=17,
            head_revision="20261101_76",
            retained_revision_count=76,
            tested_revision_count=1,
            status="passed",
        ),
    )
    _write_json(
        upgrade_path,
        MigrationTargetReceipt(
            role="upgrade",
            target_fingerprint="2" * 64,
            postgresql_major=17,
            head_revision="20261101_76",
            retained_revision_count=76,
            tested_revision_count=76,
            status="passed",
        ),
    )
    policy_path = tmp_path / "policy.json"
    _write_json(
        policy_path,
        R1SkipPolicy(
            policy_version=POLICY_VERSION,
            entries=(
                R1SkipPolicyEntry(
                    test_id="tests/provider/test_live.py::test_live_provider",
                    expected_reason="live provider is opt-in",
                    reason_code="external-provider-opt-in",
                    applicability="optional",
                ),
            ),
        ),
    )
    junit_path = tmp_path / "pytest.xml"
    junit_path.write_text(
        """<?xml version="1.0" encoding="utf-8"?>
<testsuites tests="2" failures="0" errors="0" skipped="1">
  <testsuite name="pytest" tests="2" failures="0" errors="0" skipped="1">
    <testcase classname="tests.provider.test_live" name="test_live_provider">
      <properties><property name="spine_nodeid" value="tests/provider/test_live.py::test_live_provider" /></properties>
      <skipped type="pytest.skip" message="live provider is opt-in" />
    </testcase>
    <testcase classname="tests.unit.test_ok" name="test_ok">
      <properties><property name="spine_nodeid" value="tests/unit/test_ok.py::test_ok" /></properties>
    </testcase>
  </testsuite>
</testsuites>
""",
        encoding="utf-8",
    )

    evidence = collect_r1_repository_evidence(
        head_sha=HEAD_SHA,
        clean_target_path=clean_path,
        upgrade_target_path=upgrade_path,
        junit_path=junit_path,
        skip_policy_path=policy_path,
        workflow_execution=R1WorkflowExecution(
            workflow_name="test",
            run_id="41432821168",
            job_id="r1-regression-gate",
            run_attempt=1,
            started_at=datetime(2026, 11, 2, 10, tzinfo=timezone.utc),
            completed_at=datetime(2026, 11, 2, 10, 10, tzinfo=timezone.utc),
        ),
    )

    assert evidence.repository_tests.passed == 1
    assert evidence.repository_tests.skipped == 1
    assert evidence.repository_tests.skip_dispositions[0].model_dump() == {
        "test_id": "tests/provider/test_live.py::test_live_provider",
        "reported_reason": "live provider is opt-in",
        "reason_code": "external-provider-opt-in",
        "applicability": "optional",
        "policy_version": POLICY_VERSION,
    }
    assert evidence.skip_policy_digest == hashlib.sha256(
        policy_path.read_bytes()
    ).hexdigest()
    repository_command = next(
        command for command in evidence.commands if command.boundary == "repository_tests"
    )
    assert repository_command.argv == (
        "uv",
        "run",
        "pytest",
        "-q",
        "--durations=50",
        "--deselect=tests/integration/postgresql/test_r1_migration_targets.py::test_current_graph_applies_to_clean_target",
        "--deselect=tests/integration/postgresql/test_initial_tenancy_migration.py::test_every_retained_revision_upgrades_to_one_final_head",
    )
