from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import hashlib
import json
from pathlib import Path

import pytest
from dotenv import dotenv_values

from spine.domain.common import EnvironmentKind
from spine.infrastructure.cognee_worker_settings import CogneeWorkerBindingSettings
from spine.infrastructure.evaluations.command_runner import CommandExecutionResult
from spine.infrastructure.evaluations.r2_deployment_collector import (
    DockerComposeR2EvidenceCollector,
    HttpProbeResponse,
    R2CollectionError,
    R2CollectorConfiguration,
)
from spine.infrastructure.local_cognee_provider_guards import (
    LocalCogneeProviderAuthorization,
    LocalCogneeProviderAuthorizationSet,
)
from tests.infrastructure.test_cognee_worker_settings import binding_payload


HEAD_SHA = "a" * 40
DEPLOYMENT_FINGERPRINT = "b" * 64
NOW = datetime(2026, 11, 2, 16, tzinfo=UTC)
TRACE_ID = "50800000-0000-4000-8000-000000000073"
WORKER_BINDING_JSON = json.dumps(binding_payload(), separators=(",", ":"))
WORKER_BINDING = CogneeWorkerBindingSettings(
    binding_json=WORKER_BINDING_JSON
).load_configuration()
SERVICES = (
    "api",
    "web",
    "qa-worker",
    "outbox-dispatcher",
    "postgres",
    "temporal",
    "original-storage-bootstrap",
    "neo4j",
)


def _health_summary_data(
    *,
    status: str = "ready",
    authorization_status: str = "ready",
    authorization_code: str = "authorization.ready",
    authorization_provider: str | None = None,
    publication_status: str = "ready",
    publication_code: str = "publication.ready",
    publication_provider: str | None = "postgresql",
    ingestion_status: str = "ready",
    ingestion_code: str = "ingestion.ready",
    ingestion_provider: str | None = "postgresql",
) -> dict[str, object]:
    return {
        "status": status,
        "build_version": "0.1.0",
        "authentication_mode": None,
        "configuration_version": None,
        "authorization": {
            "status": authorization_status,
            "code": authorization_code,
            "provider_name": authorization_provider,
        },
        "publication": {
            "status": publication_status,
            "code": publication_code,
            "provider_name": publication_provider,
        },
        "ingestion": {
            "status": ingestion_status,
            "code": ingestion_code,
            "provider_name": ingestion_provider,
        },
        "providers": {
            "authentication": authorization_provider,
            "publication": publication_provider,
            "ingestion": ingestion_provider,
        },
        "last_operation": None,
    }


class LiveComposeRunner:
    def __init__(self, *, provider_log: str = "") -> None:
        self.provider_log = provider_log

    def run(self, arguments: tuple[str, ...]) -> CommandExecutionResult:
        if arguments[:2] == ("docker", "inspect"):
            service = arguments[2].removeprefix("issue815-")
            running = service != "original-storage-bootstrap"
            env = []
            if service in {"api", "qa-worker", "outbox-dispatcher"}:
                env.append(
                    "SPINE_QA_DEPLOYMENT_SETTINGS_JSON="
                    + json.dumps(
                        {
                            "commit_sha": HEAD_SHA,
                            "deployment_fingerprint": DEPLOYMENT_FINGERPRINT,
                        },
                        separators=(",", ":"),
                    )
                )
            if service == "outbox-dispatcher":
                env.append(
                    f"SPINE_COGNEE_WORKER_BINDING_JSON={WORKER_BINDING_JSON}"
                )
            document = [
                {
                    "Image": "sha256:"
                    + hashlib.sha256(service.encode("ascii")).hexdigest(),
                    "Config": {"Image": f"spine/{service}:v1", "Env": env},
                    "State": {"Running": running, "ExitCode": 0},
                    "NetworkSettings": {"Networks": {"issue815_default": {}}},
                }
            ]
            return CommandExecutionResult(0, json.dumps(document), "")
        if "psql" in arguments:
            return CommandExecutionResult(0, "4\n", "")
        if "logs" in arguments:
            return CommandExecutionResult(0, self.provider_log, "")
        if "ps" in arguments:
            records = [
                {
                    "Service": service,
                    "Name": f"issue815-{service}",
                    "Image": f"spine/{service}:v1",
                }
                for service in SERVICES
            ]
            return CommandExecutionResult(0, "\n".join(map(json.dumps, records)), "")
        return CommandExecutionResult(0, "", "")


