from __future__ import annotations

import hashlib

import pytest

from spine.connectors.google_live_qualification import GoogleLiveQualificationRefused
from spine.scripts.google_live_qualification import main
from spine.infrastructure.google_live_runtime import (
    GooglePrivateLiveRuntime,
    build_google_live_config_from_environment,
)


MANIFEST_KEYS = (
    "GOOGLE_QUALIFICATION_ACCOUNT",
    "GOOGLE_CLOUD_PROJECT_ID",
    "GOOGLE_OAUTH_CLIENT_FILE",
    "GOOGLE_ROOT_FOLDER_ID",
    "GOOGLE_NESTED_FOLDER_IDS",
    "GOOGLE_ALLOWED_FILE_IDS",
    "GOOGLE_SPREADSHEET_IDS",
    "GOOGLE_NUMERIC_SHEET_IDS",
    "GOOGLE_SHARED_DRIVE_STATUS",
    "GOOGLE_RESOURCE_SCOPE_VERSION",
)


def _environment() -> dict[str, str]:
    values = {
        "GOOGLE_QUALIFICATION_ACCOUNT": "synthetic@example.test",
        "GOOGLE_CLOUD_PROJECT_ID": "synthetic-project",
        "GOOGLE_OAUTH_CLIENT_FILE": "/private/synthetic-oauth-client.json",
        "GOOGLE_ROOT_FOLDER_ID": "synthetic-root",
        "GOOGLE_NESTED_FOLDER_IDS": "synthetic-nested",
        "GOOGLE_ALLOWED_FILE_IDS": "synthetic-pdf,synthetic-docx,synthetic-doc",
        "GOOGLE_SPREADSHEET_IDS": "synthetic-spreadsheet",
        "GOOGLE_NUMERIC_SHEET_IDS": "0,12345",
        "GOOGLE_SHARED_DRIVE_STATUS": "NOT_EXERCISED",
        "GOOGLE_RESOURCE_SCOPE_VERSION": "google-615-phase1-scope:v1",
        "GOOGLE_PDF_FILE_ID": "synthetic-pdf",
        "GOOGLE_DOCX_FILE_ID": "synthetic-docx",
        "GOOGLE_NATIVE_DOC_FILE_ID": "synthetic-doc",
        "SPINE_GOOGLE_LIVE_CONFIG_OPT_IN": "1",
        "SPINE_GOOGLE_OAUTH_CLIENT_ID": "synthetic-client-id",
        "SPINE_GOOGLE_OAUTH_CLIENT_SECRET": "synthetic-client-secret",
        "SPINE_GOOGLE_OAUTH_REFRESH_TOKEN": "synthetic-refresh-token",
    }
    material = "".join(f"{key}={values[key]}\n" for key in MANIFEST_KEYS)
    values["SPINE_GOOGLE_QUALIFICATION_MANIFEST_SHA256"] = hashlib.sha256(
        material.encode()
    ).hexdigest()
    return values


class RefusingTransport:
    def __init__(self) -> None:
        self.calls = 0

    async def request(self, request):
        del request
        self.calls += 1
        raise AssertionError("private preflight must not contact Google")


def _write_environment(path, environment: dict[str, str]) -> None:
    path.write_text(
        "".join(f"{key}={value}\n" for key, value in environment.items()),
        encoding="utf-8",
    )
    path.chmod(0o600)


def test_private_environment_builds_exact_approved_phase1_config() -> None:
    environment = _environment()

    config = build_google_live_config_from_environment(environment)

    assert config.opt_in is True
    assert config.resource_scope.allowed_file_ids == (
        "synthetic-pdf",
        "synthetic-docx",
        "synthetic-doc",
    )
    assert config.resource_scope.allowed_spreadsheet_ids == (
        "synthetic-spreadsheet",
    )
    assert tuple(target.profile.sheet_id for target in config.sheet_targets) == (
        0,
        12345,
    )
    assert config.request_policy.max_requests_per_run == 500
    assert config.execution_envelope.max_run_duration_seconds == 1800.0
    assert config.shared_drive_approved is False


