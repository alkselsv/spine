"""Repeatable synthetic-data and operator-wizard contract for issue #680."""

from __future__ import annotations

import importlib.util
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[2]
GENERATOR_PATH = ROOT / "tools/generate_issue680_mounted_filesystem_corpus.py"
WIZARD_PATH = ROOT / "tools/issue680_mounted_filesystem_qualification_wizard.sh"
REPORT_WRITER_PATH = ROOT / "tools/issue680_qualification_report.py"
RUNBOOK_PATH = ROOT / "docs/runbooks/r2-mounted-filesystem.md"
EXPECTED_ARCHIVE_SHA256 = (
    "23ddf81990bc322334474b379209a307116283f2d2bf100d0297280c172f1331"
)


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _generator_module():
    return _load_module("issue680_corpus", GENERATOR_PATH)


def _report_writer_module():
    return _load_module("issue680_report_writer", REPORT_WRITER_PATH)


def test_generator_builds_the_approved_deterministic_synthetic_corpus(
    tmp_path: Path,
) -> None:
    package, archive_path, checksum = _generator_module().generate(tmp_path)
    manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))

    assert checksum == EXPECTED_ARCHIVE_SHA256
    assert manifest["classification"] == "synthetic_non_production"
    assert manifest["approved_adapter_profile"] == {
        "default_max_file_bytes": 32 * 1024 * 1024,
        "hard_ceiling_bytes": 256 * 1024 * 1024,
        "read_chunk_bytes": 64 * 1024,
        "read_timeout_seconds": 10,
        "scan_timeout_seconds": 10,
        "max_concurrent_reads_per_worker": 4,
        "max_reconciliation_attempts_per_item": 2,
    }
    entries = {entry["purpose"]: entry for entry in manifest["entries"]}
    assert entries["64 KiB chunk boundary plus one byte"]["byte_length"] == 65537
    assert entries["32 MiB default limit inclusive boundary"]["byte_length"] == (
        32 * 1024 * 1024
    )
    assert entries["32 MiB default limit rejection"]["byte_length"] == (
        32 * 1024 * 1024 + 1
    )
    assert {entry["type"] for entry in manifest["entries"]} == {
        "regular",
        "symlink",
        "fifo",
    }
    outside = package / "outside-scope-synthetic.txt"
    assert outside.is_file()
    assert (package / "corpus" / "symlink-escape").resolve(strict=True) == outside
    external_fixture = manifest["external_fixtures"][0]
    assert external_fixture["relative_path_from_package"] == outside.name
    assert external_fixture["byte_length"] == outside.stat().st_size
    assert (
        external_fixture["sha256"] == hashlib.sha256(outside.read_bytes()).hexdigest()
    )

    with tarfile.open(archive_path, "r:gz") as archive:
        members = archive.getmembers()
        assert all(member.uid == 0 and member.gid == 0 for member in members)
        assert all(member.mtime == 1_700_000_000 for member in members)
        assert any(member.issym() for member in members)
        assert any(member.isfifo() for member in members)
        assert not any(
            "outside-scope-synthetic.txt" in member.name for member in members
        )


def test_operator_wizard_describes_confirmed_stages_and_never_controls_mount() -> None:
    script = WIZARD_PATH.read_text(encoding="utf-8")
    described = subprocess.run(
        ["bash", str(WIZARD_PATH), "--describe"],
        capture_output=True,
        check=True,
        text=True,
    )

    assert described.stdout.splitlines() == [
        "1. Approved scope and ownership",
        "2. Read-only target preflight",
        "3. Baseline and containment",
        "4. Streaming and size limits",
        "5. Path-scoped rename",
        "6. Unstable read quarantine",
        "7. Mount interruption and recovery",
        "8. Identity, sanitized evidence, and cleanup",
    ]
    assert not re.search(r"^\s*(sudo\s+)?(mount|umount)\s", script, flags=re.MULTILINE)
    assert (
        subprocess.run(
            ["bash", "-n", str(WIZARD_PATH)], capture_output=True, check=False
        ).returncode
        == 0
    )


def test_operator_wizard_writes_not_verified_report_when_prerequisite_is_missing(
    tmp_path: Path,
) -> None:
    report_path = tmp_path / "operator-report.json"
    state_path = tmp_path / "private-state.env"
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_python3 = fake_bin / "python3"
    fake_python3.write_text("#!/bin/sh\nexit 77\n", encoding="utf-8")
    fake_python3.chmod(0o755)
    result = subprocess.run(
        ["bash", str(WIZARD_PATH)],
        input="\n",
        capture_output=True,
        check=False,
        text=True,
        env={
            **os.environ,
            "ISSUE680_STATE_FILE": str(state_path),
            "ISSUE680_REPORT_PATH": str(report_path),
            "NO_COLOR": "1",
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
        },
    )

    assert result.returncode == 1
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "NOT_VERIFIED"
    assert report["reason_code"] == "prerequisite_missing"
    assert set(report["scenarios"].values()) == {"NOT_VERIFIED"}
    assert report["identity_conclusion"] == "path_scoped"
    assert report["backend_file_id_claim"] == "NOT_CLAIMED"


def test_operator_report_requires_independent_review_even_when_operator_passes() -> (
    None
):
    module = _report_writer_module()
    state = {
        key: "PASS"
        for key in (
            "PREFLIGHT_STATUS",
            "CONTAINMENT_STATUS",
            "LIMITS_STATUS",
            "RENAME_STATUS",
            "UNSTABLE_STATUS",
            "OUTAGE_STATUS",
            "RECOVERY_STATUS",
            "IDENTITY_STATUS",
            "CLEANUP_STATUS",
        )
    }
    state.update(
        {
            "BACKEND_FILE_ID_STATUS": "QUALIFIED",
            "BACKEND_IDENTITY_EVIDENCE_REFERENCE": "operator-only-reference",
        }
    )

    report = module.build_report(state, "independent_evidence_review_required")
    payload = report.model_dump(mode="json")

    assert payload["status"] == "NOT_VERIFIED"
    assert payload["reason_code"] == "independent_evidence_review_required"
    assert payload["identity_conclusion"] == "path_scoped"
    assert payload["backend_file_id_claim"] == "NOT_CLAIMED"
    assert "operator-only-reference" not in json.dumps(payload)


def test_operator_report_preserves_failure_as_fail() -> None:
    module = _report_writer_module()

    report = module.build_report(
        {"CONTAINMENT_STATUS": "FAIL"}, "independent_evidence_review_required"
    )

    assert report.status == "FAIL"
    assert report.reason_code == "operator_failure"


def test_missing_prerequisite_discards_stale_scenario_results() -> None:
    module = _report_writer_module()

    report = module.build_report(
        {
            "PREFLIGHT_STATUS": "PASS",
            "CONTAINMENT_STATUS": "FAIL",
            "RECOVERY_STATUS": "PASS",
        },
        "prerequisite_missing",
    )

    assert report.status == "NOT_VERIFIED"
    assert report.reason_code == "prerequisite_missing"
    assert set(report.scenarios.model_dump().values()) == {"NOT_VERIFIED"}


def test_runbook_routes_the_operator_through_generator_and_wizard() -> None:
    runbook = RUNBOOK_PATH.read_text(encoding="utf-8")

    assert "generate_issue680_mounted_filesystem_corpus.py" in runbook
    assert "issue680_mounted_filesystem_qualification_wizard.sh" in runbook
    assert EXPECTED_ARCHIVE_SHA256 in runbook
    assert "MountedFilesystemReconciliationWorker.reconcile" in runbook
