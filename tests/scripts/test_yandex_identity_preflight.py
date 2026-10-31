from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest

from spine.auth import ExternalSecretReference
from spine.application.yandex_identity_preflight import (
    YandexIdentityPreflightConfig,
    YandexIdentityPreflightReport,
)
from spine.infrastructure.yandex_qualification_credentials import (
    YANDEX_QUALIFICATION_CLIENT_ID,
    YANDEX_QUALIFICATION_OAUTH_SCOPE,
    YANDEX_QUALIFICATION_PROVIDER_REFERENCE,
    YANDEX_QUALIFICATION_ROOT,
    YANDEX_QUALIFICATION_SECRET_REFERENCE,
    YandexQualificationCredentialBinding,
)
from spine.scripts import yandex_identity_preflight


def _config() -> YandexIdentityPreflightConfig:
    return YandexIdentityPreflightConfig(
        opt_in=True,
        credential_binding=YandexQualificationCredentialBinding(
            schema_version="yandex-qualification-credential-binding:v1",
            deployment_mode="qualification",
            workspace_id=UUID("00000000-0000-4000-8000-000000000001"),
            environment_id=UUID("00000000-0000-4000-8000-000000000002"),
            reference=ExternalSecretReference(
                provider_reference=YANDEX_QUALIFICATION_PROVIDER_REFERENCE,
                secret_reference=YANDEX_QUALIFICATION_SECRET_REFERENCE,
            ),
            client_id=YANDEX_QUALIFICATION_CLIENT_ID,
            oauth_scope=YANDEX_QUALIFICATION_OAUTH_SCOPE,
            approved_root=YANDEX_QUALIFICATION_ROOT,
            expires_at=datetime(2027, 1, 1, tzinfo=timezone.utc),
        ),
    )


def test_cli_parses_paths_and_propagates_environment_opt_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(_config().model_dump_json(), encoding="utf-8")
    captured: dict[str, object] = {}

    async def fake_run(**arguments: object) -> int:
        captured.update(arguments)
        return 0

    monkeypatch.setattr(yandex_identity_preflight, "_run", fake_run)
    monkeypatch.setenv("SPINE_YANDEX_IDENTITY_PREFLIGHT_OPT_IN", "1")
    env_file = tmp_path / ".env.qualification"
    candidate = tmp_path / "candidate"
    report = tmp_path / "report"

    result = yandex_identity_preflight.main(
        [
            "--config",
            str(config_path),
            "--env-file",
            str(env_file),
            "--identity-candidate",
            str(candidate),
            "--report",
            str(report),
        ]
    )

    assert result == 0
    assert captured == {
        "config": _config(),
        "env_file": env_file,
        "candidate_path": candidate,
        "report_path": report,
        "environment_opt_in": True,
    }


def test_report_failure_removes_new_candidate_without_replacing_report(
    tmp_path: Path,
) -> None:
    candidate = (tmp_path / "candidate").resolve()
    candidate.write_text("private identity\n", encoding="utf-8")
    report_path = (tmp_path / "report").resolve()
    report_path.write_text("operator owned\n", encoding="utf-8")
    report = YandexIdentityPreflightReport(
        status="ready_for_human_approval",
        workspace_id=UUID("00000000-0000-4000-8000-000000000001"),
        environment_id=UUID("00000000-0000-4000-8000-000000000002"),
        client_id=YANDEX_QUALIFICATION_CLIENT_ID,
        identity_fingerprint="a" * 64,
        identity_requests=1,
        identity_candidate_written=True,
    )

    with pytest.raises(OSError):
        yandex_identity_preflight._write_report_with_candidate_rollback(
            report_path=report_path,
            candidate_path=candidate,
            report=report,
        )

    assert not candidate.exists()
    assert report_path.read_text(encoding="utf-8") == "operator owned\n"


def test_cli_rejects_invalid_config_with_sanitized_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text('{"private_token":"must-not-leak"}', encoding="utf-8")

    result = yandex_identity_preflight.main(
        [
            "--config",
            str(config_path),
            "--env-file",
            str(tmp_path / "env"),
            "--identity-candidate",
            str(tmp_path / "candidate"),
            "--report",
            str(tmp_path / "report"),
        ]
    )

    captured = capsys.readouterr()
    assert result == 2
    assert captured.out == ""
    assert "must-not-leak" not in captured.err
    assert "preflight refused" in captured.err.lower()
