from uuid import UUID

import pytest

from spine.connectors.bitrix24_crm_files_v2 import (
    Bitrix24CrmFilesItemResult,
    bind_direct_disk_revision_evidence,
    map_bitrix24_crm_files,
)
from spine.domain.sources.crm_relationship_v2 import CrmRelationshipMappingProfile


def _profile(**updates: object) -> CrmRelationshipMappingProfile:
    values: dict[str, object] = {
        "schema_version": "bitrix24-crm-files-mapping-profile:v1",
        "record_kind": "mapping_profile",
        "profile_id": UUID(int=1),
        "workspace_id": UUID(int=2),
        "environment_id": UUID(int=3),
        "profile_version": 1,
        "provider_instance": "portal-test",
        "connection_id": UUID(int=4),
        "connection_approval_id": UUID(int=5),
        "qualification_reference": UUID(int=6),
        "approval_reference": UUID(int=7),
        "activity_namespace": "bitrix.activity",
        "activity_type_ids": ("1",),
        "raw_entity_type_ids": ("2",),
        "entity_namespace": "bitrix.company",
        "files_namespace": "bitrix.activity.files",
        "normalizer_id": "bitrix.enum.ownertype",
        "normalizer_version": "1",
        "read_methods": ("crm.activity.binding.list",),
        "mapping_strategy": "direct_disk_object",
        "disk_namespace": "bitrix.disk.object",
    }
    values.update(updates)
    return CrmRelationshipMappingProfile(**values)


def test_direct_mapping_preserves_each_exact_files_identity() -> None:
    result = map_bitrix24_crm_files(
        {
            "FILES": [
                {"namespace": "bitrix.activity.files", "value": "disk-17"},
                {"namespace": "bitrix.activity.files", "value": "disk-18"},
            ]
        },
        profile=_profile(),
    )

    assert [item.status for item in result.items] == ["mapped", "mapped"]
    assert [item.item.value for item in result.items if item.item] == [
        "disk-17",
        "disk-18",
    ]
    assert [item.item.field_name for item in result.items if item.item] == [
        "FILES",
        "FILES",
    ]
    assert [item.mapping.disk_object_id for item in result.items if item.mapping] == [
        "disk-17",
        "disk-18",
    ]
    assert all(
        item.mapping.disk_namespace == "bitrix.disk.object"
        for item in result.items
        if item.mapping
    )


def test_t3_mapping_adapts_to_exact_source_match_evidence() -> None:
    scoped_profile = _profile(disk_namespace="bitrix24-disk-object")
    result = map_bitrix24_crm_files(
        {
            "FILES": [
                {"namespace": "bitrix.activity.files", "value": "disk-17"},
            ]
        },
        profile=scoped_profile,
    )
    mapping = result.items[0].mapping
    assert mapping is not None

    evidence = bind_direct_disk_revision_evidence(
        mapping,
        profile=scoped_profile,
        connection_version_id=UUID(int=8),
        provider_revision="17",
    )

    assert evidence.files_item.value == "disk-17"
    assert evidence.disk_revision.object_id == "disk-17"
    assert evidence.disk_revision.provider_revision == "17"


def test_t3_mapping_cannot_be_rebound_to_another_tenant_profile() -> None:
    original_profile = _profile(disk_namespace="bitrix24-disk-object")
    result = map_bitrix24_crm_files(
        {
            "FILES": [
                {"namespace": "bitrix.activity.files", "value": "disk-17"},
            ]
        },
        profile=original_profile,
    )
    mapping = result.items[0].mapping
    assert mapping is not None
    foreign_profile = _profile(
        profile_id=UUID(int=99),
        workspace_id=UUID(int=98),
        connection_id=UUID(int=97),
        disk_namespace="bitrix24-disk-object",
    )

    with pytest.raises(ValueError, match="scoped profile"):
        bind_direct_disk_revision_evidence(
            mapping,
            profile=foreign_profile,
            connection_version_id=UUID(int=8),
            provider_revision="17",
        )


def test_malformed_item_becomes_uncertain_without_contaminating_valid_sibling() -> None:
    result = map_bitrix24_crm_files(
        {
            "FILES": [
                {"namespace": "bitrix.activity.files"},
                {"namespace": "bitrix.activity.files", "value": "disk-18"},
            ]
        },
        profile=_profile(),
    )

    assert result.items[0] == Bitrix24CrmFilesItemResult.uncertain(
        index=0,
        code="malformed_item",
    )
    assert result.items[1].status == "mapped"
    assert result.items[1].mapping is not None