def test_private_environment_rejects_manifest_digest_mismatch() -> None:
    environment = _environment()
    environment["GOOGLE_ROOT_FOLDER_ID"] = "different-root"

    with pytest.raises(GoogleLiveQualificationRefused, match="manifest"):
        build_google_live_config_from_environment(environment)


@pytest.mark.asyncio
async def test_private_runtime_positive_preflight_verifies_credentials_without_provider_call() -> None:
    environment = _environment()
    config = build_google_live_config_from_environment(environment)
    environment["SPINE_GOOGLE_LIVE_APPROVED_EFFECTIVE_FINGERPRINT"] = (
        config.effective_configuration_fingerprint
    )
    transport = RefusingTransport()
    runner = GooglePrivateLiveRuntime(
        environment, transport=transport
    ).build_runner(config)

    await runner.preflight(environment_opt_in=True)

    assert transport.calls == 0


@pytest.mark.asyncio
async def test_private_runtime_rejects_missing_refresh_token_before_provider_call() -> None:
    environment = _environment()
    config = build_google_live_config_from_environment(environment)
    environment["SPINE_GOOGLE_LIVE_APPROVED_EFFECTIVE_FINGERPRINT"] = (
        config.effective_configuration_fingerprint
    )
    environment["SPINE_GOOGLE_OAUTH_REFRESH_TOKEN"] = ""
    transport = RefusingTransport()
    runner = GooglePrivateLiveRuntime(
        environment, transport=transport
    ).build_runner(config)

    with pytest.raises(GoogleLiveQualificationRefused, match="credentials"):
        await runner.preflight(environment_opt_in=True)

    assert transport.calls == 0


def test_private_runtime_rejects_unapproved_effective_fingerprint() -> None:
    environment = _environment()
    config = build_google_live_config_from_environment(environment)
    environment["SPINE_GOOGLE_LIVE_APPROVED_EFFECTIVE_FINGERPRINT"] = "0" * 64

    with pytest.raises(GoogleLiveQualificationRefused, match="not approved"):
        GooglePrivateLiveRuntime(environment).build_runner(config)


def test_private_env_cli_inspects_safe_fingerprints_without_composition(
    tmp_path, capsys
) -> None:
    environment = _environment()
    path = tmp_path / "qualification.env"
    _write_environment(path, environment)

    result = main(["--env-file", str(path), "--inspect-fingerprints"])

    captured = capsys.readouterr()
    assert result == 0
    assert "manifest_sha256=" in captured.out
    assert "effective_configuration_fingerprint=" in captured.out
    assert "synthetic-client-secret" not in captured.out
    assert captured.err == ""


def test_private_env_cli_runs_positive_preflight_without_provider_report(
    tmp_path, monkeypatch, capsys
) -> None:
    environment = _environment()
    config = build_google_live_config_from_environment(environment)
    environment["SPINE_GOOGLE_LIVE_APPROVED_EFFECTIVE_FINGERPRINT"] = (
        config.effective_configuration_fingerprint
    )
    path = tmp_path / "qualification.env"
    _write_environment(path, environment)
    monkeypatch.setenv("SPINE_GOOGLE_PREFLIGHT_OPT_IN", "1")

    result = main(
        [
            "--env-file",
            str(path),
            "--composition",
            "spine.infrastructure.google_live_runtime:build_live_runner",
            "--preflight-only",
        ]
    )

    captured = capsys.readouterr()
    assert result == 0
    assert "preflight passed" in captured.out
    assert "synthetic-client-secret" not in captured.out
    assert list(tmp_path.iterdir()) == [path]


def test_private_env_cli_refuses_insecure_file_mode(tmp_path, capsys) -> None:
    path = tmp_path / "qualification.env"
    _write_environment(path, _environment())
    path.chmod(0o644)

    result = main(["--env-file", str(path), "--inspect-fingerprints"])

    captured = capsys.readouterr()
    assert result == 2
    assert captured.out == ""
    assert "approved opt-in or prerequisites are missing" in captured.err
