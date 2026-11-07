from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import pytest

from spine.application.yandex_qualification_connection import (
    YandexQualificationBinaryReadConfig,
)
from spine.scripts import yandex_binary_read_authority


def _id(value: int) -> UUID:
    return UUID(f"78910000-0000-4000-8000-{value:012d}")


def _config() -> YandexQualificationBinaryReadConfig:
    approved_at = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
    return YandexQualificationBinaryReadConfig(
        workspace_id=_id(1),
        environment_id=_id(2),
        operator_identity_id=_id(3),
        approved_at=approved_at,
        expires_at=approved_at + timedelta(days=7),
        manifest_digest=(
            "487360a49df37aa6fd3dac3b78750b0d991a8a7de44f5fc405a85f304befedc7"
        ),
        security_approval_reference="issue-789-comment-6687500665",
        data_approval_reference="issue-789-comment-6687500665",
        max_live_runs=10,
        download_redirect_host="mds.storage.yandex.net",
    )


def test_cli_loads_strict_binary_read_config_and_private_report_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "binary-read.json"
    config_path.write_text(_config().model_dump_json(), encoding="utf-8")
    report_path = (tmp_path / "binary-read-report.json").resolve()
    captured: dict[str, object] = {}

    async def fake_run(**arguments: object) -> int:
        captured.update(arguments)
        return 0

    monkeypatch.setattr(yandex_binary_read_authority, "_run", fake_run)

    result = yandex_binary_read_authority.main(
        ["--config", str(config_path), "--report", str(report_path)]
    )

    assert result == 0
    assert captured == {"config": _config(), "report_path": report_path}


def test_cli_rejects_invalid_config_without_disclosing_input(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = tmp_path / "binary-read.json"
    config_path.write_text('{"database_url":"must-not-leak"}', encoding="utf-8")

    result = yandex_binary_read_authority.main(
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
    assert "provisioning refused" in captured.err.lower()