def test_strategy_mismatch_denies_every_item_without_direct_fallback() -> None:
    result = map_bitrix24_crm_files(
        {"FILES": [{"namespace": "bitrix.activity.files", "value": "17"}]},
        profile=_profile(
            mapping_strategy="attached_object",
            ownership_proof_reference="synthetic:ownership",
            read_methods=(
                "crm.activity.binding.list",
                "disk.attachedObject.get",
            ),
        ),
    )

    assert result.items[0].status == "uncertain"
    assert result.items[0].error_code == "profile_strategy_mismatch"
    assert result.items[0].mapping is None


def test_duplicate_identity_is_uncertain_for_each_duplicate_only() -> None:
    result = map_bitrix24_crm_files(
        {
            "FILES": [
                {"namespace": "bitrix.activity.files", "value": "17"},
                {"namespace": "bitrix.activity.files", "value": "17"},
                {"namespace": "bitrix.activity.files", "value": "18"},
            ]
        },
        profile=_profile(),
    )

    assert [item.error_code for item in result.items] == [
        "duplicate_item_identity",
        "duplicate_item_identity",
        None,
    ]
    assert result.items[2].mapping is not None


def test_namespace_mismatch_does_not_use_numeric_or_filename_fallback() -> None:
    result = map_bitrix24_crm_files(
        {
            "FILES": [
                {
                    "namespace": "bitrix.crm.attachment",
                    "value": "17",
                    "filename": "disk-17.pdf",
                    "path": "/untrusted/disk-17.pdf",
                }
            ]
        },
        profile=_profile(),
    )

    assert result.items[0].status == "uncertain"
    assert result.items[0].error_code == "item_namespace_mismatch"


def test_sibling_namespace_cannot_substitute_for_exact_files_item() -> None:
    result = map_bitrix24_crm_files(
        {
            "FILES": [
                {"namespace": "bitrix.activity.files", "value": "17"},
                {"namespace": "bitrix.crm.attachment", "value": "17"},
            ]
        },
        profile=_profile(),
    )

    assert result.items[0].mapping is not None
    assert result.items[0].mapping.disk_object_id == "17"
    assert result.items[1].mapping is None
    assert result.items[1].error_code == "item_namespace_mismatch"


def test_item_namespace_matching_disk_namespace_fails_closed() -> None:
    result = map_bitrix24_crm_files(
        {
            "FILES": [
                {"namespace": "bitrix.disk.object", "value": "17"},
            ]
        },
        profile=_profile(),
    )

    assert result.items[0].status == "uncertain"
    assert result.items[0].error_code == "item_namespace_mismatch"


def test_files_and_disk_namespace_collision_fails_closed() -> None:
    result = map_bitrix24_crm_files(
        {"FILES": [{"namespace": "bitrix.activity.files", "value": "17"}]},
        profile=_profile(disk_namespace="bitrix.activity.files"),
    )

    assert result.items[0].status == "uncertain"
    assert result.items[0].error_code == "namespace_collision"


def test_files_field_shape_error_is_bounded_and_content_free() -> None:
    result = map_bitrix24_crm_files(
        {"FILES": {"secret": "do-not-return"}},
        profile=_profile(),
    )

    assert result.items == ()
    assert result.error_code == "malformed_files_field"
    assert "do-not-return" not in repr(result)


def test_missing_files_field_is_uncertain_without_searching_other_fields() -> None:
    result = map_bitrix24_crm_files(
        {"attachments": [{"namespace": "bitrix.activity.files", "value": "17"}]},
        profile=_profile(),
    )

    assert result.items == ()
    assert result.error_code == "missing_files_field"


def test_reordering_items_changes_only_result_order_not_identity_mapping() -> None:
    result = map_bitrix24_crm_files(
        {
            "FILES": [
                {"namespace": "bitrix.activity.files", "value": "18"},
                {"namespace": "bitrix.activity.files", "value": "17"},
            ]
        },
        profile=_profile(),
    )

    assert [item.index for item in result.items] == [0, 1]
    assert [item.mapping.disk_object_id for item in result.items if item.mapping] == [
        "18",
        "17",
    ]
