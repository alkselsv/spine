from uuid import UUID

import pytest

from spine.application.connections.bitrix24_oauth import Bitrix24PinnedGrant, SecretTokenPair
from spine.application.crm_relationship_v2_contracts import (
    Bitrix24CrmRelationshipV2ReadCommand,
    CrmRelationshipBindingOperationsProfile,
)
from spine.connectors.bitrix24_crm_attached_object_v2 import (
    Bitrix24AttachedObjectGetCommand,
    Bitrix24CrmAttachedObjectResult,
    resolve_bitrix24_crm_attached_object,
)
from spine.connectors.bitrix24_crm_files_v2 import Bitrix24CrmFilesItem
from spine.domain.connections.provider import OpaqueProviderReference
from spine.domain.sources.crm_relationship_v2 import CrmRelationshipMappingProfile


def ident(value: int) -> UUID:
    return UUID(f"00000000-0000-4000-8000-{value:012d}")


def profile(**updates: object) -> CrmRelationshipMappingProfile:
    values: dict[str, object] = {
        "schema_version": "bitrix24-crm-files-mapping-profile:v1",
        "record_kind": "mapping_profile",
        "profile_id": ident(30),
        "workspace_id": ident(1),
        "environment_id": ident(2),
        "profile_version": 2,
        "provider_instance": "portal.example.invalid",
        "connection_id": ident(3),
        "connection_approval_id": ident(5),
        "qualification_reference": ident(31),
        "approval_reference": ident(32),
        "activity_namespace": "bitrix24-crm-activity",
        "activity_type_ids": ("meeting",),
        "raw_entity_type_ids": ("DEAL",),
        "entity_namespace": "bitrix24-crm-entity",
        "files_namespace": "bitrix24-crm-files-item",
        "normalizer_id": "crm.activity.binding.list:entityTypeId",
        "normalizer_version": "v1",
        "read_methods": (
            "crm.activity.binding.list",
            "crm.activity.get",
            "disk.attachedObject.get",
        ),
        "mapping_strategy": "attached_object",
        "ownership_proof_reference": "synthetic:approval:ownership",
        "disk_namespace": "bitrix24-disk-object",
    }
    values.update(updates)
    return CrmRelationshipMappingProfile(**values)


def command(*, selected_profile=None) -> Bitrix24CrmRelationshipV2ReadCommand:
    return Bitrix24CrmRelationshipV2ReadCommand(
        workspace_id=ident(1),
        environment_id=ident(2),
        acting_human_id=ident(20),
        human_authorization_generation=7,
        connection_id=ident(3),
        connection_version_id=ident(4),
        authorization_generation=10,
        configuration_generation=11,
        resource_scope_reference=OpaqueProviderReference(value=ident(9)),
        provider_instance="portal.example.invalid",
        provider_acting_user_id="provider-user-20",
        activity_namespace="bitrix24-crm-activity",
        activity_type_id="meeting",
        activity_id="activity-42",
        raw_entity_type_id="DEAL",
        entity_namespace="bitrix24-crm-entity",
        entity_id="deal-7",
        mapping_profile=selected_profile or profile(),
        operations_profile=CrmRelationshipBindingOperationsProfile(
            profile_id="synthetic.crm-binding",
            profile_version="v1",
            profile_kind="synthetic",
            page_size=2,
            max_pages=3,
            max_response_bytes=100,
            max_attempts=6,
        ),
        pinned_grant=Bitrix24PinnedGrant(
            binding_id=ident(40),
            acting_user_id="provider-user-20",
            token_epoch=3,
            security_fence=0,
            tokens=SecretTokenPair("access-secret", "refresh-secret"),
        ),
    )


def item(value: str = "attached-17") -> Bitrix24CrmFilesItem:
    return Bitrix24CrmFilesItem(
        namespace="bitrix24-crm-files-item", value=value
    )


class ProviderFake:
    def __init__(self, result: object = None, *, error: Exception | None = None):
        self.result = {
            "MODULE_ID": "CRM",
            "ENTITY_TYPE": "DEAL",
            "ENTITY_ID": "deal-7",
            "OBJECT_ID": "disk-17",
        } if result is None else result
        self.error = error
        self.commands: list[Bitrix24AttachedObjectGetCommand] = []

    async def get_attached_object(self, get_command):
        self.commands.append(get_command)
        if self.error is not None:
            raise self.error
        return self.result


