"""Application port for the bounded Yandex qualification inventory probe."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Literal, Protocol

from pydantic import ConfigDict, Field, SecretStr, StrictInt, model_validator

from spine.application.yandex_qualification_credentials import (
    YandexQualificationCredentialBinding,
)
from spine.domain.common import DefinitionModel
from spine.domain.connections.provider import OpaqueProviderReference, ProviderConnection


InventoryFailureCode = Literal[
    "candidate_write_denied",
    "cancelled",
    "contract_rejected",
    "deadline_exceeded",
    "inventory_changed",
    "manifest_mismatch",
    "page_limit_reached",
    "permission_lost",
    "prerequisite_denied",
    "provider_unavailable",
    "resource_limit_reached",
    "resource_not_found",
    "response_byte_limit_reached",
    "retry_budget_exhausted",
    "scope_denied",
    "throttled",
    "authorization_revoked",
]


class YandexQualificationInventoryError(RuntimeError):
    """A content-free refusal to claim a complete recursive inventory."""

    def __init__(
        self,
        code: InventoryFailureCode,
        *,
        identity_requests: int = 0,
        disk_requests: int = 0,
    ) -> None:
        self.code = code
        self.identity_requests = identity_requests
        self.disk_requests = disk_requests
        super().__init__("Yandex qualification inventory is incomplete")


class YandexQualificationInventoryItem(DefinitionModel):
    """Typed provider metadata retained only in the private manifest candidate."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: str = Field(min_length=1, max_length=1024)
    path: str = Field(min_length=1, max_length=2048)
    type: Literal["file", "dir"]
    mime_type: str | None = Field(default=None, min_length=1, max_length=255)
    size: StrictInt | None = Field(default=None, ge=0)
    md5: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    revision: str | None = Field(default=None, min_length=1, max_length=128)
    modified: datetime | None = None
    resource_id: str | None = Field(default=None, min_length=1, max_length=1024)

    @model_validator(mode="after")
    def validate_file_revision_evidence(self) -> "YandexQualificationInventoryItem":
        if self.type == "file" and (
            self.mime_type is None
            or self.size is None
            or self.revision is None
            or self.modified is None
            or (self.sha256 is None and self.md5 is None)
        ):
            raise ValueError("Yandex qualification file metadata is incomplete")
        return self


class YandexQualificationInventory(DefinitionModel):
    """Private exact inventory; callers must not serialize it publicly."""

    schema_version: Literal["yandex-qualification-inventory:v1"] = (
        "yandex-qualification-inventory:v1"
    )
    resources: tuple[YandexQualificationInventoryItem, ...]
    file_count: StrictInt = Field(ge=0)
    directory_count: StrictInt = Field(ge=0)
    file_bytes: StrictInt = Field(ge=0)
    provider_pages: StrictInt = Field(ge=1)


class YandexQualificationInventoryProbeResult(DefinitionModel):
    inventory: YandexQualificationInventory
    identity_requests: StrictInt = Field(ge=0)
    disk_requests: StrictInt = Field(ge=0)


class YandexQualificationInventoryProbe(Protocol):
    async def read(
        self,
        connection: ProviderConnection,
        resource_scope_reference: OpaqueProviderReference,
        *,
        credential_binding: YandexQualificationCredentialBinding,
        expected_user_id: SecretStr,
        revalidate: Callable[[], Awaitable[None]],
    ) -> YandexQualificationInventoryProbeResult: ...


__all__ = [
    "InventoryFailureCode",
    "YandexQualificationInventory",
    "YandexQualificationInventoryError",
    "YandexQualificationInventoryItem",
    "YandexQualificationInventoryProbe",
    "YandexQualificationInventoryProbeResult",
]