class ScopedHealthProbe:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, str]]] = []

    def get(self, *, url: str, headers: dict[str, str]) -> HttpProbeResponse:
        self.requests.append((url, headers))
        if headers.get("Authorization") == "Bearer r2-invalid-credential":
            return HttpProbeResponse(status_code=401, body=b"")
        if headers.get("X-Workspace-ID", "").startswith("00000000-"):
            return HttpProbeResponse(status_code=403, body=b"")
        if url.endswith("/api/v1/health/summary"):
            data: dict[str, object] = {"data": _health_summary_data()}
        else:
            if url.endswith("/api/v1/documents?limit=1"):
                data = {"items": []}
            else:
                assert url == "http://127.0.0.1:4175/"
                return HttpProbeResponse(status_code=200, body=b"")
        return HttpProbeResponse(
            status_code=200,
            body=json.dumps(
                {
                    "schema_version": 1,
                    "workspace_id": headers["X-Workspace-ID"],
                    "environment_id": headers["X-Environment-ID"],
                    "trace_id": TRACE_ID,
                    **data,
                }
            ).encode("utf-8"),
        )


class AuthorizationBypassingProbe(ScopedHealthProbe):
    def __init__(self, *, bypass: str) -> None:
        super().__init__()
        self.bypass = bypass

    def get(self, *, url: str, headers: dict[str, str]) -> HttpProbeResponse:
        if (
            self.bypass == "authentication"
            and headers.get("Authorization") == "Bearer r2-invalid-credential"
        ) or (
            self.bypass == "scope"
            and headers.get("X-Workspace-ID", "").startswith("00000000-")
        ):
            self.requests.append((url, headers))
            return HttpProbeResponse(status_code=200, body=b"{}")
        return super().get(url=url, headers=headers)


class ConfiguredHealthSummaryProbe(ScopedHealthProbe):
    def __init__(
        self,
        data: dict[str, object],
        *,
        envelope_updates: dict[str, object] | None = None,
        omitted_envelope_field: str | None = None,
    ) -> None:
        super().__init__()
        self.data = data
        self.envelope_updates = envelope_updates or {}
        self.omitted_envelope_field = omitted_envelope_field

    def get(self, *, url: str, headers: dict[str, str]) -> HttpProbeResponse:
        if (
            url.endswith("/api/v1/health/summary")
            and headers.get("Authorization") != "Bearer r2-invalid-credential"
            and not headers.get("X-Workspace-ID", "").startswith("00000000-")
        ):
            self.requests.append((url, headers))
            envelope: dict[str, object] = {
                "schema_version": 1,
                "workspace_id": headers["X-Workspace-ID"],
                "environment_id": headers["X-Environment-ID"],
                "trace_id": TRACE_ID,
                "data": self.data,
                **self.envelope_updates,
            }
            if self.omitted_envelope_field is not None:
                envelope.pop(self.omitted_envelope_field)
            return HttpProbeResponse(
                status_code=200,
                body=json.dumps(envelope).encode("utf-8"),
            )
        return super().get(url=url, headers=headers)


