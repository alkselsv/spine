"""Concrete Yandex adapter for the metadata-only qualification inventory port."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import asyncio
import random

from pydantic import SecretStr

from spine.application.connections.provider import ProviderReadDeniedError
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
from spine.application.yandex_qualification_credentials import (
    YANDEX_QUALIFICATION_CLIENT_ID,
    YANDEX_QUALIFICATION_ROOT,
    YandexQualificationCredentialBinding,
)
from spine.application.yandex_qualification_inventory import (
    InventoryFailureCode,
    YandexQualificationInventoryError,
    YandexQualificationInventoryProbeResult,
)
from spine.connectors.yandex_disk import YandexDiskCredentialResolver
from spine.connectors.yandex_disk_api import (
    YandexDiskApiRequest,
    YandexDiskApiResponse,
    YandexDiskApiService,
    YandexDiskApiTransport,
    YandexQualificationApiBinding,
    YandexQualificationRequestGovernor,
    YandexQualificationRequestPolicy,
)
from spine.connectors.yandex_disk_inventory import YandexQualificationInventoryReader
from spine.domain.connections.provider import OpaqueProviderReference, ProviderConnection


@dataclass(frozen=True, slots=True)
class YandexDiskQualificationInventoryProbe:
    credential_resolver: YandexDiskCredentialResolver
    transport: YandexDiskApiTransport
    request_governor: YandexQualificationRequestGovernor
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    jitter: Callable[[float], float] = lambda ceiling: random.SystemRandom().uniform(
        0.0, ceiling
    )

    async def read(
        self,
        connection: ProviderConnection,
        resource_scope_reference: OpaqueProviderReference,
        *,
        credential_binding: YandexQualificationCredentialBinding,
        expected_user_id: SecretStr,
        revalidate: Callable[[], Awaitable[None]],
    ) -> YandexQualificationInventoryProbeResult:
        policy = YandexQualificationRequestPolicy()
        counting_transport = _CountingTransport(
            self.transport,
            max_response_bytes=policy.max_scan_bytes,
            max_requests=policy.max_requests_per_logical_scan,
        )
        service = YandexDiskApiService(
            binding=_qualification_binding(
                credential_binding,
                connection,
                resource_scope_reference,
                expected_user_id,
            ),
            credential_resolver=self.credential_resolver,
            transport=counting_transport,
            request_policy=policy,
            request_governor=self.request_governor,
        )
        try:
            inventory = await YandexQualificationInventoryReader(
                service=service,
                approved_root=YANDEX_QUALIFICATION_ROOT,
                page_size=policy.max_items,
                max_pages=policy.max_pages,
                max_resources=policy.max_items,
                max_attempts=policy.max_request_attempts,
                max_retry_requests=policy.max_retry_requests,
                backoff_ceilings=policy.retry_backoff_ceilings,
                sleep=self.sleep,
                jitter=self.jitter,
                revalidate=revalidate,
            ).read(connection, resource_scope_reference)
        except ProviderReadDeniedError:
            raise
        except YandexQualificationInventoryError as exc:
            raise YandexQualificationInventoryError(
                exc.code,
                identity_requests=counting_transport.identity_requests,
                disk_requests=counting_transport.disk_requests,
            ) from None
        except Exception as exc:
            raise YandexQualificationInventoryError(
                _provider_failure_code(exc),
                identity_requests=counting_transport.identity_requests,
                disk_requests=counting_transport.disk_requests,
            ) from None
        return YandexQualificationInventoryProbeResult(
            inventory=inventory,
            identity_requests=counting_transport.identity_requests,
            disk_requests=counting_transport.disk_requests,
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
    return "contract_rejected"


__all__ = ["YandexDiskQualificationInventoryProbe"]
