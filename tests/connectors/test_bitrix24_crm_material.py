from datetime import datetime, timezone
from uuid import UUID

import pytest

from spine.connectors.bitrix24_crm_material import (
    BITRIX24_CRM_REQUIRED_CAPABILITIES,
    Bitrix24CrmActivity,
    Bitrix24CrmAttachment,
    Bitrix24CrmMaterialAdapter,
    Bitrix24CrmMaterialReadCommand,
    Bitrix24CrmMaterialReadProfile,
    Bitrix24CrmMaterialPermissionLostError,
    Bitrix24CrmMaterialNotFoundError,
    bitrix24_crm_capability,
)
from spine.connectors.bitrix24_disk import bitrix24_disk_capabilities
from spine.domain.connections.provider import (
    OpaqueProviderReference,
    ProviderCapabilityKind,
    ProviderCapabilityStatus,
    ProviderConnection,
    ProviderConnectionStatus,
    ProviderKind,
)

NOW = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)


def _id(value: int) -> UUID:
    return UUID(f"00000000-0000-4000-8000-{value:012d}")


SCOPE = OpaqueProviderReference(value=_id(9))


def _connection(*, workspace_id: UUID = _id(4)) -> ProviderConnection:
    capabilities = bitrix24_disk_capabilities(
        qualification_reference=OpaqueProviderReference(value=_id(10)),
        crm_qualification_reference=OpaqueProviderReference(value=_id(11)),
    )
    return ProviderConnection(
        connection_version_id=_id(1),
        connection_id=_id(2),
        approval_id=_id(3),
        version=1,
        workspace_id=workspace_id,
        environment_id=_id(5),
        service_principal_id=_id(6),
        provider_kind=ProviderKind.BITRIX24,
        credential_reference=OpaqueProviderReference(value=_id(7)),
        grant_reference=OpaqueProviderReference(value=_id(8)),
        approved_resource_scope_references=(SCOPE,),
        approved_crm_entity_types=("deal",),
        authorization_generation=2,
        configuration_generation=3,
        status=ProviderConnectionStatus.ACTIVE,
        capabilities=capabilities,
    )


def _command(**changes: object) -> Bitrix24CrmMaterialReadCommand:
    values: dict[str, object] = {
        "workspace_id": _id(4),
        "environment_id": _id(5),
        "connection_id": _id(2),
        "connection_version_id": _id(1),
        "resource_scope_reference": SCOPE,
        "authorization_generation": 2,
        "configuration_generation": 3,
        "entity_type": "deal",
        "entity_id": "932",
        "activity_id": "501",
        "disk_file_id": "9043",
        "target_source_object_id": _id(20),
        "target_source_revision_id": _id(21),
        "approved_entity_types": ("deal",),
        "required_capabilities": BITRIX24_CRM_REQUIRED_CAPABILITIES,
        "acting_subject_id": _id(77),
    }
    values.update(changes)
    return Bitrix24CrmMaterialReadCommand(**values)


class FakeCrmService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def authorize_read(self, connection, scope, **kwargs):
        self.calls.append(("authorize", (scope, kwargs)))

    async def get_activity(self, connection, scope, **kwargs):
        self.calls.append(("activity", (scope, kwargs)))
        return Bitrix24CrmActivity(
            ID="501",
            OWNER_TYPE_ID="deal",
            OWNER_ID="932",
            LAST_UPDATED=NOW,
            ACTIVITY_REVISION="activity-v7",
        )

    async def get_attachment(self, connection, scope, **kwargs):
        self.calls.append(("attachment", (scope, kwargs)))
        return Bitrix24CrmAttachment(
            ID="attachment-1",
            ACTIVITY_ID="501",
            OWNER_TYPE_ID="deal",
            OWNER_ID="932",
            DISK_FILE_ID="9043",
            DISK_FILE_REVISION="42",
            RELATIONSHIP_REVISION="link-v3",
        )


def _adapter(service: FakeCrmService) -> Bitrix24CrmMaterialAdapter:
    return Bitrix24CrmMaterialAdapter(
        service=service,
        profile=Bitrix24CrmMaterialReadProfile(),
        clock=lambda: NOW,
    )


@pytest.mark.asyncio
async def test_crm_activity_attachment_preserves_exact_binding_and_disk_revision() -> None:
    service = FakeCrmService()
    observation = await _adapter(service).read(_connection(), _command())

    assert observation.crm_entity_type == "deal"
    assert observation.crm_entity_id == "932"
    assert observation.crm_activity_id == "501"
    assert observation.disk_file_id == "9043"
    assert observation.disk_file_revision == "42"
    assert observation.target_source_revision_id == _id(21)
    assert "brief" not in observation.model_dump_json().lower()


@pytest.mark.asyncio
async def test_crm_reader_rejects_unapproved_entity_and_never_calls_provider() -> None:
    service = FakeCrmService()
    with pytest.raises(ValueError, match="approved entity"):
        await _adapter(service).read(
            _connection(), _command(approved_entity_types=("contact",))
        )
    assert service.calls == []


@pytest.mark.asyncio
async def test_crm_reader_rejects_missing_scope_before_provider_access() -> None:
    service = FakeCrmService()
    with pytest.raises(ValueError, match="resource scope"):
        await _adapter(service).read(
            _connection(),
            _command(resource_scope_reference=OpaqueProviderReference(value=_id(99))),
        )
    assert service.calls == []


@pytest.mark.asyncio
async def test_crm_reader_rejects_cross_workspace_before_provider_access() -> None:
    service = FakeCrmService()
    with pytest.raises(ValueError, match="trusted connection scope"):
        await _adapter(service).read(_connection(), _command(workspace_id=_id(44)))
    assert service.calls == []


@pytest.mark.asyncio
async def test_crm_reader_rejects_mismatched_attachment_file() -> None:
    service = FakeCrmService()
    with pytest.raises(Bitrix24CrmMaterialNotFoundError):
        await _adapter(service).read(_connection(), _command(disk_file_id="different-file"))


def test_crm_capability_is_conditional_and_does_not_enable_unrelated_management() -> None:
    capabilities = {
        capability.kind: capability
        for capability in bitrix24_crm_capability(
            qualification_reference=OpaqueProviderReference(value=_id(10)),
            approved_entity_types=("deal",),
        )
    }
    assert capabilities[ProviderCapabilityKind.CRM].status is ProviderCapabilityStatus.SUPPORTED
    assert ProviderCapabilityKind.STRUCTURED_READ not in BITRIX24_CRM_REQUIRED_CAPABILITIES


@pytest.mark.asyncio
async def test_crm_reader_translates_current_access_loss_without_disclosing_payload() -> None:
    service = FakeCrmService()

    async def deny(*args, **kwargs):
        del args, kwargs
        raise Bitrix24CrmMaterialPermissionLostError()

    service.authorize_read = deny
    with pytest.raises(Bitrix24CrmMaterialPermissionLostError):
        await _adapter(service).read(_connection(), _command())
    assert "9043" not in str(Bitrix24CrmMaterialPermissionLostError())
