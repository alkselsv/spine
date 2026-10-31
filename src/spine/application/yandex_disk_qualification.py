"""Metadata-only application use case for isolated Yandex qualification."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import random
from typing import Literal
from collections.abc import Awaitable, Callable
from uuid import UUID

from pydantic import Field, SecretStr, StrictBool, StrictInt, model_validator

from spine.application.yandex_disk_scan import (
    YandexDiskAuthorizationRevokedError,
    YandexDiskContractError,
    YandexDiskFileTooLargeError,
    YandexDiskPermissionLostError,
    YandexDiskProviderUnavailableError,
    YandexDiskResourceNotFoundError,
    YandexDiskScopeOutOfScopeError,
    YandexDiskThrottleError,
    YandexDiskTransientError,
)
from spine.application.connections.provider import (
    ProviderConnectionRepository,
    ProviderReadCommand,
    ProviderReadDeniedError,
)
from spine.application.persistence.context import TrustedPersistenceContext
from spine.application.private_qualification_artifacts import (
    PrivateQualificationArtifactWriter,
)
from spine.application.yandex_qualification_execution import (
    YandexQualificationExecutionDeniedError,
    YandexQualificationExecutionLease,
    YandexQualificationExecutionScope,
)
from spine.connectors.yandex_disk import (
    YandexDiskCredentialResolver,
)
from spine.connectors.yandex_disk_inventory import (
    InventoryFailureCode,
    YandexQualificationInventory,
    YandexQualificationInventoryError,
    YandexQualificationInventoryReader,
)
from spine.connectors.yandex_disk_api import (
    YandexDiskApiRequest,
    YandexDiskApiResponse,
    YandexDiskApiService,
    YandexDiskApiTransport,
    YandexQualificationApiBinding,
    YandexQualificationRequestGovernor,
    YandexQualificationRequestPolicy,
)
from spine.domain.common import DefinitionModel
from spine.domain.connections.provider import (
    OpaqueProviderReference,
    ProviderConnection,
    ProviderCapabilityKind,
    ProviderKind,
    ProviderConnectionStatus,
)
from spine.application.yandex_qualification_credentials import (
    YANDEX_QUALIFICATION_CLIENT_ID,
    YANDEX_QUALIFICATION_ROOT,
    YandexQualificationCredentialBinding,
)


class YandexQualificationInventoryConfig(DefinitionModel):
    schema_version: Literal["yandex-qualification-inventory-config:v1"] = (
        "yandex-qualification-inventory-config:v1"
    )
    opt_in: StrictBool
    credential_binding: YandexQualificationCredentialBinding
    connection_id: UUID
    connection_version_id: UUID
    resource_scope_reference: OpaqueProviderReference
    authorization_generation: StrictInt = Field(ge=1)
    configuration_generation: StrictInt = Field(ge=1)
    security_approval_reference: Literal["issue-701-comment-6539550358"]
    data_approval_reference: Literal["issue-702-comment-6539550684"]
    fixture_manifest_version: Literal["spine-yandex-qualification-fixtures-v1"]
    scope_version: Literal["spine-yandex-qualification-scope:v1"]
    approved_manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_file_count: Literal[5] = 5
    expected_directory_count: Literal[4] = 4
    expected_file_bytes: Literal[1416] = 1416

    @model_validator(mode="after")
    def validate_connection_authority(self) -> "YandexQualificationInventoryConfig":
        if self.connection_id.int == 0 or self.connection_version_id.int == 0:
            raise ValueError("qualification connection selectors must be non-nil")
        return self


class YandexQualificationInventoryReport(DefinitionModel):
    schema_version: Literal["yandex-qualification-inventory-report:v1"] = (
        "yandex-qualification-inventory-report:v1"
    )
    status: Literal["ready_for_manifest_approval", "not_verified"]
    fixture_manifest_version: Literal["spine-yandex-qualification-fixtures-v1"]
    scope_version: Literal["spine-yandex-qualification-scope:v1"]
    provider_api_version: Literal["yandex-disk-rest:v1"] = "yandex-disk-rest:v1"
    request_policy_version: Literal["spine-yandex-disk-qualification-ops:v1"] = (
        "spine-yandex-disk-qualification-ops:v1"
    )
    workspace_id: UUID
    environment_id: UUID
    connection_id: UUID
    connection_version_id: UUID
    authorization_generation: StrictInt = Field(ge=1)
    configuration_generation: StrictInt = Field(ge=1)
    acting_identity_fingerprint: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    manifest_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    file_count: StrictInt = Field(ge=0)
    directory_count: StrictInt = Field(ge=0)
    file_bytes: StrictInt = Field(ge=0)
    provider_pages_per_pass: StrictInt = Field(ge=0)
    identity_requests: StrictInt = Field(ge=0)
    disk_requests: StrictInt = Field(ge=0)
    inventory_passes: Literal[0, 2]
    failure_code: InventoryFailureCode | None = None
    manifest_candidate_written: StrictBool
    credentials_recorded: Literal[False] = False
    provider_payloads_recorded: Literal[False] = False
    protected_paths_or_names_in_report: Literal[False] = False

    @model_validator(mode="after")
    def validate_outcome(self) -> "YandexQualificationInventoryReport":
        passed = self.status == "ready_for_manifest_approval"
        if passed != self.manifest_candidate_written:
            raise ValueError("candidate status must match inventory outcome")
        if passed != (self.failure_code is None):
            raise ValueError("failure code must exist only for failed inventory")
        if passed != (
            self.acting_identity_fingerprint is not None
            and self.manifest_digest is not None
        ):
            raise ValueError("successful inventory requires opaque evidence digests")
        return self


class YandexQualificationInventoryRunner:
    """Run the metadata-only phase and emit a private manifest candidate."""

    def __init__(
        self,
        *,
        config: YandexQualificationInventoryConfig,
        expected_user_id: SecretStr,
        credential_resolver: YandexDiskCredentialResolver,
        transport: YandexDiskApiTransport,
        request_governor: YandexQualificationRequestGovernor,
        connection_repository: ProviderConnectionRepository,
        execution_lease: YandexQualificationExecutionLease,
        artifact_writer: PrivateQualificationArtifactWriter,
        context: TrustedPersistenceContext,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[float], float] = lambda ceiling: random.SystemRandom().uniform(
            0.0, ceiling
        ),
    ) -> None:
        self.config = config
        self._expected_user_id = expected_user_id
        self._credential_resolver = credential_resolver
        self._transport = transport
        self._request_governor = request_governor
        self._connection_repository = connection_repository
        self._execution_lease = execution_lease
        self._artifact_writer = artifact_writer
        self._context = context
        self._sleep = sleep
        self._jitter = jitter

    async def run(
        self, *, environment_opt_in: bool, candidate_path: Path
    ) -> YandexQualificationInventoryReport:
        if not self.config.opt_in or environment_opt_in is not True:
            return self._failed("prerequisite_denied")
        command = ProviderReadCommand(
            workspace_id=self.config.credential_binding.workspace_id,
            environment_id=self.config.credential_binding.environment_id,
            connection_id=self.config.connection_id,
            connection_version_id=self.config.connection_version_id,
            resource_scope_reference=self.config.resource_scope_reference,
            authorization_generation=self.config.authorization_generation,
            configuration_generation=self.config.configuration_generation,
            required_capabilities=(
                ProviderCapabilityKind.ENUMERATION,
                ProviderCapabilityKind.METADATA_READ,
                ProviderCapabilityKind.FULL_RECONCILIATION,
            ),
        )
        try:
            async with self._connection_repository.admit_read(
                command, self._context
            ) as connection:
                _validate_admitted_connection(command, connection)
                scope = YandexQualificationExecutionScope(
                    workspace_id=command.workspace_id,
                    environment_id=command.environment_id,
                    connection_id=command.connection_id,
                    connection_version_id=command.connection_version_id,
                    resource_scope_reference=command.resource_scope_reference,
                    authorization_generation=command.authorization_generation,
                    configuration_generation=command.configuration_generation,
                )
                async with self._execution_lease.hold(scope):
                    async def revalidate() -> None:
                        async with self._connection_repository.admit_read(
                            command, self._context
                        ) as current_connection:
                            _validate_admitted_connection(command, current_connection)

                    return await self._run_admitted(
                        connection,
                        candidate_path,
                        revalidate=revalidate,
                    )
        except asyncio.CancelledError:
            return self._failed("cancelled")
        except (ProviderReadDeniedError, YandexQualificationExecutionDeniedError):
            return self._failed("prerequisite_denied")

    async def _run_admitted(
        self,
        connection: ProviderConnection,
        candidate_path: Path,
        *,
        revalidate: Callable[[], Awaitable[None]],
    ) -> YandexQualificationInventoryReport:
        binding = _qualification_binding(
            self.config.credential_binding,
            connection,
            self.config.resource_scope_reference,
            self._expected_user_id,
        )
        policy = YandexQualificationRequestPolicy()
        counting_transport = _CountingTransport(
            self._transport,
            max_response_bytes=policy.max_scan_bytes,
            max_requests=policy.max_requests_per_logical_scan,
        )
        service = YandexDiskApiService(
            binding=binding,
            credential_resolver=self._credential_resolver,
            transport=counting_transport,
            request_policy=policy,
            request_governor=self._request_governor,
        )
        try:
            inventory = await YandexQualificationInventoryReader(
                service=service,
                approved_root=binding.approved_root,
                page_size=policy.max_items,
                max_pages=policy.max_pages,
                max_resources=policy.max_items,
                max_attempts=policy.max_request_attempts,
                max_retry_requests=policy.max_retry_requests,
                backoff_ceilings=policy.retry_backoff_ceilings,
                sleep=self._sleep,
                jitter=self._jitter,
                revalidate=revalidate,
            ).read(connection, binding.resource_scope_reference)
        except asyncio.CancelledError:
            return self._failed("cancelled", counts=counting_transport)
        except YandexQualificationInventoryError as exc:
            return self._failed(exc.code, counts=counting_transport)
        except ProviderReadDeniedError:
            raise
        except Exception as exc:
            return self._failed(
                _provider_failure_code(exc), counts=counting_transport
            )

        counts = (
            inventory.file_count,
            inventory.directory_count,
            inventory.file_bytes,
        )
        expected = (
            self.config.expected_file_count,
            self.config.expected_directory_count,
            self.config.expected_file_bytes,
        )
        if counts != expected:
            return self._failed(
                "manifest_mismatch",
                inventory=inventory,
                counts=counting_transport,
            )
        manifest_digest = _inventory_digest(inventory)
        if manifest_digest != self.config.approved_manifest_digest:
            return self._failed(
                "manifest_mismatch",
                inventory=inventory,
                counts=counting_transport,
            )
        try:
            _write_inventory_candidate(
                candidate_path,
                self.config,
                inventory,
                artifact_writer=self._artifact_writer,
            )
        except OSError:
            return self._failed(
                "candidate_write_denied",
                inventory=inventory,
                counts=counting_transport,
            )
        return YandexQualificationInventoryReport(
            status="ready_for_manifest_approval",
            fixture_manifest_version=self.config.fixture_manifest_version,
            scope_version=self.config.scope_version,
            workspace_id=self.config.credential_binding.workspace_id,
            environment_id=self.config.credential_binding.environment_id,
            connection_id=self.config.connection_id,
            connection_version_id=self.config.connection_version_id,
            authorization_generation=self.config.authorization_generation,
            configuration_generation=self.config.configuration_generation,
            acting_identity_fingerprint=hashlib.sha256(
                self._expected_user_id.get_secret_value().encode("utf-8")
            ).hexdigest(),
            manifest_digest=manifest_digest,
            file_count=inventory.file_count,
            directory_count=inventory.directory_count,
            file_bytes=inventory.file_bytes,
            provider_pages_per_pass=inventory.provider_pages,
            identity_requests=counting_transport.identity_requests,
            disk_requests=counting_transport.disk_requests,
            inventory_passes=2,
            manifest_candidate_written=True,
        )

    def _failed(
        self,
        code: InventoryFailureCode,
        *,
        inventory: YandexQualificationInventory | None = None,
        counts: "_CountingTransport | None" = None,
    ) -> YandexQualificationInventoryReport:
        return YandexQualificationInventoryReport.model_validate(
            {
                "status": "not_verified",
                "fixture_manifest_version": self.config.fixture_manifest_version,
                "scope_version": self.config.scope_version,
                "workspace_id": self.config.credential_binding.workspace_id,
                "environment_id": self.config.credential_binding.environment_id,
                "connection_id": self.config.connection_id,
                "connection_version_id": self.config.connection_version_id,
                "authorization_generation": self.config.authorization_generation,
                "configuration_generation": self.config.configuration_generation,
                "file_count": inventory.file_count if inventory else 0,
                "directory_count": inventory.directory_count if inventory else 0,
                "file_bytes": inventory.file_bytes if inventory else 0,
                "provider_pages_per_pass": inventory.provider_pages if inventory else 0,
                "identity_requests": counts.identity_requests if counts else 0,
                "disk_requests": counts.disk_requests if counts else 0,
                "inventory_passes": 2 if inventory else 0,
                "failure_code": code,
                "manifest_candidate_written": False,
            }
        )


class _CountingTransport:
    """Count endpoint classes without retaining request or response data."""

    def __init__(
        self,
        transport: YandexDiskApiTransport,
        *,
        max_response_bytes: int,
        max_requests: int,
    ) -> None:
        self._transport = transport
        self._max_response_bytes = max_response_bytes
        self._max_requests = max_requests
        self._response_bytes = 0
        self.identity_requests = 0
        self.disk_requests = 0

    async def request(self, request: YandexDiskApiRequest) -> YandexDiskApiResponse:
        if self.identity_requests + self.disk_requests >= self._max_requests:
            raise YandexDiskContractError()
        if request.url == "https://login.yandex.ru/info":
            self.identity_requests += 1
        else:
            self.disk_requests += 1
        response = await self._transport.request(request)
        if response.wire_bytes is None or response.wire_bytes < 0:
            raise YandexDiskContractError()
        self._response_bytes += response.wire_bytes
        if self._response_bytes > self._max_response_bytes:
            raise YandexDiskFileTooLargeError()
        return response


def _provider_failure_code(exc: Exception) -> InventoryFailureCode:
    if isinstance(exc, YandexDiskAuthorizationRevokedError):
        return "authorization_revoked"
    if isinstance(exc, YandexDiskPermissionLostError):
        return "permission_lost"
    if isinstance(exc, YandexDiskThrottleError):
        return "throttled"
    if isinstance(exc, (YandexDiskTransientError, YandexDiskProviderUnavailableError)):
        return "provider_unavailable"
    if isinstance(exc, YandexDiskScopeOutOfScopeError):
        return "scope_denied"
    if isinstance(exc, YandexDiskResourceNotFoundError):
        return "resource_not_found"
    if isinstance(exc, YandexDiskFileTooLargeError):
        return "response_byte_limit_reached"
    if isinstance(exc, YandexDiskContractError):
        return "contract_rejected"
    return "contract_rejected"


def _qualification_binding(
    credential_binding: YandexQualificationCredentialBinding,
    connection: ProviderConnection,
    resource_scope_reference: OpaqueProviderReference,
    expected_user_id: SecretStr,
) -> YandexQualificationApiBinding:
    return YandexQualificationApiBinding(
        schema_version="yandex-qualification-api-binding:v1",
        deployment_mode="qualification",
        workspace_id=connection.workspace_id,
        environment_id=connection.environment_id,
        connection_id=connection.connection_id,
        connection_version_id=connection.connection_version_id,
        credential_reference=connection.credential_reference,
        grant_reference=connection.grant_reference,
        resource_scope_reference=resource_scope_reference,
        authorization_generation=connection.authorization_generation,
        configuration_generation=connection.configuration_generation,
        external_secret_reference=credential_binding.reference,
        expected_user_id=expected_user_id,
        client_id=YANDEX_QUALIFICATION_CLIENT_ID,
        approved_root=YANDEX_QUALIFICATION_ROOT,
    )


def _validate_admitted_connection(
    command: ProviderReadCommand, connection: ProviderConnection
) -> None:
    if (
        connection.status is not ProviderConnectionStatus.ACTIVE
        or connection.provider_kind is not ProviderKind.YANDEX_DISK
        or (
            connection.workspace_id,
            connection.environment_id,
            connection.connection_id,
            connection.connection_version_id,
            connection.authorization_generation,
            connection.configuration_generation,
        )
        != (
            command.workspace_id,
            command.environment_id,
            command.connection_id,
            command.connection_version_id,
            command.authorization_generation,
            command.configuration_generation,
        )
        or command.resource_scope_reference
        not in connection.approved_resource_scope_references
    ):
        raise ProviderReadDeniedError()
    if any(not connection.supports(kind) for kind in command.required_capabilities):
        raise ProviderReadDeniedError()


def _write_inventory_candidate(
    path: Path,
    config: YandexQualificationInventoryConfig,
    inventory: YandexQualificationInventory,
    *,
    artifact_writer: PrivateQualificationArtifactWriter,
) -> None:
    payload = {
        "schema_version": "yandex-qualification-manifest-candidate:v1",
        "fixture_manifest_version": config.fixture_manifest_version,
        "scope_version": config.scope_version,
        "workspace_id": str(config.credential_binding.workspace_id),
        "environment_id": str(config.credential_binding.environment_id),
        "connection_id": str(config.connection_id),
        "connection_version_id": str(config.connection_version_id),
        "authorization_generation": config.authorization_generation,
        "configuration_generation": config.configuration_generation,
        "request_policy_version": "spine-yandex-disk-qualification-ops:v1",
        "approved_manifest_digest": config.approved_manifest_digest,
        "manifest_digest": _inventory_digest(inventory),
        "resources": [item.model_dump(mode="json") for item in inventory.resources],
    }
    artifact_writer.create(
        path,
        (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )


def _inventory_digest(inventory: YandexQualificationInventory) -> str:
    payload = json.dumps(
        [item.model_dump(mode="json") for item in inventory.resources],
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def write_yandex_inventory_report(
    path: Path,
    report: YandexQualificationInventoryReport,
    *,
    artifact_writer: PrivateQualificationArtifactWriter,
) -> None:
    artifact_writer.create(
        path,
        (json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )


def yandex_inventory_not_verified_report(
    config: YandexQualificationInventoryConfig,
    code: InventoryFailureCode,
) -> YandexQualificationInventoryReport:
    """Build a sanitized zero-request result for a missing prerequisite."""

    return YandexQualificationInventoryReport(
        status="not_verified",
        fixture_manifest_version=config.fixture_manifest_version,
        scope_version=config.scope_version,
        workspace_id=config.credential_binding.workspace_id,
        environment_id=config.credential_binding.environment_id,
        connection_id=config.connection_id,
        connection_version_id=config.connection_version_id,
        authorization_generation=config.authorization_generation,
        configuration_generation=config.configuration_generation,
        file_count=0,
        directory_count=0,
        file_bytes=0,
        provider_pages_per_pass=0,
        identity_requests=0,
        disk_requests=0,
        inventory_passes=0,
        failure_code=code,
        manifest_candidate_written=False,
    )


__all__ = [
    "YandexQualificationInventory",
    "YandexQualificationInventoryConfig",
    "YandexQualificationInventoryError",
    "YandexQualificationInventoryReader",
    "YandexQualificationInventoryReport",
    "YandexQualificationInventoryRunner",
    "write_yandex_inventory_report",
    "yandex_inventory_not_verified_report",
]
