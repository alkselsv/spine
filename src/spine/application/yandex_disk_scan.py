"""Inward-facing contracts for bounded Yandex Disk reads and scans."""

from __future__ import annotations

from pydantic import ConfigDict, Field, model_validator

from spine.domain.common import DefinitionModel


class YandexDiskReadError(RuntimeError):
    """Content-free base error exposed by the Yandex Disk provider port."""


class YandexDiskThrottleError(YandexDiskReadError):
    def __init__(self, *, retry_after_seconds: float) -> None:
        if retry_after_seconds < 0:
            raise ValueError("retry_after_seconds cannot be negative")
        self.retry_after_seconds = retry_after_seconds
        super().__init__("Yandex Disk request was throttled.")


class YandexDiskTransientError(YandexDiskReadError):
    def __init__(self) -> None:
        super().__init__("Yandex Disk is temporarily unavailable.")


class YandexDiskProviderUnavailableError(YandexDiskReadError):
    """A provider HTTP failure that the approved qualification must not retry."""

    def __init__(self) -> None:
        super().__init__("Yandex Disk provider request failed.")


class YandexDiskAuthorizationRevokedError(YandexDiskReadError):
    def __init__(self) -> None:
        super().__init__("Yandex Disk authorization was revoked.")


class YandexDiskPermissionLostError(YandexDiskReadError):
    def __init__(self) -> None:
        super().__init__("Yandex Disk approved scope is no longer accessible.")


class YandexDiskScopeOutOfScopeError(YandexDiskReadError):
    def __init__(self) -> None:
        super().__init__("Yandex Disk resource is outside the approved scope.")


class YandexDiskFileTooLargeError(YandexDiskReadError):
    def __init__(self) -> None:
        super().__init__("Yandex Disk file exceeds the approved read limit.")


class YandexDiskPartialDownloadError(YandexDiskReadError):
    def __init__(self) -> None:
        super().__init__("Yandex Disk download did not match provider metadata.")


class YandexDiskConcurrentModificationError(YandexDiskReadError):
    def __init__(self) -> None:
        super().__init__("Yandex Disk file changed during the guarded read.")


class YandexDiskResourceNotFoundError(YandexDiskReadError):
    def __init__(self) -> None:
        super().__init__("Yandex Disk resource was not found in the approved scope.")


class YandexDiskContractError(YandexDiskReadError):
    def __init__(self) -> None:
        super().__init__("Yandex Disk returned an invalid resource listing.")


class YandexDiskScanItem(DefinitionModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    provider_object_reference: str = Field(min_length=1, max_length=2048)
    provider_revision: str = Field(min_length=1, max_length=128)


class YandexDiskScanPage(DefinitionModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    page_identity: str = Field(min_length=1, max_length=128)
    requested_checkpoint: str | None = Field(default=None, max_length=4096)
    checkpoint: str = Field(min_length=1, max_length=4096)
    next_checkpoint: str | None = Field(default=None, max_length=4096)
    items: tuple[YandexDiskScanItem, ...]
    coverage_entry_digests: tuple[str, ...] = ()
    expected_entry_count: int | None = Field(default=None, ge=0)
    confirmed_scan_generation: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )

    @model_validator(mode="after")
    def validate_page(self) -> "YandexDiskScanPage":
        identities = [item.provider_object_reference for item in self.items]
        if len(set(identities)) != len(identities):
            raise ValueError("Yandex Disk scan page items must be unique")
        if len(set(self.coverage_entry_digests)) != len(self.coverage_entry_digests):
            raise ValueError("Yandex Disk coverage entries must be unique per page")
        if any(len(value) != 64 for value in self.coverage_entry_digests):
            raise ValueError("Yandex Disk coverage entries must be SHA-256 digests")
        if self.next_checkpoint is not None and self.next_checkpoint == self.requested_checkpoint:
            raise ValueError("Yandex Disk scan checkpoint must advance")
        return self


__all__ = [name for name in globals() if name.startswith("YandexDisk")]
