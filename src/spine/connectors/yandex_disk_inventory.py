"""Bounded recursive metadata inventory for the Yandex Disk adapter."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
import random
import time
from typing import Literal

from spine.application.yandex_disk_scan import (
    YandexDiskThrottleError,
    YandexDiskTransientError,
)
from spine.application.yandex_qualification_inventory import (
    InventoryFailureCode,
    YandexQualificationInventory,
    YandexQualificationInventoryError,
    YandexQualificationInventoryItem,
)
from spine.connectors.yandex_disk import (
    YandexDiskResource,
    YandexDiskResourcePage,
    YandexDiskService,
)
from spine.domain.connections.provider import (
    OpaqueProviderReference,
    ProviderConnection,
)


async def _skip_revalidation() -> None:
    return None


@dataclass(frozen=True, slots=True)
class YandexQualificationInventoryReader:
    """Traverse the approved root and prove a stable recursive snapshot."""

    service: YandexDiskService
    approved_root: str
    page_size: int = 32
    max_pages: int = 10
    max_resources: int = 32
    max_retry_requests: int = 20
    max_attempts: int = 3
    backoff_ceilings: tuple[float, ...] = (1.0, 2.0)
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    jitter: Callable[[float], float] = lambda ceiling: random.SystemRandom().uniform(
        0.0, ceiling
    )
    revalidate: Callable[[], Awaitable[None]] = _skip_revalidation
    attempt_timeout_seconds: float = 5 * 60
    logical_timeout_seconds: float = 10 * 60
    monotonic: Callable[[], float] = time.monotonic

    def __post_init__(self) -> None:
        if (
            self.max_attempts < 1
            or len(self.backoff_ceilings) != self.max_attempts - 1
            or any(value < 0 for value in self.backoff_ceilings)
            or self.attempt_timeout_seconds <= 0
            or self.logical_timeout_seconds < self.attempt_timeout_seconds
        ):
            raise ValueError("inventory retry profile is invalid")

    async def read(
        self,
        connection: ProviderConnection,
        resource_scope_reference: OpaqueProviderReference,
    ) -> YandexQualificationInventory:
        retry_budget = _RetryBudget(max_requests=self.max_retry_requests)
        inventory_budget = _InventoryBudget(
            max_pages=self.max_pages,
            max_resources=self.max_resources,
        )
        logical_deadline = self.monotonic() + self.logical_timeout_seconds
        attempt_deadline = min(
            logical_deadline,
            self.monotonic() + self.attempt_timeout_seconds,
        )
        first = await self._read_once(
            connection,
            resource_scope_reference,
            retry_budget=retry_budget,
            inventory_budget=inventory_budget,
            attempt_deadline=attempt_deadline,
        )
        confirmation = await self._read_once(
            connection,
            resource_scope_reference,
            retry_budget=retry_budget,
            inventory_budget=inventory_budget,
            attempt_deadline=attempt_deadline,
        )
        if first.resources != confirmation.resources:
            raise YandexQualificationInventoryError("inventory_changed")
        return first

    async def _read_once(
        self,
        connection: ProviderConnection,
        resource_scope_reference: OpaqueProviderReference,
        *,
        retry_budget: _RetryBudget,
        inventory_budget: _InventoryBudget,
        attempt_deadline: float,
    ) -> YandexQualificationInventory:
        root = f"disk:{self.approved_root}"
        pending = [root]
        seen_directories = {root}
        resources: dict[str, YandexDiskResource] = {}
        page_count = 0
        while pending:
            directory = pending.pop(0)
            offset = 0
            while True:
                self._require_before_deadline(attempt_deadline)
                inventory_budget.reserve_page()
                page_count += 1
                page = await self._list_resources(
                    connection,
                    resource_scope_reference,
                    path=directory,
                    offset=offset,
                    limit=self.page_size,
                    retry_budget=retry_budget,
                    deadline=attempt_deadline,
                )
                self._require_before_deadline(attempt_deadline)
                embedded = page.embedded
                if (
                    embedded.path != directory
                    or embedded.offset != offset
                    or embedded.limit != self.page_size
                ):
                    raise YandexQualificationInventoryError("contract_rejected")
                for resource in embedded.items:
                    inventory_budget.observe_resource(resource.path)
                    if resource.path.rsplit("/", 1)[0] != directory:
                        raise YandexQualificationInventoryError("contract_rejected")
                    previous = resources.get(resource.path)
                    if previous is not None and previous != resource:
                        raise YandexQualificationInventoryError("contract_rejected")
                    resources[resource.path] = resource
                    if resource.type == "dir" and resource.path not in seen_directories:
                        seen_directories.add(resource.path)
                        pending.append(resource.path)
                next_offset = offset + len(embedded.items)
                if next_offset >= embedded.total:
                    if next_offset != embedded.total:
                        raise YandexQualificationInventoryError("contract_rejected")
                    break
                if next_offset <= offset:
                    raise YandexQualificationInventoryError("contract_rejected")
                offset = next_offset

        ordered = tuple(
            YandexQualificationInventoryItem.model_validate(
                resources[path].model_dump(mode="python")
            )
            for path in sorted(resources)
        )
        files = tuple(item for item in ordered if item.type == "file")
        return YandexQualificationInventory(
            resources=ordered,
            file_count=len(files),
            directory_count=len(ordered) - len(files),
            file_bytes=sum(item.size or 0 for item in files),
            provider_pages=page_count,
        )

    async def _list_resources(
        self,
        connection: ProviderConnection,
        resource_scope_reference: OpaqueProviderReference,
        *,
        path: str,
        offset: int,
        limit: int,
        retry_budget: _RetryBudget,
        deadline: float,
    ) -> YandexDiskResourcePage:
        for attempt in range(1, self.max_attempts + 1):
            self._require_before_deadline(deadline)
            try:
                remaining = deadline - self.monotonic()
                if remaining <= 0:
                    raise YandexQualificationInventoryError("deadline_exceeded")
                return await asyncio.wait_for(
                    self.service.list_resources(
                        connection,
                        resource_scope_reference,
                        path=path,
                        offset=offset,
                        limit=limit,
                    ),
                    timeout=remaining,
                )
            except TimeoutError:
                raise YandexQualificationInventoryError(
                    "deadline_exceeded"
                ) from None
            except (YandexDiskThrottleError, YandexDiskTransientError) as exc:
                if attempt == self.max_attempts:
                    raise
                retry_budget.reserve(2)
                ceiling = self.backoff_ceilings[attempt - 1]
                delay = self.jitter(ceiling)
                if delay < 0 or delay > ceiling:
                    raise ValueError("inventory retry jitter exceeds its ceiling")
                provider_delay = (
                    exc.retry_after_seconds
                    if isinstance(exc, YandexDiskThrottleError)
                    else 0.0
                )
                retry_delay = max(provider_delay, delay)
                if self.monotonic() + retry_delay >= deadline:
                    raise YandexQualificationInventoryError("deadline_exceeded")
                await self.sleep(retry_delay)
                self._require_before_deadline(deadline)
                await self.revalidate()
        raise AssertionError("bounded inventory retry loop did not terminate")

    def _require_before_deadline(self, deadline: float) -> None:
        if self.monotonic() >= deadline:
            raise YandexQualificationInventoryError("deadline_exceeded")


@dataclass(slots=True)
class _RetryBudget:
    max_requests: int
    used_requests: int = 0

    def reserve(self, requests: int) -> None:
        if requests < 1 or self.used_requests + requests > self.max_requests:
            raise YandexQualificationInventoryError("retry_budget_exhausted")
        self.used_requests += requests


@dataclass(slots=True)
class _InventoryBudget:
    max_pages: int
    max_resources: int
    used_pages: int = 0
    observed_resource_paths: set[str] = field(default_factory=set)

    def reserve_page(self) -> None:
        if self.used_pages >= self.max_pages:
            raise YandexQualificationInventoryError("page_limit_reached")
        self.used_pages += 1

    def observe_resource(self, path: str) -> None:
        self.observed_resource_paths.add(path)
        if len(self.observed_resource_paths) > self.max_resources:
            raise YandexQualificationInventoryError("resource_limit_reached")


__all__ = [
    "InventoryFailureCode",
    "YandexQualificationInventory",
    "YandexQualificationInventoryError",
    "YandexQualificationInventoryReader",
]
