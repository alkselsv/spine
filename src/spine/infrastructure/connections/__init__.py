"""Persistence adapters for external connection authority."""

from spine.infrastructure.connections.bitrix24_oauth import (
    InMemoryBitrix24OAuthRepository,
    InMemoryTokenPairStore,
)
from spine.infrastructure.connections.postgresql_bitrix24_oauth import (
    PostgreSQLBitrix24OAuthRepository,
)
from spine.infrastructure.connections.google import InMemoryGoogleConnectionRepository
from spine.infrastructure.connections.postgresql_google import (
    PostgreSQLGoogleConnectionRepository,
)
from spine.infrastructure.connections.provider_management import (
    InMemoryProviderConnectionManagementRepository,
)
from spine.infrastructure.connections.fake_provider import FakeProviderConnectionObserver
from spine.infrastructure.connections.postgresql_provider_management import (
    PostgreSQLProviderConnectionManagementRepository,
)

__all__ = [
    "InMemoryBitrix24OAuthRepository",
    "InMemoryTokenPairStore",
    "InMemoryGoogleConnectionRepository",
    "InMemoryProviderConnectionManagementRepository",
    "FakeProviderConnectionObserver",
    "PostgreSQLGoogleConnectionRepository",
    "PostgreSQLBitrix24OAuthRepository",
    "PostgreSQLProviderConnectionManagementRepository",
]
