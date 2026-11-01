"""Qualification evidence contract for issue #680."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPORT_PATH = ROOT / "docs/qualification/issue-680-mounted-filesystem-attempt-v1.json"


def _report() -> dict[str, object]:
    return json.loads(REPORT_PATH.read_text(encoding="utf-8"))


def test_missing_live_prerequisites_are_not_verified() -> None:
    report = _report()

    assert report["schema_version"] == "issue-680-mounted-filesystem:v1"
    assert report["ticket"] == 680
    assert report["status"] == "NOT_VERIFIED"
    assert report["reason_code"] == "approved_target_unavailable"
    assert report["prerequisites"] == {
        "approved_qualification_root": "NOT_VERIFIED",
        "spine_source_reader_identity": "NOT_VERIFIED",
        "read_only_mount_options": "NOT_VERIFIED",
        "synthetic_fixture_approval": "NOT_VERIFIED",
        "platform_operations_outage_control": "NOT_VERIFIED",
        "cleanup_ownership": "NOT_VERIFIED",
    }


def test_unattempted_live_scenarios_never_receive_conditional_pass() -> None:
    report = _report()
    scenarios = report["scenarios"]

    assert isinstance(scenarios, dict)
    assert set(scenarios) == {
        "deployment_scope_and_identity",
        "traversal_and_symlink_containment",
        "streaming_limits",
        "path_scoped_rename",
        "unstable_read",
        "mount_interruption",
        "recovery_reconciliation",
        "canonical_source_retention",
        "backend_file_identity",
        "cleanup",
    }
    assert set(scenarios.values()) == {"NOT_VERIFIED"}
    assert report["identity_conclusion"] == "path_scoped"
    assert report["backend_file_id_claim"] == "NOT_CLAIMED"


def test_attempt_report_contains_only_content_safe_host_evidence() -> None:
    report = _report()
    serialized = REPORT_PATH.read_text(encoding="utf-8").lower()

    assert report["safety"] == {
        "mount_operation_performed": False,
        "source_write_operation_performed": False,
        "source_content_recorded": False,
        "source_names_recorded": False,
        "share_address_recorded": False,
        "credentials_recorded": False,
        "raw_errors_recorded": False,
    }
    for forbidden in (
        "smb://",
        "cifs://",
        "password",
        "access_token",
        "refresh_token",
        "/users/",
    ):
        assert forbidden not in serialized
