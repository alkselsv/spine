from __future__ import annotations

import json
from pathlib import Path
import re

import pytest


REPORT_PATH = (
    Path(__file__).parents[2]
    / "docs"
    / "qualification"
    / "issue-950-google-phase1-offline-closure.json"
)

REQUIRED_CASES = {
    "positive_full_trace",
    "oauth_401_reauthorization",
    "oauth_second_401",
    "drive_policy_reasons",
    "not_found_404",
    "quota_429",
    "http_5xx",
    "timeout",
    "stream_failure",
    "malformed_contract_response",
    "cancellation",
    "deadline",
    "resource_limits",
    "drive_snapshot_pagination",
    "drive_changes_schema",
    "sheets_representations_pagination",
    "disclosure_safety",
}

TOP_LEVEL_KEYS = {
    "schema_version",
    "issue",
    "parent_issue",
    "phase",
    "oauth_scopes",
    "live_status",
    "shared_drive_status",
    "provider_transport_used",
    "mutations_exercised",
    "cases",
}
CASE_KEYS = {"status", "test_node"}
POSITIVE_CASE_KEYS = CASE_KEYS | {
    "expected_completed_operations",
    "expected_transport_attempts",
}


def _test_node_exists(repository_root: Path, node: str) -> bool:
    path_text, separator, test_name = node.partition("::")
    if not separator or not test_name.startswith("test_"):
        return False
    path = repository_root / path_text
    if not path.is_file():
        return False
    source = path.read_text(encoding="utf-8")
    return re.search(rf"^(?:async )?def {re.escape(test_name)}\(", source, re.MULTILINE) is not None


def _validate_report(report: dict[str, object], repository_root: Path) -> None:
    assert set(report) == TOP_LEVEL_KEYS
    assert report["schema_version"] == "google-phase1-offline-closure:v1"
    assert report["issue"] == 950
    assert report["parent_issue"] == 615
    assert report["phase"] == "phase1-my-drive-sheets-readonly"
    assert report["oauth_scopes"] == [
        "https://www.googleapis.com/auth/drive.readonly",
        "https://www.googleapis.com/auth/spreadsheets.readonly",
    ]
    assert report["live_status"] == "NOT_RUN"
    assert report["shared_drive_status"] == "NOT_EXERCISED"
    assert report["provider_transport_used"] is False
    assert report["mutations_exercised"] is False

    cases = report["cases"]
    assert isinstance(cases, dict)
    assert set(cases) == REQUIRED_CASES
    assert all(case["status"] == "OFFLINE_VERIFIED" for case in cases.values())
    assert all(
        set(case) == (
            POSITIVE_CASE_KEYS if name == "positive_full_trace" else CASE_KEYS
        )
        for name, case in cases.items()
    )
    assert all(
        _test_node_exists(repository_root, case["test_node"])
        for case in cases.values()
    )

    positive = cases["positive_full_trace"]
    assert positive["expected_completed_operations"] == {
        "metadata_reads": 3,
        "downloads": 2,
        "exports": 2,
        "snapshot_pages": 1,
        "change_pages": 1,
        "sheet_pages": 2,
    }
    assert positive["expected_transport_attempts"] == {
        "oauth": 1,
        "drive": 14,
        "sheets": 4,
    }

    serialized = re.sub(
        r"[^a-z0-9]", "", json.dumps(report, sort_keys=True).lower()
    )
    for forbidden in (
        "clientsecret",
        "refreshtoken",
        "accesstoken",
        "authorizationheader",
        "payload",
        "rawerror",
        "providerid",
        "fileid",
        "folderid",
        "spreadsheetid",
        "sheetid",
    ):
        assert forbidden not in serialized


def test_issue950_offline_closure_matrix_is_complete_and_executable() -> None:
    _validate_report(
        json.loads(REPORT_PATH.read_text(encoding="utf-8")),
        Path(__file__).parents[2],
    )


@pytest.mark.parametrize(
    "forbidden_field",
    ("client_secret", "provider_file_id", "payload", "rawError"),
)
def test_issue950_offline_closure_matrix_rejects_private_evidence_fields(
    forbidden_field: str,
) -> None:
    report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    report["cases"]["not_found_404"][forbidden_field] = "synthetic-private-value"

    with pytest.raises(AssertionError):
        _validate_report(report, Path(__file__).parents[2])


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("live_status", "PASSED"),
        ("shared_drive_status", "PASSED"),
        ("provider_transport_used", True),
        ("mutations_exercised", True),
    ),
)
def test_issue950_offline_closure_matrix_rejects_unperformed_claims(
    field: str, value: object
) -> None:
    report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    report[field] = value

    with pytest.raises(AssertionError):
        _validate_report(report, Path(__file__).parents[2])
