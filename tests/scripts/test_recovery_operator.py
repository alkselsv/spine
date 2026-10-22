from __future__ import annotations

import json

import pytest

from spine.scripts.recovery_operator import main


def valid_profile() -> dict[str, object]:
    return {
        "operation": "backup",
        "profile_reference": "profile:recovery-v1",
        "deployment_scope_reference": "deployment:production",
        "scope_reference": "scope:workspace-environment",
        "change_reference": "change:2026-532",
        "recovery_set_id": "recovery-set:one",
        "operator_identity_reference": "identity:operator",
        "coordinator_identity_reference": "identity:coordinator",
        "restore_identity_reference": "identity:restore",
        "verifier_identity_reference": "identity:verifier",
        "source_inventory_reference": "inventory:source-v1",
        "source_volume_reference": "volume:source",
        "protected_destination_reference": "destination:protected",
        "encryption_profile_reference": "encryption:prod",
        "protected_transfer_profile_reference": "transfer:tls",
        "retention_profile_reference": "retention:30d",
        "postgres_tool_profile_reference": "tool:postgres-17",
        "filesystem_semantics_profile_reference": "filesystem:posix-v1",
        "fence_profile_reference": "fence:adr-0022-v1",
        "restore_target_inventory_reference": "inventory:restore-v1",
        "serving_identity_inventory_reference": "inventory:serving-v1",
        "evidence_destination_reference": "evidence:protected",
        "evidence_reviewer_reference": "identity:reviewer",
    }


def test_recovery_operator_cli_validates_profile_without_starting_recovery(
    tmp_path, capsys
) -> None:
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps(valid_profile()), encoding="utf-8")

    with pytest.raises(SystemExit) as exit_info:
        main(["--profile", str(profile_path), "--validate-only"])

    assert exit_info.value.code == 0
    assert json.loads(capsys.readouterr().out)["status"] == "validated"


def test_recovery_operator_cli_redacts_invalid_profile(tmp_path, capsys) -> None:
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(
        json.dumps(valid_profile() | {"source_volume_reference": "postgresql://secret"}),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as exit_info:
        main(["--profile", str(profile_path), "--validate-only"])

    captured = capsys.readouterr()
    assert exit_info.value.code == 2
    assert "postgresql://secret" not in captured.err
    assert "config.invalid" in captured.err
