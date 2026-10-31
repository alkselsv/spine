"""Approved qualification composition for the governed Yandex Disk reader."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

from spine.application.connections.provider import (
    ProviderConnectionRepository,
    ProviderReadAdmission,
)
from spine.connectors.yandex_disk import (
    YandexDiskCredentialResolver,
    YandexDiskProviderAdapter,
    YandexDiskReadProfile,
)
from spine.connectors.yandex_disk_api import (
    YandexDiskApiService,
    YandexDiskApiTransport,
    YandexQualificationApiBinding,
    YandexQualificationRequestPolicy,
)


@dataclass(frozen=True, slots=True)
class YandexQualificationReaderComposition:
    """One adapter graph for qualification reads and full reconciliation scans."""

    admission: ProviderReadAdmission
    provider: YandexDiskProviderAdapter
    service: YandexDiskApiService
    read_profile: YandexDiskReadProfile


def compose_yandex_qualification_reader(
    *,
    binding: YandexQualificationApiBinding,
    credential_resolver: YandexDiskCredentialResolver,
    transport: YandexDiskApiTransport,
    connection_repository: ProviderConnectionRepository,
    clock: Callable[[], datetime],
    sleep: Callable[[float], Awaitable[None]],
    jitter: Callable[[float], float] | None = None,
) -> YandexQualificationReaderComposition:
    """Wire the concrete API only through the admitted provider adapter."""

    request_policy = YandexQualificationRequestPolicy()
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
        sleep=sleep,
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
        ),
        provider=provider,
        service=service,
        read_profile=read_profile,
    )


__all__ = [
    "YandexQualificationReaderComposition",
    "compose_yandex_qualification_reader",
]
