"""Disclosure-safe provider connection management failures."""


class ProviderConnectionManagementError(RuntimeError):
    """Base class for content-free connection-management failures."""


class ProviderConnectionNotFoundError(ProviderConnectionManagementError):
    def __init__(self) -> None:
        super().__init__("Provider connection is unavailable.")


class InvalidProviderConnectionRequest(ProviderConnectionManagementError):
    def __init__(self) -> None:
        super().__init__("Provider connection request is invalid.")


__all__ = [
    "InvalidProviderConnectionRequest",
    "ProviderConnectionManagementError",
    "ProviderConnectionNotFoundError",
]
