from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest

from spine.auth import ExternalSecretReference
from spine.application.yandex_disk_qualification import (
    YandexQualificationInventoryConfig,
)
from spine.application.yandex_qualification_credentials import (
    YANDEX_QUALIFICATION_CLIENT_ID,
    YANDEX_QUALIFICATION_OAUTH_SCOPE,
    YANDEX_QUALIFICATION_PROVIDER_REFERENCE,
    YANDEX_QUALIFICATION_ROOT,
    YANDEX_QUALIFICATION_SECRET_REFERENCE,
    YandexQualificationCredentialBinding,
)
from spine.domain.connections.provider import OpaqueProviderReference
from spine.scripts import yandex_live_inventory


def _id(value: int) -> UUID:
    return UUID(f"00000000-0000-4000-8000-{value:012d}")


def _config() -> YandexQualificationInventoryConfig:
    return YandexQualificationInventoryConfig(
        opt_in=True,
        credential_binding=YandexQualificationCredentialBinding(
            schema_version="yandex-qualification-credential-binding:v1",
            deployment_mode="qualification",
            workspace_id=_id(1),
            environment_id=_id(2),
            reference=ExternalSecretReference(
                provider_reference=YANDEX_QUALIFICATION_PROVIDER_REFERENCE,
                secret_reference=YANDEX_QUALIFICATION_SECRET_REFERENCE,
            ),
            client_id=YANDEX_QUALIFICATION_CLIENT_ID,
            oauth_scope=YANDEX_QUALIFICATION_OAUTH_SCOPE,
            approved_root=YANDEX_QUALIFICATION_ROOT,
            expires_at=datetime(2027, 1, 1, tzinfo=timezone.utc),
        ),
        connection_id=_id(3),
        connection_version_id=_id(4),
        service_principal_id=_id(5),
        resource_scope_reference=OpaqueProviderReference(value=_id(6)),
        authorization_generation=2,
        configuration_generation=3,
        security_approval_reference="issue-701-comment-6539550358",
        data_approval_reference="issue-702-comment-6539550684",
        fixture_manifest_version="spine-yandex-qualification-fixtures-v1",
        scope_version="spine-yandex-qualification-scope:v1",
        approved_manifest_digest="a" * 64,
    )


def test_cli_parses_private_paths_and_propagates_double_opt_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(_config().model_dump_json(), encoding="utf-8")
    captured: dict[str, object] = {}

    async def fake_run(**arguments: object) -> int:
        captured.update(arguments)
        return 0

    monkeypatch.setattr(yandex_live_inventory, "_run", fake_run)
    monkeypatch.setenv("SPINE_YANDEX_LIVE_INVENTORY_OPT_IN", "1")
    paths = {
        "env_file": (tmp_path / ".env.qualification").resolve(),
        "identity_file": (tmp_path / "identity.env").resolve(),
        "candidate_path": (tmp_path / "candidate.json").resolve(),
        "report_path": (tmp_path / "report.json").resolve(),
        "lease_file": (tmp_path / "inventory.lock").resolve(),
    }

    result = yandex_live_inventory.main(
        [
            "--config", str(config_path),
            "--env-file", str(paths["env_file"]),
            "--identity-file", str(paths["identity_file"]),
            "--manifest-candidate", str(paths["candidate_path"]),
            "--report", str(paths["report_path"]),
            "--lease-file", str(paths["lease_file"]),
        ]
    )

    assert result == 0
    assert captured == {
        "config": _config(),
        **paths,
        "environment_opt_in": True,
    }


def test_cli_rejects_invalid_config_without_disclosing_input(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "config.json"
    config.write_text('{"oauth_token":"must-not-leak"}', encoding="utf-8")

    result = yandex_live_inventory.main(
        [
            "--config", str(config),
            "--env-file", str(tmp_path / "env"),
            "--identity-file", str(tmp_path / "identity"),
            "--manifest-candidate", str(tmp_path / "candidate"),
            "--report", str(tmp_path / "report"),
            "--lease-file", str(tmp_path / "lease"),
        ]
    )

    captured = capsys.readouterr()
    assert result == 2
    assert captured.out == ""
    assert "must-not-leak" not in captured.err
    assert "inventory refused" in captured.err.lower()


def test_cli_refuses_without_environment_opt_in_before_private_inputs_or_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config.json"
    config.write_text(_config().model_dump_json(), encoding="utf-8")
    report = (tmp_path / "report.json").resolve()
    candidate = (tmp_path / "candidate.json").resolve()
    monkeypatch.delenv("SPINE_YANDEX_LIVE_INVENTORY_OPT_IN", raising=False)
    monkeypatch.delenv("SPINE_DATABASE_URL", raising=False)

    result = yandex_live_inventory.main(
        [
            "--config", str(config),
            "--env-file", str((tmp_path / "missing-env").resolve()),
            "--identity-file", str((tmp_path / "missing-identity").resolve()),
            "--manifest-candidate", str(candidate),
            "--report", str(report),
            "--lease-file", str((tmp_path / "lease").resolve()),
        ]
    )

    assert result == 1
    assert '"failure_code": "prerequisite_denied"' in report.read_text(
        encoding="utf-8"
    )
    assert not candidate.exists()
