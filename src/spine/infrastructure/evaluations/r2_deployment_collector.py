"""Concrete Docker Compose and authenticated HTTP collector for R2 evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Callable, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from dotenv import dotenv_values

from spine.domain.common import EnvironmentKind
from spine.domain.evaluations.r2_deployment_qualification import (
    R2CanaryObservation,
    R2ComponentObservation,
    R2DeploymentEvidence,
    R2LiveExecution,
    R2PrivacyDeclaration,
    R2ReadinessObservation,
    R2StartupObservation,
)
from spine.evaluations.release_gates.r2_deployment_qualification import (
    r2_composition_digest,
)
from spine.infrastructure.cognee_worker_settings import (
    CogneeWorkerBindingConfiguration,
    CogneeWorkerBindingSettings,
    CogneeWorkerConfigurationError,
)
from spine.infrastructure.evaluations.command_runner import CommandRunner
from spine.infrastructure.local_cognee_provider_guards import (
    LocalCogneeProviderAuthorization,
    LocalCogneeProviderAuthorizationError,
    LocalCogneeProviderAuthorizationSet,
    LocalCogneeProviderAuthorizationSettings,
    build_local_cognee_provider_guards,
)


_PROVIDER_REQUEST_MARKER = "spine_provider_request_observed"
_SERVICE_BY_ROLE = {
    "api": "api",
    "web": "web",
    "qa_worker": "qa-worker",
    "outbox_dispatcher": "outbox-dispatcher",
    "postgresql": "postgres",
    "temporal": "temporal",
    "original_storage": "original-storage-bootstrap",
    "selected_provider": "neo4j",
}
_DEPENDENCIES = {
    "api": ("postgresql", "temporal", "original_storage", "selected_provider"),
    "web": ("api",),
    "qa_worker": ("postgresql", "temporal", "selected_provider"),
    "outbox_dispatcher": ("postgresql", "temporal", "selected_provider"),
    "postgresql": (),
    "temporal": ("postgresql",),
    "original_storage": (),
    "selected_provider": ("postgresql",),
}
_AUTHORITY_COUNT_QUERY = (
    "SELECT (SELECT count(*) FROM spine.qualification_authority_bindings) + "
    "(SELECT count(*) FROM spine.qualification_target_bindings) + "
    "(SELECT count(*) FROM spine.access_policy_versions) + "
    "(SELECT count(*) FROM spine.access_policy_grants)"
)


class R2CollectionError(RuntimeError):
    """Bounded collection refusal without command output or secrets."""


@dataclass(frozen=True, slots=True)
class HttpProbeResponse:
    status_code: int
    body: bytes


class HttpProbe(Protocol):
    def get(self, *, url: str, headers: Mapping[str, str]) -> HttpProbeResponse: ...


class UrllibHttpProbe:
    """Perform one bounded HTTP GET without logging headers or response bodies."""

    def get(self, *, url: str, headers: Mapping[str, str]) -> HttpProbeResponse:
        request = Request(url, headers=dict(headers), method="GET")
        try:
            with urlopen(  # noqa: S310 - operator-configured loopback URL
                request, timeout=10
            ) as response:
                return HttpProbeResponse(
                    status_code=response.status,
                    body=response.read(1024 * 1024),
                )
        except HTTPError as error:
            return HttpProbeResponse(
                status_code=error.code,
                body=error.read(1024 * 1024),
            )
        except URLError:
            raise R2CollectionError("http_probe_unavailable") from None


@dataclass(frozen=True, slots=True)
class R2CollectorConfiguration:
    compose_project: str
    runtime_env_path: Path
    qualification_env_path: Path
    api_base_url: str
    web_base_url: str
    head_sha: str
    deployment_fingerprint: str
    environment: EnvironmentKind
    principal_id: str


@dataclass(frozen=True, slots=True)
class _ApprovedProviderBinding:
    raw_json: str
    configuration: CogneeWorkerBindingConfiguration


class DockerComposeR2EvidenceCollector:
    """Own one live execution and derive evidence only from concrete probes."""

    def __init__(
        self,
        *,
        command_runner: CommandRunner,
        http_probe: HttpProbe,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        execution_id_factory: Callable[[], str] = lambda: str(uuid4()),
    ) -> None:
        self._commands = command_runner
        self._http = http_probe
        self._clock = clock
        self._execution_id_factory = execution_id_factory

    def collect(self, configuration: R2CollectorConfiguration) -> R2DeploymentEvidence:
        """Provision, start, inspect and probe one isolated Compose target."""

        runtime = _load_env(configuration.runtime_env_path)
        qualification = _load_env(configuration.qualification_env_path)
        prefix = _compose_prefix(configuration)
        started_at = self._clock()
        approved_binding = _validate_configured_target(
            runtime,
            qualification,
            configuration,
            observed_at=started_at,
        )

        self._require_success(
            (
                *prefix,
                "--profile",
                "qualification-provisioning",
                "run",
                "--rm",
                "qualification-authority-provisioner",
            ),
            "qualification_provision_failed",
        )
        authority_baseline = self._authority_count(prefix)
        startup_log_boundary = self._clock()
        self._require_success(
            (*prefix, "up", "--build", "--detach", "--wait"),
            "deployment_start_failed",
        )
        authority_after_startup = self._authority_count(prefix)
        startup_provider_calls = self._provider_calls_since(
            prefix, startup_log_boundary
        )

        readiness_boundary = self._clock()
        summary = self._health_summary(configuration, runtime)
        authentication_denied = self._invalid_credential_denied(
            configuration, runtime
        )
        foreign_scope_denied = self._foreign_scope_denied(configuration, runtime)
        authority_after_readiness = self._authority_count(prefix)
        readiness_provider_calls = self._provider_calls_since(
            prefix, readiness_boundary
        )
        canary = self._application_canary(configuration, runtime)
        web_reachable = self._web_reachable(configuration)
        components = self._components(
            prefix,
            configuration,
            approved_provider_binding=approved_binding,
            web_reachable=web_reachable,
        )
        captured_at = self._clock()

        evidence = R2DeploymentEvidence(
            target_commit=configuration.head_sha,
            deployment_fingerprint=configuration.deployment_fingerprint,
            environment=configuration.environment,
            captured_at=captured_at,
            live_execution=R2LiveExecution(
                execution_id=self._execution_id_factory(),
                principal_id=configuration.principal_id,
                environment=configuration.environment,
                started_at=started_at,
                completed_at=captured_at,
            ),
            components=components,
            composition_digest="0" * 64,
            startup=R2StartupObservation(
                status="passed",
                authority_mutations=max(
                    0, authority_after_startup - authority_baseline
                ),
                provider_calls=startup_provider_calls,
            ),
            readiness=R2ReadinessObservation(
                status="passed" if summary["ready"] else "failed",
                authenticated=authentication_denied,
                scope_verified=(
                    summary["scope_verified"] and foreign_scope_denied
                ),
                authority_mutations=max(
                    0, authority_after_readiness - authority_after_startup
                ),
                provider_calls=readiness_provider_calls,
            ),
            canary=R2CanaryObservation(
                status="passed" if canary["passed"] else "failed",
                authenticated=True,
                scope_verified=canary["scope_verified"],
            ),
            privacy=R2PrivacyDeclaration(
                metadata_only=True,
                credentials_recorded=False,
                network_locations_recorded=False,
                protected_content_recorded=False,
                provider_payloads_recorded=False,
                raw_errors_recorded=False,
            ),
        )
        return R2DeploymentEvidence.model_validate(
            {
                **evidence.model_dump(),
                "composition_digest": r2_composition_digest(evidence),
            }
        )

    def _require_success(self, arguments: tuple[str, ...], reason: str) -> str:
        result = self._commands.run(arguments)
        if result.return_code != 0:
            raise R2CollectionError(reason)
        return result.stdout

    def _authority_count(self, prefix: tuple[str, ...]) -> int:
        output = self._require_success(
            (
                *prefix,
                "exec",
                "-T",
                "postgres",
                "psql",
                "-U",
                "postgres",
                "-d",
                "spine",
                "-Atc",
                _AUTHORITY_COUNT_QUERY,
            ),
            "authority_probe_failed",
        )
        try:
            return int(output.strip())
        except ValueError:
            raise R2CollectionError("authority_probe_invalid") from None

    def _provider_calls_since(
        self, prefix: tuple[str, ...], boundary: datetime
    ) -> int:
        output = self._require_success(
            (
                *prefix,
                "logs",
                "--no-color",
                "--since",
                boundary.astimezone(timezone.utc).isoformat(),
                "qa-worker",
                "outbox-dispatcher",
            ),
            "provider_call_probe_failed",
        )
        return output.count(_PROVIDER_REQUEST_MARKER)

    def _health_summary(
        self,
        configuration: R2CollectorConfiguration,
        runtime: Mapping[str, str],
    ) -> dict[str, bool]:
        token = _required(runtime, "SPINE_LOCAL_AUTH_BEARER_CREDENTIAL")
        workspace_id = _required(runtime, "SPINE_WORKSPACE_A_ID")
        environment_id = _required(runtime, "SPINE_ENVIRONMENT_A_ID")
        response = self._http.get(
            url=f"{configuration.api_base_url.rstrip('/')}/api/v1/health/summary",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Workspace-ID": workspace_id,
                "X-Environment-ID": environment_id,
            },
        )
        try:
            body = json.loads(response.body)
            scope_verified = (
                body["workspace_id"] == workspace_id
                and body["environment_id"] == environment_id
            )
            data = body["data"]
            ready = response.status_code == 200 and _deployment_summary_ready(data)
        except (KeyError, TypeError, ValueError):
            raise R2CollectionError("authenticated_canary_invalid") from None
        return {"ready": ready, "scope_verified": scope_verified}

    def _application_canary(
        self,
        configuration: R2CollectorConfiguration,
        runtime: Mapping[str, str],
    ) -> dict[str, bool]:
        token = _required(runtime, "SPINE_LOCAL_AUTH_BEARER_CREDENTIAL")
        workspace_id = _required(runtime, "SPINE_WORKSPACE_A_ID")
        environment_id = _required(runtime, "SPINE_ENVIRONMENT_A_ID")
        response = self._http.get(
            url=f"{configuration.api_base_url.rstrip('/')}/api/v1/documents?limit=1",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Workspace-ID": workspace_id,
                "X-Environment-ID": environment_id,
            },
        )
        try:
            body = json.loads(response.body)
            scope_verified = (
                body["workspace_id"] == workspace_id
                and body["environment_id"] == environment_id
            )
            passed = response.status_code == 200 and isinstance(body["items"], list)
        except (KeyError, TypeError, ValueError):
            raise R2CollectionError("authenticated_canary_invalid") from None
        return {"passed": passed, "scope_verified": scope_verified}

    def _invalid_credential_denied(
        self,
        configuration: R2CollectorConfiguration,
        runtime: Mapping[str, str],
    ) -> bool:
        response = self._http.get(
            url=f"{configuration.api_base_url.rstrip('/')}/api/v1/health/summary",
            headers={
                "Authorization": "Bearer r2-invalid-credential",
                "X-Workspace-ID": _required(runtime, "SPINE_WORKSPACE_A_ID"),
                "X-Environment-ID": _required(runtime, "SPINE_ENVIRONMENT_A_ID"),
            },
        )
        return response.status_code in {401, 403}

    def _foreign_scope_denied(
        self,
        configuration: R2CollectorConfiguration,
        runtime: Mapping[str, str],
    ) -> bool:
        response = self._http.get(
            url=f"{configuration.api_base_url.rstrip('/')}/api/v1/health/summary",
            headers={
                "Authorization": (
                    "Bearer "
                    + _required(runtime, "SPINE_LOCAL_AUTH_BEARER_CREDENTIAL")
                ),
                "X-Workspace-ID": "00000000-0000-4000-8000-000000000001",
                "X-Environment-ID": "00000000-0000-4000-8000-000000000002",
            },
        )
        return response.status_code == 403

    def _web_reachable(self, configuration: R2CollectorConfiguration) -> bool:
        response = self._http.get(
            url=f"{configuration.web_base_url.rstrip('/')}/",
            headers={},
        )
        return response.status_code == 200

    def _components(
        self,
        prefix: tuple[str, ...],
        configuration: R2CollectorConfiguration,
        *,
        approved_provider_binding: _ApprovedProviderBinding,
        web_reachable: bool,
    ) -> tuple[R2ComponentObservation, ...]:
        ps_output = self._require_success(
            (*prefix, "ps", "--all", "--format", "json"),
            "compose_inventory_failed",
        )
        inventory = _compose_inventory(ps_output)
        web_fallback = "web-fixtures" in inventory
        inspected: dict[str, dict[str, object]] = {}
        for service in set(_SERVICE_BY_ROLE.values()):
            item = inventory.get(service)
            if item is None:
                continue
            name = str(item.get("Name") or item.get("name") or "")
            output = self._require_success(
                ("docker", "inspect", name), "container_inspection_failed"
            )
            try:
                inspected[service] = json.loads(output)[0]
            except (IndexError, KeyError, TypeError, ValueError):
                raise R2CollectionError("container_inspection_invalid") from None

        observations: list[R2ComponentObservation] = []
        for role, service in _SERVICE_BY_ROLE.items():
            item = inventory.get(service)
            inspection = inspected.get(service)
            if item is None or inspection is None:
                continue
            config = inspection.get("Config") or {}
            state = inspection.get("State") or {}
            image = str(inspection.get("Image") or "")
            image_digest = image.removeprefix("sha256:")
            if len(image_digest) != 64:
                raise R2CollectionError("container_image_digest_invalid")
            environment = _container_environment(config)
            networks = tuple(
                sorted(
                    ((inspection.get("NetworkSettings") or {}).get("Networks") or {})
                )
            )
            configured = _component_configured(
                role=role,
                environment=environment,
                inspected=inspected,
                dependencies=_DEPENDENCIES[role],
                networks=networks,
                expected_head_sha=configuration.head_sha,
                expected_fingerprint=configuration.deployment_fingerprint,
                approved_provider_binding=approved_provider_binding.raw_json,
            )
            running = bool(state.get("Running"))
            exit_code = state.get("ExitCode")
            reachable = (
                exit_code == 0 if role == "original_storage" else running
            )
            if role == "web":
                reachable = reachable and web_reachable
            safe_observation = {
                "role": role,
                "service": service,
                "image": image,
                "networks": networks,
                "running": running,
                "exit_code": exit_code,
                "configured": configured,
            }
            if role == "selected_provider":
                safe_observation["provider_profile_digest"] = (
                    approved_provider_binding.configuration.profile.profile_digest
                )
            observation_digest = hashlib.sha256(
                json.dumps(
                    safe_observation,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                ).encode("ascii")
            ).hexdigest()
            version = str(config.get("Image") or item.get("Image") or role)
            component_id = f"spine-{role.replace('_', '-')}"
            if role == "selected_provider":
                profile = approved_provider_binding.configuration.profile
                component_id = profile.profile_id
                version = f"{profile.profile_id}@{profile.profile_digest}"
            observations.append(
                R2ComponentObservation(
                    role=role,
                    component_id=component_id,
                    version=version,
                    artifact_digest=image_digest,
                    observation_digest=observation_digest,
                    deployment_fingerprint=configuration.deployment_fingerprint,
                    dependencies=_DEPENDENCIES[role],
                    configured=configured,
                    reachable=reachable,
                    fallback_selected=(role == "web" and web_fallback),
                )
            )
        return tuple(observations)


def _deployment_summary_ready(data: object) -> bool:
    if not isinstance(data, Mapping):
        return False
    authorization = data.get("authorization")
    publication = data.get("publication")
    ingestion = data.get("ingestion")
    if not all(
        isinstance(component, Mapping)
        for component in (authorization, publication, ingestion)
    ):
        return False
    authorization_ready = (
        authorization.get("status") == "ready"
        and authorization.get("code")
        in {"authorization.ready", "local.ready", "oidc.ready"}
    )
    ingestion_ready = (
        ingestion.get("status") == "ready"
        and ingestion.get("code") == "ingestion.ready"
    )
    publication_ready = (
        publication.get("status") == "ready"
        and publication.get("code") == "publication.ready"
        and data.get("status") == "ready"
    )
    publication_unpublished = (
        publication.get("status") == "degraded"
        and publication.get("code") == "publication.unpublished"
        and data.get("status") == "degraded"
    )
    return bool(
        authorization_ready
        and ingestion_ready
        and (publication_ready or publication_unpublished)
    )


def _compose_prefix(configuration: R2CollectorConfiguration) -> tuple[str, ...]:
    return (
        "docker",
        "compose",
        "--env-file",
        str(configuration.runtime_env_path),
        "--env-file",
        str(configuration.qualification_env_path),
        "-p",
        configuration.compose_project,
    )


def _load_env(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise R2CollectionError("runtime_configuration_unavailable")
    values = dotenv_values(path, interpolate=False)
    return {key: value for key, value in values.items() if value is not None}


def _required(values: Mapping[str, str], key: str) -> str:
    value = values.get(key)
    if not value:
        raise R2CollectionError(f"runtime_setting_missing:{key}")
    return value


def _validate_configured_target(
    runtime: Mapping[str, str],
    qualification: Mapping[str, str],
    configuration: R2CollectorConfiguration,
    *,
    observed_at: datetime,
) -> _ApprovedProviderBinding:
    try:
        profile = json.loads(_required(runtime, "SPINE_QA_DEPLOYMENT_SETTINGS_JSON"))
    except (TypeError, ValueError):
        raise R2CollectionError("deployment_profile_invalid") from None
    if profile.get("commit_sha") != configuration.head_sha:
        raise R2CollectionError("target_commit_mismatch")
    if profile.get("deployment_fingerprint") != configuration.deployment_fingerprint:
        raise R2CollectionError("deployment_fingerprint_mismatch")
    if configuration.environment is not EnvironmentKind.DEVELOPMENT:
        raise R2CollectionError("deployment_environment_mismatch")
    binding_json = _required(runtime, "SPINE_COGNEE_WORKER_BINDING_JSON")
    try:
        binding = CogneeWorkerBindingSettings(
            binding_json=binding_json
        ).load_configuration()
    except CogneeWorkerConfigurationError as error:
        raise R2CollectionError(
            f"provider_binding_invalid:{error.reason_code}"
        ) from None

    expected_target = f"deployment:source:{configuration.deployment_fingerprint}"
    if (
        _required(qualification, "SPINE_QUALIFICATION_TARGET_REFERENCE")
        != expected_target
    ):
        raise R2CollectionError("qualification_target_mismatch")
    authorization_json = _required(
        qualification, "SPINE_COGNEE_LOCAL_PROVIDER_AUTHORIZATION_JSON"
    )
    try:
        authorization = LocalCogneeProviderAuthorizationSettings(
            authorization_json=authorization_json
        ).load_optional()
    except LocalCogneeProviderAuthorizationError as error:
        raise R2CollectionError(
            f"provider_authorization_invalid:{error.reason_code}"
        ) from None
    if authorization is None:
        raise R2CollectionError("provider_authorization_missing")
    required_references = frozenset(
        (
            binding.profile.postgres_secret_ref,
            binding.profile.neo4j_secret_ref,
            binding.profile.model_secret_ref,
        )
    )
    try:
        build_local_cognee_provider_guards(
            profile=binding.profile,
            authorization=authorization,
            available_secret_references=required_references,
            clock=lambda: observed_at,
            provider_usage_meter_registrar=lambda _meter: None,
        )
    except LocalCogneeProviderAuthorizationError as error:
        raise R2CollectionError(
            f"provider_authorization_invalid:{error.reason_code}"
        ) from None
    authorizations = (
        authorization.authorizations
        if isinstance(authorization, LocalCogneeProviderAuthorizationSet)
        else (authorization,)
    )
    if not _has_active_scope_authorization(
        authorizations=authorizations,
        binding=binding,
        workspace_id=_required(runtime, "SPINE_WORKSPACE_A_ID"),
        environment_id=_required(runtime, "SPINE_ENVIRONMENT_A_ID"),
        observed_at=observed_at,
    ):
        raise R2CollectionError("provider_authorization_scope_mismatch")
    return _ApprovedProviderBinding(
        raw_json=binding_json,
        configuration=binding,
    )


def _has_active_scope_authorization(
    *,
    authorizations: tuple[LocalCogneeProviderAuthorization, ...],
    binding: CogneeWorkerBindingConfiguration,
    workspace_id: str,
    environment_id: str,
    observed_at: datetime,
) -> bool:
    return any(
        item.profile_id == binding.profile.profile_id
        and item.profile_digest == binding.profile.profile_digest
        and str(item.workspace_id) == workspace_id
        and str(item.environment_id) == environment_id
        and item.expires_at > observed_at
        for item in authorizations
    )


def _compose_inventory(output: str) -> dict[str, dict[str, object]]:
    try:
        stripped = output.strip()
        records = json.loads(stripped) if stripped.startswith("[") else [
            json.loads(line) for line in stripped.splitlines() if line.strip()
        ]
        return {
            str(item.get("Service") or item.get("service")): item for item in records
        }
    except (AttributeError, TypeError, ValueError):
        raise R2CollectionError("compose_inventory_invalid") from None


def _container_environment(config: object) -> dict[str, str]:
    if not isinstance(config, dict):
        return {}
    values = config.get("Env") or []
    return {
        key: value
        for item in values
        if isinstance(item, str) and "=" in item
        for key, value in (item.split("=", 1),)
    }


def _component_configured(
    *,
    role: str,
    environment: Mapping[str, str],
    inspected: Mapping[str, Mapping[str, object]],
    dependencies: tuple[str, ...],
    networks: tuple[str, ...],
    expected_head_sha: str,
    expected_fingerprint: str,
    approved_provider_binding: str,
) -> bool:
    dependency_services = (_SERVICE_BY_ROLE[item] for item in dependencies)
    for service in dependency_services:
        dependency = inspected.get(service)
        dependency_networks = set(
            ((dependency or {}).get("NetworkSettings") or {}).get("Networks") or {}
        )
        if not set(networks).intersection(dependency_networks):
            return False
    if role in {"api", "qa_worker", "outbox_dispatcher"}:
        try:
            profile = json.loads(environment["SPINE_QA_DEPLOYMENT_SETTINGS_JSON"])
        except (KeyError, TypeError, ValueError):
            return False
        if (
            profile.get("commit_sha") != expected_head_sha
            or profile.get("deployment_fingerprint") != expected_fingerprint
        ):
            return False
    if role == "selected_provider":
        dispatcher = inspected.get("outbox-dispatcher") or {}
        dispatcher_env = _container_environment(dispatcher.get("Config"))
        if (
            dispatcher_env.get("SPINE_COGNEE_WORKER_BINDING_JSON")
            != approved_provider_binding
        ):
            return False
    return True
