from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest

from spine.application.yandex_qualification_connection import (
    YANDEX_QUALIFICATION_APPROVAL_ID,
    YANDEX_QUALIFICATION_CONNECTION_ID,
    YANDEX_QUALIFICATION_CREDENTIAL_REFERENCE,
    YANDEX_QUALIFICATION_GRANT_REFERENCE,
    YANDEX_QUALIFICATION_REFERENCE,
    YANDEX_QUALIFICATION_RESOURCE_SCOPE_REFERENCE,
    YANDEX_QUALIFICATION_SERVICE_PRINCIPAL_ID,
    YandexQualificationConnectionConfig,
)
from spine.scripts import yandex_qualification_connection


def _id(value: int) -> UUID:
    return UUID(f"76210000-0000-4000-8000-{value:012d}")


def _config() -> YandexQualificationConnectionConfig:
    return YandexQualificationConnectionConfig(
        workspace_id=_id(1),
        environment_id=_id(2),
        approval_id=YANDEX_QUALIFICATION_APPROVAL_ID,
        connection_id=YANDEX_QUALIFICATION_CONNECTION_ID,
        service_principal_id=YANDEX_QUALIFICATION_SERVICE_PRINCIPAL_ID,
        operator_identity_id=_id(6),
        credential_reference=YANDEX_QUALIFICATION_CREDENTIAL_REFERENCE,
        grant_reference=YANDEX_QUALIFICATION_GRANT_REFERENCE,
        resource_scope_reference=YANDEX_QUALIFICATION_RESOURCE_SCOPE_REFERENCE,
        qualification_reference=YANDEX_QUALIFICATION_REFERENCE,
        authorization_generation=2,
        configuration_generation=3,
        approved_at=datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc),
        security_approval_reference="issue-701-comment-6539550358",
        data_approval_reference="issue-702-comment-6539550684",
        fixture_manifest_version="spine-yandex-qualification-fixtures-v1",
        scope_version="spine-yandex-qualification-scope:v1",
        approved_root="/Spine-Qualification-678-v1",
    )


def test_cli_loads_strict_config_and_private_report_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "connection.json"
    config_path.write_text(_config().model_dump_json(), encoding="utf-8")
    report_path = (tmp_path / "connection-report.json").resolve()
    captured: dict[str, object] = {}

    async def fake_run(**arguments: object) -> int:
        captured.update(arguments)
        return 0

    monkeypatch.setattr(yandex_qualification_connection, "_run", fake_run)

    result = yandex_qualification_connection.main(
        ["--config", str(config_path), "--report", str(report_path)]
    )

    assert result == 0
    assert captured == {"config": _config(), "report_path": report_path}


def test_cli_rejects_invalid_config_without_disclosing_input(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = tmp_path / "connection.json"
    config_path.write_text('{"database_url":"must-not-leak"}', encoding="utf-8")

    result = yandex_qualification_connection.main(
        [
            "--config",
            str(config_path),
            "--report",
            str((tmp_path / "report.json").resolve()),
        ]
    )

    captured = capsys.readouterr()
    assert result == 2
    assert captured.out == ""
    assert "must-not-leak" not in captured.err
    assert "bootstrap refused" in captured.err.lower()


def test_database_target_binding_ignores_role_but_rejects_other_database() -> None:
    same_target = yandex_qualification_connection._same_database_target(
        "postgresql+psycopg://spine_migration:migration-secret@db:5432/spine",
        "postgresql+psycopg://spine_runtime:runtime-secret@db/spine",
    )
    other_target = yandex_qualification_connection._same_database_target(
        "postgresql+psycopg://spine_migration:migration-secret@db/spine",
        "postgresql+psycopg://spine_runtime:runtime-secret@db/other",
    )

    assert same_target is True
    assert other_target is False