@pytest.mark.asyncio
async def test_exact_ownership_resolves_namespaced_disk_identity() -> None:
    provider = ProviderFake()

    result = await resolve_bitrix24_crm_attached_object(
        command(), files_item=item(), provider=provider,
        authority_is_current=_authority(True, True),
    )

    assert result.status == "resolved"
    assert result.proof is not None
    assert result.proof.files_item.value == "attached-17"
    assert result.proof.workspace_id == ident(1)
    assert result.proof.environment_id == ident(2)
    assert result.proof.activity_id == "activity-42"
    assert result.proof.entity_id == "deal-7"
    assert result.proof.disk_namespace == "bitrix24-disk-object"
    assert result.proof.disk_object_id == "disk-17"
    assert result.proof.disk_object_id != result.proof.files_item.value
    assert provider.commands[0].attached_object_id == "attached-17"
    assert provider.commands[0].acting_human_id == ident(20)
    assert provider.commands[0].authorization_generation == 10


@pytest.mark.parametrize(
    ("result", "expected_code"),
    [
        ({"MODULE_ID": "CRM", "ENTITY_TYPE": "DEAL", "ENTITY_ID": "deal-7"}, "malformed_provider_result"),
        ({"ENTITY_TYPE": "DEAL", "ENTITY_ID": "deal-7", "OBJECT_ID": "disk-17"}, "malformed_provider_result"),
        ({"MODULE_ID": "CRM", "ENTITY_ID": "deal-7", "OBJECT_ID": "disk-17"}, "malformed_provider_result"),
        ({"MODULE_ID": "CRM", "ENTITY_TYPE": "DEAL", "ENTITY_ID": "deal-7", "OBJECT_ID": "disk-17"}, "resolved"),
        ({"MODULE_ID": "CRM", "ENTITY_TYPE": "LEAD", "ENTITY_ID": "deal-7", "OBJECT_ID": "disk-17"}, "ownership_mismatch"),
        ({"MODULE_ID": "CRM", "ENTITY_TYPE": "DEAL", "ENTITY_ID": "deal-8", "OBJECT_ID": "disk-17"}, "ownership_mismatch"),
        ({"MODULE_ID": "TASKS", "ENTITY_TYPE": "DEAL", "ENTITY_ID": "deal-7", "OBJECT_ID": "disk-17"}, "ownership_mismatch"),
        ({"MODULE_ID": "CRM", "ENTITY_TYPE": "DEAL", "ENTITY_ID": None, "OBJECT_ID": "disk-17"}, "malformed_provider_result"),
    ],
)
@pytest.mark.asyncio
async def test_wrong_or_missing_ownership_fields_fail_closed(
    result: object, expected_code: str
) -> None:
    outcome = await resolve_bitrix24_crm_attached_object(
        command(), files_item=item(), provider=ProviderFake(result),
        authority_is_current=_authority(True, True),
    )

    if expected_code == "resolved":
        assert outcome.status == "resolved"
    else:
        assert outcome == Bitrix24CrmAttachedObjectResult.uncertain(expected_code)


@pytest.mark.parametrize("result", [[], [{"OBJECT_ID": "1"}, {"OBJECT_ID": "2"}]])
@pytest.mark.asyncio
async def test_multiple_or_empty_provider_matches_are_ambiguous(result: object) -> None:
    outcome = await resolve_bitrix24_crm_attached_object(
        command(), files_item=item(), provider=ProviderFake(result),
        authority_is_current=_authority(True, True),
    )

    assert outcome.status == "uncertain"
    assert outcome.error_code == "ambiguous_provider_result"
    assert outcome.proof is None


@pytest.mark.asyncio
async def test_direct_strategy_never_calls_attached_object_port() -> None:
    provider = ProviderFake()
    direct_profile = profile(
        mapping_strategy="direct_disk_object", ownership_proof_reference=None
    )

    outcome = await resolve_bitrix24_crm_attached_object(
        command(selected_profile=direct_profile), files_item=item(), provider=provider,
        authority_is_current=_authority(True, True),
    )

    assert outcome.error_code == "profile_strategy_mismatch"
    assert provider.commands == []


@pytest.mark.asyncio
async def test_foreign_workspace_profile_is_denied_before_provider_call() -> None:
    provider = ProviderFake()
    foreign_profile = profile(workspace_id=ident(99))

    outcome = await resolve_bitrix24_crm_attached_object(
        command(selected_profile=foreign_profile), files_item=item(),
        provider=provider, authority_is_current=_authority(True, True),
    )

    assert outcome.error_code == "profile_scope_mismatch"
    assert provider.commands == []


