from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import json
import re
from types import SimpleNamespace

import pytest

from spine.domain.storage.recovery_operator import (
    RecoveryOperatorOperation,
    RecoveryOperatorProfile,
    RecoveryOperatorResult,
)


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "deploy/qualification/issue-509/recovery_job.py"
RECOVERY_DOCKERFILE = ROOT / "deploy/qualification/issue-509/Dockerfile.recovery"
SPEC = importlib.util.spec_from_file_location("issue509_recovery_job", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
JOB = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(JOB)
ATTEMPT_REPORT = ROOT / "docs/qualification/issue-509-recovery-attempt-v2.json"
VERIFIED_RESTORE_REPORT = ROOT / "docs/qualification/issue-509-recovery-attempt-v3.json"


@pytest.mark.asyncio
async def test_expired_or_mismatched_run_approval_is_rejected() -> None:
    profile = SimpleNamespace(approval_reference="approval:issue509-recovery-001")

    expired = JOB._Approval(
        "approval:issue509-recovery-001",
        datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    mismatched = JOB._Approval(
        "approval:different",
        datetime.now(timezone.utc) + timedelta(hours=1),
    )

    assert await expired.validate(profile) is False
    assert await mismatched.validate(profile) is False


@pytest.mark.asyncio
async def test_evidence_record_is_content_free_immutable_and_replayable(tmp_path: Path) -> None:
    evidence = JOB._Evidence(tmp_path / "evidence")
    await evidence.ensure_available(SimpleNamespace())
    result = RecoveryOperatorResult(
        operation=RecoveryOperatorOperation.BACKUP_AND_RESTORE,
        status="verified",
        recovery_set_id="recovery:issue509-001",
        manifest_reference="manifest:issue509-001",
        manifest_digest="a" * 64,
        restore_evidence_reference="restore:issue509-001",
    )

    reference = await evidence.record(result, reference_kind="restore")
    replay = await evidence.record(result, reference_kind="restore")

    assert replay == reference
    files = tuple((tmp_path / "evidence").iterdir())
    assert len(files) == 1
    payload = files[0].read_text(encoding="utf-8")
    assert "verified" in payload
    assert "password" not in payload.lower()
    assert "/" not in payload


def test_physical_attempt_report_is_closed_content_free_and_fail_closed() -> None:
    report = json.loads(ATTEMPT_REPORT.read_text(encoding="utf-8"))

    assert report["schema_version"] == "r1-backup-recovery-attempt:v2"
    assert report["decision"] == "fail"
    assert report["production_qualified"] is False
    assert report["immutable"] is True
    assert report["content_free"] is True
    assert len(report["attempts"]) == 3
    assert all(item["manifest_status"] == "incomplete" for item in report["attempts"])
    assert all(re.fullmatch(r"[0-9a-f]{64}", item["manifest_digest"]) for item in report["attempts"])
    assert report["isolation"]["restore_database_empty"] is True
    assert report["isolation"]["restore_filesystem_absent"] is True
    assert report["bounded_observations"]["coordinated_backup"] == "fail"
    assert report["bounded_observations"]["isolated_restore"] == "not_verified"
    assert report["independent_review"] == "required"

    serialized = json.dumps(report, sort_keys=True).lower()
    for forbidden in ("password", "credential", "source_text", "raw_error"):
        assert forbidden not in serialized


def test_recovery_image_pins_postgres_17_tools_without_changing_application_image() -> None:
    recipe = RECOVERY_DOCKERFILE.read_text(encoding="utf-8")

    assert recipe.startswith("FROM pgvector/pgvector:0.8.7-pg17-bookworm")
    assert 'ENV PATH="/opt/venv/bin:/usr/lib/postgresql/17/bin:${PATH}"' in recipe
    assert 'ENTRYPOINT ["/app/.venv/bin/python"]' in recipe


def test_verified_restore_report_does_not_overclaim_product_qualification() -> None:
    report = json.loads(VERIFIED_RESTORE_REPORT.read_text(encoding="utf-8"))

    assert report["backup"]["outcome"] == "verified"
    assert report["restore"]["outcome"] == "verified"
    assert report["bounded_observations"]["reconciliation"] == "verified"
    assert report["bounded_observations"]["context_projection_recovery"] == "not_verified"
    assert report["bounded_observations"]["restored_application_canary"] == "not_verified"
    assert report["production_qualified"] is False
    assert report["independent_review"] == "required"

    serialized = json.dumps(report, sort_keys=True).lower()
    for forbidden in ("password", "credential", "source_text", "raw_error", "filename"):
        assert forbidden not in serialized


def test_restore_target_facts_use_deployment_bound_references(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = RecoveryOperatorProfile.model_validate_json(
        (ROOT / "deploy/qualification/issue-509/recovery-profile-retry-2.json").read_text(
            encoding="utf-8"
        )
    )
    monkeypatch.setenv("SPINE_RESTORE_TARGET_REFERENCE", "target:issue509-restore-002")
    monkeypatch.setenv(
        "SPINE_RESTORE_DATABASE_TARGET_REFERENCE",
        "database:issue509-restore-002",
    )
    monkeypatch.setenv(
        "SPINE_RESTORE_FILESYSTEM_TARGET_REFERENCE",
        "filesystem:issue509-restore-002",
    )

    facts = JOB._target_facts(profile)

    assert facts.target_reference == "target:issue509-restore-002"
    assert facts.database_target_reference == "database:issue509-restore-002"
    assert facts.filesystem_target_reference == "filesystem:issue509-restore-002"
