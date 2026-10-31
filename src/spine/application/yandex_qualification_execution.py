"""Application lease contracts for isolated Yandex qualification execution."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from spine.domain.connections.provider import OpaqueProviderReference


class YandexQualificationExecutionDeniedError(RuntimeError):
    """The exact qualification execution scope could not obtain its lease."""

    def __init__(self) -> None:
        super().__init__("Yandex qualification execution denied.")


@dataclass(frozen=True, slots=True)
class YandexQualificationExecutionScope:
    workspace_id: UUID
    environment_id: UUID
    connection_id: UUID
    connection_version_id: UUID
    resource_scope_reference: OpaqueProviderReference
    authorization_generation: int
    configuration_generation: int


class YandexQualificationExecutionLease(Protocol):
    """Hold exclusive authority for one connection/scope/generation execution."""

    def hold(
        self, scope: YandexQualificationExecutionScope
    ) -> AbstractAsyncContextManager[None]: ...


__all__ = [
    "YandexQualificationExecutionDeniedError",
    "YandexQualificationExecutionLease",
    "YandexQualificationExecutionScope",
]