@pytest.mark.asyncio
async def test_each_sibling_files_item_resolves_only_its_own_attached_id() -> None:
    provider = ProviderFake()
    first = await resolve_bitrix24_crm_attached_object(
        command(), files_item=item("attached-17"), provider=provider,
        authority_is_current=_authority(True, True),
    )
    second = await resolve_bitrix24_crm_attached_object(
        command(), files_item=item("attached-18"), provider=provider,
        authority_is_current=_authority(True, True),
    )

    assert [entry.attached_object_id for entry in provider.commands] == [
        "attached-17", "attached-18"
    ]
    assert first.proof is not None and first.proof.files_item.value == "attached-17"
    assert second.proof is not None and second.proof.files_item.value == "attached-18"


@pytest.mark.asyncio
async def test_equal_file_and_disk_values_remain_separate_namespaces() -> None:
    provider = ProviderFake(
        {
            "MODULE_ID": "CRM",
            "ENTITY_TYPE": "DEAL",
            "ENTITY_ID": "deal-7",
            "OBJECT_ID": "17",
        }
    )

    outcome = await resolve_bitrix24_crm_attached_object(
        command(), files_item=item("17"), provider=provider,
        authority_is_current=_authority(True, True),
    )

    assert outcome.proof is not None
    assert outcome.proof.files_item.value == outcome.proof.disk_object_id == "17"
    assert outcome.proof.files_item.namespace != outcome.proof.disk_namespace


@pytest.mark.asyncio
async def test_stale_authority_before_read_prevents_provider_call() -> None:
    provider = ProviderFake()

    outcome = await resolve_bitrix24_crm_attached_object(
        command(), files_item=item(), provider=provider,
        authority_is_current=_authority(False),
    )

    assert outcome.error_code == "authority_unavailable"
    assert provider.commands == []


@pytest.mark.asyncio
async def test_authority_change_after_read_withholds_proof() -> None:
    provider = ProviderFake()

    outcome = await resolve_bitrix24_crm_attached_object(
        command(), files_item=item(), provider=provider,
        authority_is_current=_authority(True, False),
    )

    assert outcome.error_code == "authority_unavailable"
    assert outcome.proof is None
    assert len(provider.commands) == 1


@pytest.mark.asyncio
async def test_partial_provider_failure_is_redacted_and_never_falls_back() -> None:
    provider = ProviderFake(error=RuntimeError("https://private.invalid/?token=secret"))

    outcome = await resolve_bitrix24_crm_attached_object(
        command(), files_item=item("17"), provider=provider,
        authority_is_current=_authority(True, True),
    )

    assert outcome.error_code == "provider_failure"
    assert outcome.proof is None
    assert "private.invalid" not in repr(outcome)
    assert provider.commands[0].attached_object_id == "17"


@pytest.mark.asyncio
async def test_malformed_provider_payload_is_not_returned() -> None:
    secret_payload = {
        "MODULE_ID": "CRM",
        "ENTITY_TYPE": "DEAL",
        "ENTITY_ID": "deal-7",
        "OBJECT_ID": "disk-17",
        "downloadUrl": "https://private.invalid/file?token=secret",
    }

    outcome = await resolve_bitrix24_crm_attached_object(
        command(), files_item=item(), provider=ProviderFake(secret_payload),
        authority_is_current=_authority(True, True),
    )

    assert outcome.proof is not None
    assert "private.invalid" not in repr(outcome)
    assert "downloadUrl" not in repr(outcome)


@pytest.mark.asyncio
async def test_files_disk_namespace_collision_fails_before_provider_call() -> None:
    provider = ProviderFake()
    collision_profile = profile(disk_namespace="bitrix24-crm-files-item")

    outcome = await resolve_bitrix24_crm_attached_object(
        command(selected_profile=collision_profile), files_item=item(),
        provider=provider, authority_is_current=_authority(True, True),
    )

    assert outcome.error_code == "namespace_collision"
    assert provider.commands == []


def test_result_model_rejects_resolved_outcome_without_proof() -> None:
    with pytest.raises(ValueError, match="requires only an ownership proof"):
        Bitrix24CrmAttachedObjectResult(status="resolved")


def test_result_model_rejects_uncertain_outcome_without_error() -> None:
    with pytest.raises(ValueError, match="requires only a bounded error"):
        Bitrix24CrmAttachedObjectResult(status="uncertain")


def _authority(*currents: bool):
    calls = 0

    async def check(_command) -> bool:
        nonlocal calls
        current = currents[min(calls, len(currents) - 1)]
        calls += 1
        return current

    return check
