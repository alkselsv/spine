from __future__ import annotations

from uuid import UUID

import pytest

from spine.domain.connections.provider import (
    OpaqueProviderReference,
    ProviderCapability,
    ProviderCapabilityKind,
    ProviderCapabilityStatus,
    ProviderConnection,
    ProviderConnectionStatus,
    ProviderKind,
    google_v1_connection_to_v2,
)
from spine.domain.connections.google import (
    GoogleConnection,
    GoogleConnectionStatus,
    OpaqueGoogleReference,
)


def _id(value: int) -> UUID:
    return UUID(f"00000000-0000-0000-0000-{value:012d}")


def _connection(*capabilities: ProviderCapability) -> ProviderConnection:
    return ProviderConnection(
        connection_version_id=_id(1),
        connection_id=_id(2),
        approval_id=_id(3),
        version=1,
        workspace_id=_id(4),
        environment_id=_id(5),
        service_principal_id=_id(6),
        provider_kind=ProviderKind.GOOGLE,
        credential_reference=OpaqueProviderReference(value=_id(7)),
        grant_reference=OpaqueProviderReference(value=_id(8)),
        approved_resource_scope_references=(OpaqueProviderReference(value=_id(9)),),
        authorization_generation=1,
        configuration_generation=1,
        status=ProviderConnectionStatus.ACTIVE,
        capabilities=capabilities,
    )


def test_supported_capability_requires_qualification_evidence() -> None:
    with pytest.raises(ValueError, match="qualification reference"):
        ProviderCapability(
            kind=ProviderCapabilityKind.STABLE_IDENTITY,
            status=ProviderCapabilityStatus.SUPPORTED,
        )


@pytest.mark.parametrize(
    "status",
    (ProviderCapabilityStatus.UNSUPPORTED, ProviderCapabilityStatus.UNQUALIFIED),
)
def test_unsupported_or_unqualified_capability_cannot_satisfy_guarantee(
    status: ProviderCapabilityStatus,
) -> None:
    connection = _connection(
        ProviderCapability(
            kind=ProviderCapabilityKind.STABLE_IDENTITY,
            status=status,
        )
    )

    assert not connection.supports(ProviderCapabilityKind.STABLE_IDENTITY)


def test_google_v1_connection_maps_explicitly_without_changing_scope_or_generations() -> None:
    legacy = GoogleConnection(
        connection_version_id=_id(11),
        connection_id=_id(12),
        approval_id=_id(13),
        version=1,
        workspace_id=_id(14),
        environment_id=_id(15),
        service_principal_id=_id(16),
        credential_reference=OpaqueGoogleReference(value=_id(17)),
        grant_reference=OpaqueGoogleReference(value=_id(18)),
        approved_resource_scope_references=(OpaqueGoogleReference(value=_id(19)),),
        authorization_generation=7,
        configuration_generation=11,
        status=GoogleConnectionStatus.ACTIVE,
    )
    capabilities = (
        ProviderCapability(
            kind=ProviderCapabilityKind.STABLE_IDENTITY,
            status=ProviderCapabilityStatus.SUPPORTED,
            qualification_reference=OpaqueProviderReference(value=_id(20)),
        ),
    )

    mapped = google_v1_connection_to_v2(legacy, capabilities=capabilities)

    assert mapped.provider_kind is ProviderKind.GOOGLE
    assert mapped.connection_version_id == legacy.connection_version_id
    assert mapped.connection_id == legacy.connection_id
    assert mapped.workspace_id == legacy.workspace_id
    assert mapped.environment_id == legacy.environment_id
    assert mapped.authorization_generation == 7
    assert mapped.configuration_generation == 11
    assert tuple(item.value for item in mapped.approved_resource_scope_references) == (
        _id(19),
    )
    assert mapped.capabilities == capabilities
