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


def test_provider_kind_and_existing_capability_wire_values_remain_stable() -> None:
    assert [kind.value for kind in ProviderKind] == [
        "google",
        "yandex_disk",
        "bitrix24",
        "mounted_filesystem",
        "yandex_mail",
    ]
    assert [kind.value for kind in ProviderCapabilityKind] == [
        "enumeration",
        "metadata_read",
        "binary_read",
        "tabular_read",
        "structured_read",
        "stable_identity",
        "incremental_change_discovery",
        "full_reconciliation",
        "lifecycle_evidence",
        "health_diagnostics",
        "crm",
        "mailbox_enumeration",
        "message_metadata_read",
        "raw_message_read",
        "attachment_read",
    ]


def test_existing_provider_connection_payload_decodes_without_migration() -> None:
    legacy_payload = {
        "schema_version": "provider-connection:v2",
        "connection_version_id": str(_id(1)),
        "connection_id": str(_id(2)),
        "approval_id": str(_id(3)),
        "version": 1,
        "predecessor_version_id": None,
        "workspace_id": str(_id(4)),
        "environment_id": str(_id(5)),
        "service_principal_id": str(_id(6)),
        "provider_kind": "yandex_disk",
        "credential_reference": {"value": str(_id(7))},
        "grant_reference": {"value": str(_id(8))},
        "approved_resource_scope_references": [{"value": str(_id(9))}],
        "approved_crm_entity_types": [],
        "authorization_generation": 1,
        "configuration_generation": 1,
        "status": "active",
        "capabilities": [],
    }

    decoded = ProviderConnection.model_validate(legacy_payload)

    assert decoded.provider_kind is ProviderKind.YANDEX_DISK
    assert decoded.model_dump(mode="json") == legacy_payload


@pytest.mark.parametrize(
    ("provider_kind", "existing_capabilities"),
    (
        (
            ProviderKind.GOOGLE,
            (ProviderCapabilityKind.BINARY_READ, ProviderCapabilityKind.TABULAR_READ),
        ),
        (
            ProviderKind.YANDEX_DISK,
            (ProviderCapabilityKind.ENUMERATION, ProviderCapabilityKind.BINARY_READ),
        ),
    ),
)
def test_existing_file_authority_does_not_imply_email_capabilities(
    provider_kind: ProviderKind,
    existing_capabilities: tuple[ProviderCapabilityKind, ...],
) -> None:
    connection = _connection(
        *(
            ProviderCapability(
                kind=kind,
                status=ProviderCapabilityStatus.SUPPORTED,
                qualification_reference=OpaqueProviderReference(
                    value=_id(30 + index)
                ),
            )
            for index, kind in enumerate(existing_capabilities)
        )
    ).model_copy(update={"provider_kind": provider_kind})

    assert not connection.supports(ProviderCapabilityKind.MAILBOX_ENUMERATION)
    assert not connection.supports(ProviderCapabilityKind.MESSAGE_METADATA_READ)
    assert not connection.supports(ProviderCapabilityKind.RAW_MESSAGE_READ)
    assert not connection.supports(ProviderCapabilityKind.ATTACHMENT_READ)


@pytest.mark.parametrize(
    "provider_kind", (ProviderKind.GOOGLE, ProviderKind.YANDEX_MAIL)
)
def test_mail_connection_uses_explicit_operation_capabilities(
    provider_kind: ProviderKind,
) -> None:
    email_capabilities = tuple(
        ProviderCapability(
            kind=kind,
            status=ProviderCapabilityStatus.SUPPORTED,
            qualification_reference=OpaqueProviderReference(value=_id(40 + index)),
        )
        for index, kind in enumerate(
            (
                ProviderCapabilityKind.MAILBOX_ENUMERATION,
                ProviderCapabilityKind.MESSAGE_METADATA_READ,
                ProviderCapabilityKind.RAW_MESSAGE_READ,
                ProviderCapabilityKind.ATTACHMENT_READ,
            )
        )
    )
    connection = ProviderConnection.model_validate(
        _connection(*email_capabilities).model_dump(mode="json")
        | {"provider_kind": provider_kind.value}
    )

    assert connection.provider_kind is provider_kind
    assert all(connection.supports(capability.kind) for capability in email_capabilities)


def test_duplicate_email_capability_is_rejected() -> None:
    capability = ProviderCapability(
        kind=ProviderCapabilityKind.RAW_MESSAGE_READ,
        status=ProviderCapabilityStatus.UNSUPPORTED,
    )

    with pytest.raises(ValueError, match="must be unique"):
        _connection(capability, capability)


def test_unknown_provider_capability_fails_closed() -> None:
    with pytest.raises(ValueError):
        ProviderCapability.model_validate(
            {"kind": "provider_supplied_diagnostic", "status": "supported"}
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
