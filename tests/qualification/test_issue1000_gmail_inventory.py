from __future__ import annotations

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]
INVENTORY = ROOT / "docs/qualification/issue-1000-gmail-storage-copy-inventory-v1.md"


def test_inventory_maps_twenty_numbered_classes_with_unverified_gaps() -> None:
    content = INVENTORY.read_text(encoding="utf-8")
    rows = [
        line
        for line in content.splitlines()
        if re.match(r"^\| (?:[1-9]|1[0-9]|20)\. ", line)
    ]

    assert len(rows) == 20
    assert [int(re.match(r"^\| (\d+)\.", row).group(1)) for row in rows] == list(range(1, 21))
    assert all("NOT_VERIFIED" in row for row in rows)


def test_inventory_distinguishes_lifecycle_and_cleanup_boundaries() -> None:
    content = INVENTORY.read_text(encoding="utf-8")

    for term in (
        "Provider revocation signal",
        "Current access / processing fence",
        "Canonical tombstone",
        "Future separately authorized physical cleanup",
        "Replicas / WAL / backups",
        "Isolated restore",
    ):
        assert term in content


def test_inventory_evidence_paths_exist_in_checkout() -> None:
    for path in (
        "src/spine/domain/artifacts/email_governance.py",
        "src/spine/domain/sources/retention.py",
        "src/spine/application/email_artifacts.py",
        "src/spine/application/email_attachments.py",
        "src/spine/application/persistence/parsed_artifacts.py",
        "src/spine/application/projection_recovery_execution.py",
        "src/spine/application/projection_publication.py",
        "src/spine/infrastructure/persistence/projection.py",
        "src/spine/memory/cognee/context_projection.py",
        "src/spine/memory/cognee/recovery.py",
        "src/spine/application/persistence/workflow_runs.py",
        "src/spine/infrastructure/persistence/postgresql_workflow_runs.py",
        "src/spine/domain/findings/__init__.py",
        "src/spine/application/storage/upload.py",
        "src/spine/application/storage/reconciliation.py",
        "src/spine/application/answer_question_attempts.py",
        "src/spine/api/authorized_ask.py",
        "src/spine/infrastructure/object_storage",
        "src/spine/infrastructure/isolated_restore.py",
        "src/spine/infrastructure/backup_artifacts.py",
        "src/spine/infrastructure/postgres_backup.py",
        "src/spine/application/diagnostics/audit.py",
        "src/spine/infrastructure/telemetry/diagnostics.py",
        "src/spine/infrastructure/diagnostics/sinks.py",
        "src/spine/api/diagnostics.py",
    ):
        assert (ROOT / path).exists(), path