def _configuration(tmp_path: Path) -> R2CollectorConfiguration:
    profile = json.dumps(
        {
            "commit_sha": HEAD_SHA,
            "deployment_fingerprint": DEPLOYMENT_FINGERPRINT,
        },
        separators=(",", ":"),
    )
    (tmp_path / ".env").write_text(
        "\n".join(
            (
                f"SPINE_QA_DEPLOYMENT_SETTINGS_JSON={profile}",
                f"SPINE_COGNEE_WORKER_BINDING_JSON={WORKER_BINDING_JSON}",
                "SPINE_LOCAL_AUTH_BEARER_CREDENTIAL=synthetic-secret-0000000000000000",
                "SPINE_WORKSPACE_A_ID=50800000-0000-4000-8000-00000000000a",
                "SPINE_ENVIRONMENT_A_ID=50800000-0000-4000-8000-00000000000e",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    secret_references = tuple(
        dict.fromkeys(
            (
                WORKER_BINDING.profile.postgres_secret_ref,
                WORKER_BINDING.profile.neo4j_secret_ref,
                WORKER_BINDING.profile.model_secret_ref,
            )
        )
    )
    provider_authorization = LocalCogneeProviderAuthorizationSet(
        schema_version="cognee-local-provider-authorization-set:v1",
        deployment_mode="development",
        authorizations=(
            LocalCogneeProviderAuthorization(
                schema_version="cognee-local-provider-authorization:v1",
                deployment_mode="development",
                authorization_reference="authorization:issue-815:workspace-a",
                workspace_id="50800000-0000-4000-8000-00000000000a",
                environment_id="50800000-0000-4000-8000-00000000000e",
                profile_id=WORKER_BINDING.profile.profile_id,
                profile_digest=WORKER_BINDING.profile.profile_digest,
                amount=Decimal("1.00"),
                currency=WORKER_BINDING.profile.limits.budget_currency,
                expires_at=NOW + timedelta(days=1),
                secret_references=secret_references,
            ),
        ),
    ).model_dump_json()
    (tmp_path / ".env.qualification").write_text(
        "\n".join(
            (
                "SPINE_QUALIFICATION_AUTHORITY_EXPIRES_AT=2026-11-03T00:00:00Z",
                "SPINE_QUALIFICATION_TARGET_REFERENCE="
                f"deployment:source:{DEPLOYMENT_FINGERPRINT}",
                "SPINE_COGNEE_LOCAL_PROVIDER_AUTHORIZATION_JSON="
                f"{provider_authorization}",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    return R2CollectorConfiguration(
        compose_project="issue815",
        runtime_env_path=tmp_path / ".env",
        qualification_env_path=tmp_path / ".env.qualification",
        api_base_url="http://127.0.0.1:18080",
        web_base_url="http://127.0.0.1:4175",
        head_sha=HEAD_SHA,
        deployment_fingerprint=DEPLOYMENT_FINGERPRINT,
        environment=EnvironmentKind.DEVELOPMENT,
        principal_id="release-operator:alkselsv",
    )


def _replace_provider_authorization(
    configuration: R2CollectorConfiguration,
    *,
    case: str,
) -> None:
    values = dotenv_values(configuration.qualification_env_path, interpolate=False)
    document = LocalCogneeProviderAuthorizationSet.model_validate_json(
        values["SPINE_COGNEE_LOCAL_PROVIDER_AUTHORIZATION_JSON"]
    )
    payload = document.model_dump(mode="json")
    authorization = payload["authorizations"][0]
    if case == "currency":
        authorization["currency"] = "EUR"
    elif case == "amount":
        authorization["amount"] = str(
            WORKER_BINDING.profile.limits.max_budget_amount + Decimal("0.01")
        )
    elif case == "secret_references":
        authorization["secret_references"] = authorization["secret_references"][:-1]
    elif case == "expiry":
        authorization["expires_at"] = NOW.isoformat()
    else:  # pragma: no cover - closed test helper input
        raise AssertionError(case)
    replacement = LocalCogneeProviderAuthorizationSet.model_validate(payload)
    values["SPINE_COGNEE_LOCAL_PROVIDER_AUTHORIZATION_JSON"] = (
        replacement.model_dump_json()
    )
    configuration.qualification_env_path.write_text(
        "".join(f"{key}={value}\n" for key, value in values.items()),
        encoding="utf-8",
    )


def test_live_collector_derives_complete_bound_evidence(tmp_path: Path) -> None:
    times = iter((NOW, NOW, NOW, NOW + timedelta(minutes=1)))
    http_probe = ScopedHealthProbe()
    collector = DockerComposeR2EvidenceCollector(
        command_runner=LiveComposeRunner(),
        http_probe=http_probe,
        clock=lambda: next(times),
        execution_id_factory=lambda: "issue-815-live:v1",
    )

    evidence = collector.collect(_configuration(tmp_path))

    assert evidence.target_commit == HEAD_SHA
    assert evidence.deployment_fingerprint == DEPLOYMENT_FINGERPRINT
    assert evidence.environment is EnvironmentKind.DEVELOPMENT
    assert {item.role for item in evidence.components} == {
        "api",
        "web",
        "qa_worker",
        "outbox_dispatcher",
        "postgresql",
        "temporal",
        "original_storage",
        "selected_provider",
    }
    assert all(item.configured and item.reachable for item in evidence.components)
    assert all(len(item.artifact_digest) == 64 for item in evidence.components)
    selected_provider = next(
        item for item in evidence.components if item.role == "selected_provider"
    )
    assert selected_provider.component_id == WORKER_BINDING.profile.profile_id
    assert WORKER_BINDING.profile.profile_digest in selected_provider.version
    assert evidence.startup.authority_mutations == 0
    assert evidence.startup.provider_calls == 0
    assert evidence.readiness.authority_mutations == 0
    assert evidence.readiness.provider_calls == 0
    assert evidence.canary.status == "passed"
    assert any(
        headers.get("Authorization") == "Bearer r2-invalid-credential"
        for _, headers in http_probe.requests
    )
    assert any(
        headers.get("X-Workspace-ID", "").startswith("00000000-")
        for _, headers in http_probe.requests
    )


@pytest.mark.parametrize(
    ("authorization_code", "authorization_provider"),
    [
        ("authorization.ready", None),
        ("local.ready", "local"),
        ("oidc.ready", "oidc"),
    ],
)
def test_unpublished_projection_composition_is_r2_ready_before_r5(
    tmp_path: Path,
    authorization_code: str,
    authorization_provider: str | None,
) -> None:
    times = iter((NOW, NOW, NOW, NOW + timedelta(minutes=1)))
    collector = DockerComposeR2EvidenceCollector(
        command_runner=LiveComposeRunner(),
        http_probe=ConfiguredHealthSummaryProbe(
            _health_summary_data(
                status="degraded",
                authorization_code=authorization_code,
                authorization_provider=authorization_provider,
                publication_status="degraded",
                publication_code="publication.unpublished",
                publication_provider=None,
            )
        ),
        clock=lambda: next(times),
        execution_id_factory=lambda: "issue-871-unpublished-composition:v1",
    )

    evidence = collector.collect(_configuration(tmp_path))

    assert evidence.readiness.status == "passed"


@pytest.mark.parametrize(
    "data",
    [
        _health_summary_data(
            status="degraded",
            publication_status="degraded",
            publication_code="publication.degraded",
        ),
        _health_summary_data(
            status="stale",
            publication_status="stale",
            publication_code="publication.stale",
        ),
        _health_summary_data(
            status="unavailable",
            publication_status="unavailable",
            publication_code="publication.unavailable",
        ),
        _health_summary_data(
            status="unavailable",
            authorization_status="unavailable",
            authorization_code="authorization.unavailable",
        ),
        _health_summary_data(
            status="degraded",
            ingestion_status="degraded",
            ingestion_code="ingestion.degraded",
        ),
        _health_summary_data(
            status="ready",
            publication_status="degraded",
            publication_code="publication.unpublished",
            publication_provider=None,
        ),
        _health_summary_data(status="degraded"),
        _health_summary_data(
            authorization_status="ready",
            authorization_code="authorization.degraded",
        ),
    ],
    ids=[
        "degraded-publication",
        "stale-publication",
        "unavailable-publication",
        "unavailable-authorization",
        "degraded-ingestion",
        "ready-overall-with-unpublished-publication",
        "degraded-overall-with-ready-publication",
        "ready-authorization-with-wrong-code",
    ],
)
def test_non_qualifying_component_summary_fails_r2_readiness(
    tmp_path: Path,
    data: dict[str, object],
) -> None:
    times = iter((NOW, NOW, NOW, NOW + timedelta(minutes=1)))
    collector = DockerComposeR2EvidenceCollector(
        command_runner=LiveComposeRunner(),
        http_probe=ConfiguredHealthSummaryProbe(data),
        clock=lambda: next(times),
        execution_id_factory=lambda: "issue-871-non-qualifying-summary:v1",
    )

    evidence = collector.collect(_configuration(tmp_path))

    assert evidence.readiness.status == "failed"


@pytest.mark.parametrize(
    "data",
    [
        {key: value for key, value in _health_summary_data().items() if key != "ingestion"},
        {**_health_summary_data(), "protected": "must-not-be-accepted"},
        {**_health_summary_data(), "build_version": "not valid whitespace"},
        {
            **_health_summary_data(),
            "publication": {
                "status": "ready",
                "code": "publication.ready",
                "provider_name": "unknown-provider",
            },
        },
    ],
    ids=["missing-field", "extra-field", "invalid-field", "invalid-provider"],
)
def test_malformed_health_summary_is_rejected(
    tmp_path: Path,
    data: dict[str, object],
) -> None:
    collector = DockerComposeR2EvidenceCollector(
        command_runner=LiveComposeRunner(),
        http_probe=ConfiguredHealthSummaryProbe(data),
        clock=lambda: NOW,
    )

    with pytest.raises(R2CollectionError, match="authenticated_canary_invalid"):
        collector.collect(_configuration(tmp_path))


@pytest.mark.parametrize(
    ("envelope_updates", "omitted_envelope_field"),
    [
        ({"schema_version": 2}, None),
        ({"unexpected": "field"}, None),
        ({"trace_id": "00000000-0000-0000-0000-000000000000"}, None),
        ({}, "trace_id"),
    ],
    ids=["wrong-version", "extra-field", "zero-trace", "missing-trace"],
)
def test_malformed_health_summary_envelope_is_rejected(
    tmp_path: Path,
    envelope_updates: dict[str, object],
    omitted_envelope_field: str | None,
) -> None:
    collector = DockerComposeR2EvidenceCollector(
        command_runner=LiveComposeRunner(),
        http_probe=ConfiguredHealthSummaryProbe(
            _health_summary_data(),
            envelope_updates=envelope_updates,
            omitted_envelope_field=omitted_envelope_field,
        ),
        clock=lambda: NOW,
    )

    with pytest.raises(R2CollectionError, match="authenticated_canary_invalid"):
        collector.collect(_configuration(tmp_path))


def test_inconsistent_provider_summary_fails_r2_readiness(tmp_path: Path) -> None:
    data = _health_summary_data()
    data["providers"] = {
        "authentication": None,
        "publication": "cognee",
        "ingestion": "postgresql",
    }
    times = iter((NOW, NOW, NOW, NOW + timedelta(minutes=1)))
    collector = DockerComposeR2EvidenceCollector(
        command_runner=LiveComposeRunner(),
        http_probe=ConfiguredHealthSummaryProbe(data),
        clock=lambda: next(times),
        execution_id_factory=lambda: "issue-871-inconsistent-providers:v1",
    )

    evidence = collector.collect(_configuration(tmp_path))

    assert evidence.readiness.status == "failed"


def test_live_collector_observes_provider_request_marker(tmp_path: Path) -> None:
    times = iter((NOW, NOW, NOW, NOW + timedelta(minutes=1)))
    collector = DockerComposeR2EvidenceCollector(
        command_runner=LiveComposeRunner(
            provider_log="spine_provider_request_observed\n"
        ),
        http_probe=ScopedHealthProbe(),
        clock=lambda: next(times),
        execution_id_factory=lambda: "issue-815-live:v1",
    )

    evidence = collector.collect(_configuration(tmp_path))

    assert evidence.startup.provider_calls == 1
    assert evidence.readiness.provider_calls == 1


@pytest.mark.parametrize(
    ("bypass", "failed_field"),
    [
        ("authentication", "authenticated"),
        ("scope", "scope_verified"),
    ],
)
def test_live_collector_records_authorization_bypass_as_failed_readiness(
    tmp_path: Path,
    bypass: str,
    failed_field: str,
) -> None:
    times = iter((NOW, NOW, NOW, NOW + timedelta(minutes=1)))
    collector = DockerComposeR2EvidenceCollector(
        command_runner=LiveComposeRunner(),
        http_probe=AuthorizationBypassingProbe(bypass=bypass),
        clock=lambda: next(times),
        execution_id_factory=lambda: "issue-815-live:v1",
    )

    evidence = collector.collect(_configuration(tmp_path))

    assert getattr(evidence.readiness, failed_field) is False


def test_live_collector_refuses_without_runtime_configuration(
    tmp_path: Path,
) -> None:
    configuration = _configuration(tmp_path)
    configuration.runtime_env_path.unlink()
    collector = DockerComposeR2EvidenceCollector(
        command_runner=LiveComposeRunner(),
        http_probe=ScopedHealthProbe(),
    )

    with pytest.raises(R2CollectionError, match="runtime_configuration_unavailable"):
        collector.collect(configuration)


def test_live_collector_refuses_invalid_selected_provider_binding(
    tmp_path: Path,
) -> None:
    configuration = _configuration(tmp_path)
    runtime = configuration.runtime_env_path.read_text(encoding="utf-8")
    configuration.runtime_env_path.write_text(
        runtime.replace(WORKER_BINDING_JSON, "{}"),
        encoding="utf-8",
    )
    collector = DockerComposeR2EvidenceCollector(
        command_runner=LiveComposeRunner(),
        http_probe=ScopedHealthProbe(),
        clock=lambda: NOW,
    )

    with pytest.raises(
        R2CollectionError,
        match="provider_binding_invalid:profile_binding_invalid",
    ):
        collector.collect(configuration)


def test_live_collector_refuses_provider_authorization_for_another_scope(
    tmp_path: Path,
) -> None:
    configuration = _configuration(tmp_path)
    qualification = configuration.qualification_env_path.read_text(encoding="utf-8")
    configuration.qualification_env_path.write_text(
        qualification.replace(
            "50800000-0000-4000-8000-00000000000a",
            "50800000-0000-4000-8000-00000000000b",
        ),
        encoding="utf-8",
    )
    collector = DockerComposeR2EvidenceCollector(
        command_runner=LiveComposeRunner(),
        http_probe=ScopedHealthProbe(),
        clock=lambda: NOW,
    )

    with pytest.raises(
        R2CollectionError,
        match="provider_authorization_scope_mismatch",
    ):
        collector.collect(configuration)


def test_live_collector_refuses_environment_above_compose_authorization(
    tmp_path: Path,
) -> None:
    configuration = _configuration(tmp_path)
    configuration = replace(
        configuration,
        environment=EnvironmentKind.PRODUCTION,
    )
    collector = DockerComposeR2EvidenceCollector(
        command_runner=LiveComposeRunner(),
        http_probe=ScopedHealthProbe(),
        clock=lambda: NOW,
    )

    with pytest.raises(
        R2CollectionError,
        match="deployment_environment_mismatch",
    ):
        collector.collect(configuration)


@pytest.mark.parametrize(
    ("case", "reason_code"),
    [
        ("currency", "budget_currency_mismatch"),
        ("amount", "budget_over_limit"),
        ("secret_references", "secret_reference_set_mismatch"),
        ("expiry", "authorization_expired"),
    ],
)
def test_live_collector_refuses_provider_authorization_outside_profile_limits(
    tmp_path: Path,
    case: str,
    reason_code: str,
) -> None:
    configuration = _configuration(tmp_path)
    _replace_provider_authorization(configuration, case=case)
    collector = DockerComposeR2EvidenceCollector(
        command_runner=LiveComposeRunner(),
        http_probe=ScopedHealthProbe(),
        clock=lambda: NOW,
    )

    with pytest.raises(
        R2CollectionError,
        match=f"provider_authorization_invalid:{reason_code}",
    ):
        collector.collect(configuration)
