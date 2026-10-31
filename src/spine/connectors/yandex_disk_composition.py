"""Approved qualification composition for the governed Yandex Disk reader."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
import time

from spine.application.connections.provider import (
    ProviderConnectionRepository,
    ProviderReadAdmission,
    ProviderReadCommand,
)
from spine.application.lifecycle import CurrentLifecycleEligibilityReader
from spine.application.yandex_disk_reconciliation import AuthorizedYandexDiskScanner
from spine.connectors.yandex_disk import (
    YandexDiskCredentialResolver,
    YandexDiskProviderAdapter,
    YandexDiskReadProfile,
)
from spine.connectors.yandex_disk_api import (
    YandexDiskApiService,
    YandexDiskApiTransport,
    YandexQualificationApiBinding,
    YandexQualificationRequestGovernor,
    YandexQualificationRequestPolicy,
)
from spine.domain.connections.provider import ProviderConnection, ProviderKind
from spine.domain.sources.lifecycle import (
    SourceExternalIdentity,
    yandex_disk_source_identity,
)


@dataclass(frozen=True, slots=True)
class YandexDiskSourceIdentityResolver:
    """Resolve a Yandex path identity before content is downloaded."""

    def resolve_source_identity(
        self, connection: ProviderConnection, command: ProviderReadCommand
    ) -> SourceExternalIdentity | None:
        if (
            connection.provider_kind is not ProviderKind.YANDEX_DISK
            or command.resource_object_reference is None
        ):
            return None
        return yandex_disk_source_identity(
            connection.connection_id,
            command.resource_object_reference,
            command.external_identity_occurrence,
        )


@dataclass(frozen=True, slots=True)
class YandexQualificationReaderComposition:
    """One adapter graph for qualification reads and full reconciliation scans."""

    admission: ProviderReadAdmission
    reconciliation_scanner: AuthorizedYandexDiskScanner
    provider: YandexDiskProviderAdapter
    service: YandexDiskApiService
    read_profile: YandexDiskReadProfile
    request_governor: YandexQualificationRequestGovernor


def compose_yandex_qualification_reader(
    *,
    binding: YandexQualificationApiBinding,
    credential_resolver: YandexDiskCredentialResolver,
    transport: YandexDiskApiTransport,
    connection_repository: ProviderConnectionRepository,
    lifecycle_reader: CurrentLifecycleEligibilityReader,
    clock: Callable[[], datetime],
    sleep: Callable[[float], Awaitable[None]],
    monotonic: Callable[[], float] = time.monotonic,
    jitter: Callable[[float], float] | None = None,
) -> YandexQualificationReaderComposition:
    """Wire the concrete API only through the admitted provider adapter."""

    request_policy = YandexQualificationRequestPolicy()
    request_governor = YandexQualificationRequestGovernor(
        policy=request_policy,
        monotonic=monotonic,
        sleep=sleep,
    )
    read_profile = YandexDiskReadProfile(
        page_size=request_policy.max_items,
        max_attempts=request_policy.max_request_attempts,
        max_file_bytes=request_policy.max_file_bytes,
        max_pages=request_policy.max_pages,
        max_items=request_policy.max_items,
        backoff_seconds=request_policy.retry_backoff_ceilings,
    )
    service = YandexDiskApiService(
        binding=binding,
        credential_resolver=credential_resolver,
        transport=transport,
        request_policy=request_policy,
        request_governor=request_governor,
    )
    if jitter is None:
        provider = YandexDiskProviderAdapter(
            service=service,
            profile=read_profile,
            clock=clock,
            sleep=sleep,
        )
    else:
        provider = YandexDiskProviderAdapter(
            service=service,
            profile=read_profile,
            clock=clock,
            sleep=sleep,
            jitter=jitter,
        )
    return YandexQualificationReaderComposition(
        admission=ProviderReadAdmission(
            repository=connection_repository,
            provider=provider,
            lifecycle_reader=lifecycle_reader,
            source_identity_resolvers=(YandexDiskSourceIdentityResolver(),),
        ),
        reconciliation_scanner=AuthorizedYandexDiskScanner(
            repository=connection_repository,
            provider=provider,
            lifecycle_reader=lifecycle_reader,
        ),
        provider=provider,
        service=service,
        read_profile=read_profile,
        request_governor=request_governor,
    )


__all__ = [
    "YandexDiskSourceIdentityResolver",
    "YandexQualificationReaderComposition",
    "compose_yandex_qualification_reader",
]
