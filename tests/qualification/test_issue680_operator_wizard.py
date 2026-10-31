"""Repeatable synthetic-data and operator-wizard contract for issue #680."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import re
import subprocess
import tarfile


ROOT = Path(__file__).resolve().parents[2]
GENERATOR_PATH = ROOT / "tools/generate_issue680_mounted_filesystem_corpus.py"
WIZARD_PATH = ROOT / "tools/issue680_mounted_filesystem_qualification_wizard.sh"
RUNBOOK_PATH = ROOT / "docs/runbooks/r2-mounted-filesystem.md"
EXPECTED_ARCHIVE_SHA256 = (
    "f440f989356fc0842749b1db6045e7df8878470a6476c60dbc84563bc9699faf"
)


def _generator_module():
    spec = importlib.util.spec_from_file_location("issue680_corpus", GENERATOR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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

    with tarfile.open(archive_path, "r:gz") as archive:
        members = archive.getmembers()
        assert all(member.uid == 0 and member.gid == 0 for member in members)
        assert all(member.mtime == 1_700_000_000 for member in members)
        assert any(member.issym() for member in members)
        assert any(member.isfifo() for member in members)
        assert not any("outside-scope-synthetic.txt" in member.name for member in members)


def test_operator_wizard_has_the_confirmed_stages_and_never_controls_the_mount() -> (
    None
):
    script = WIZARD_PATH.read_text(encoding="utf-8")
    stages = re.findall(r'^stage "([^"]+)"$', script, flags=re.MULTILINE)

    assert "TOTAL_STAGES=8" in script
    assert stages == [
        "Approved scope and ownership",
        "Read-only target preflight",
        "Baseline and containment",
        "Streaming and size limits",
        "Path-scoped rename",
        "Unstable read quarantine",
        "Mount interruption and recovery",
        "Identity, sanitized evidence, and cleanup",
    ]
    assert "MountedFilesystemReconciliationWorker.reconcile" in script
    assert "source_write_operation_performed_by_spine\": False" in script
    assert "mount_operation_performed_by_spine\": False" in script
    assert "QUALIFICATION_ROOT" not in script.split("report = {", 1)[1]
    assert not re.search(r"^\s*(sudo\s+)?(mount|umount)\s", script, flags=re.MULTILINE)
    assert subprocess.run(
        ["bash", "-n", str(WIZARD_PATH)], capture_output=True, check=False
    ).returncode == 0


def test_runbook_routes_the_operator_through_generator_and_wizard() -> None:
    runbook = RUNBOOK_PATH.read_text(encoding="utf-8")

    assert "generate_issue680_mounted_filesystem_corpus.py" in runbook
    assert "issue680_mounted_filesystem_qualification_wizard.sh" in runbook
    assert EXPECTED_ARCHIVE_SHA256 in runbook
    assert "MountedFilesystemReconciliationWorker.reconcile" in runbook
