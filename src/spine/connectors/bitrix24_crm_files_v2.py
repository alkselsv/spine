"""Pure, per-item FILES parsing and direct Disk identity mapping.

This module intentionally accepts only the small, synthetic FILES boundary
needed by the v2 relationship adapter.  It does not call Bitrix24, retain raw
provider payloads, or infer an identity from filenames, paths, subjects or
numeric coincidences.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal
from uuid import UUID

from pydantic import Field, StrictStr, model_validator

from spine.application.crm_relationship_v2_source_match import (
    CrmFilesItemIdentity,
    CrmMappedDiskRevisionEvidence,
    DiskRevisionIdentity,
)
from spine.domain.common import DefinitionModel
from spine.domain.sources.crm_relationship_v2 import CrmRelationshipMappingProfile

BITRIX24_CRM_FILES_FIELD = "FILES"
Bitrix24CrmFilesErrorCode = Literal[
    "malformed_files_payload",
    "missing_files_field",
    "malformed_files_field",
    "malformed_item",
    "profile_strategy_mismatch",
    "namespace_collision",
    "item_namespace_mismatch",
    "duplicate_item_identity",
]


class Bitrix24CrmFilesItem(DefinitionModel):
    """The exact namespaced identity of one FILES item.

    The two identity values are deliberately strict strings.  In particular,
    an integer-looking value is never converted or compared across namespaces.
    """

    field_name: Literal["FILES"] = BITRIX24_CRM_FILES_FIELD
    namespace: StrictStr = Field(min_length=1, max_length=128)
    value: StrictStr = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def validate_identity(self) -> "Bitrix24CrmFilesItem":
        if (
            self.namespace != self.namespace.strip()
            or not self.namespace.isprintable()
            or not self.namespace.strip()
        ):
            raise ValueError("FILES item namespace must be a bounded identifier")
        if (
            self.value != self.value.strip()
            or not self.value.isprintable()
            or not self.value.strip()
        ):
            raise ValueError("FILES item value must be a bounded identifier")
        return self


class Bitrix24CrmDirectDiskObject(DefinitionModel):
    """One exact FILES item mapped to a distinct Disk namespace."""

    mapping_strategy: Literal["direct_disk_object"] = "direct_disk_object"
    profile_id: UUID
    profile_version: int = Field(ge=1)
    files_item: Bitrix24CrmFilesItem
    disk_namespace: StrictStr = Field(min_length=1, max_length=128)
    disk_object_id: StrictStr = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def validate_distinct_identity(self) -> "Bitrix24CrmDirectDiskObject":
        if self.files_item.namespace == self.disk_namespace:
            raise ValueError("FILES and Disk namespaces must be distinct")
        if self.files_item.value != self.disk_object_id:
            raise ValueError("direct Disk mapping must preserve the exact item value")
        return self


class Bitrix24CrmFilesItemResult(DefinitionModel):
    """Safe outcome for one independently processed FILES position."""

    index: int = Field(ge=0)
    status: Literal["mapped", "uncertain"]
    item: Bitrix24CrmFilesItem | None = None
    mapping: Bitrix24CrmDirectDiskObject | None = None
    error_code: Bitrix24CrmFilesErrorCode | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> "Bitrix24CrmFilesItemResult":
        if self.status == "mapped":
            if self.item is None or self.mapping is None or self.error_code is not None:
                raise ValueError("mapped FILES result must contain only a mapping")
        elif self.mapping is not None or self.error_code is None:
            raise ValueError("uncertain FILES result must contain a safe error")
        return self

    @classmethod
    def mapped(
        cls,
        *,
        index: int,
        item: Bitrix24CrmFilesItem,
        mapping: Bitrix24CrmDirectDiskObject,
    ) -> "Bitrix24CrmFilesItemResult":
        return cls(index=index, status="mapped", item=item, mapping=mapping)

    @classmethod
    def uncertain(
        cls,
        *,
        index: int,
        code: Bitrix24CrmFilesErrorCode,
        item: Bitrix24CrmFilesItem | None = None,
    ) -> "Bitrix24CrmFilesItemResult":
        return cls(index=index, status="uncertain", item=item, error_code=code)


class Bitrix24CrmFilesMappingResult(DefinitionModel):
    """Per-item direct mapping results with only bounded error codes."""

    items: tuple[Bitrix24CrmFilesItemResult, ...] = ()
    error_code: (
        Literal[
            "malformed_files_payload", "missing_files_field", "malformed_files_field"
        ]
        | None
    ) = None


def map_bitrix24_crm_files(
    payload: Mapping[str, object],
    *,
    profile: CrmRelationshipMappingProfile,
) -> Bitrix24CrmFilesMappingResult:
    """Parse the exact ``FILES`` field and map each item independently.

    The accepted synthetic input shape is
    ``{"FILES": [{"namespace": ..., "value": ...}, ...]}``.  Only those two
    item keys participate in identity;
    all other provider fields are intentionally ignored.
    """

    if not isinstance(payload, Mapping):
        return Bitrix24CrmFilesMappingResult(error_code="malformed_files_payload")
    if BITRIX24_CRM_FILES_FIELD not in payload:
        return Bitrix24CrmFilesMappingResult(error_code="missing_files_field")
    raw_items = payload[BITRIX24_CRM_FILES_FIELD]
    if not isinstance(raw_items, Sequence) or isinstance(raw_items, (str, bytes)):
        return Bitrix24CrmFilesMappingResult(error_code="malformed_files_field")

    parsed: list[tuple[int, Bitrix24CrmFilesItem | None]] = []
    results: list[Bitrix24CrmFilesItemResult] = []
    for index, raw_item in enumerate(raw_items):
        item = _parse_item(raw_item)
        parsed.append((index, item))
        if item is None:
            results.append(
                Bitrix24CrmFilesItemResult.uncertain(index=index, code="malformed_item")
            )
        else:
            results.append(
                Bitrix24CrmFilesItemResult.uncertain(
                    index=index, item=item, code="malformed_item"
                )
            )

    if profile.mapping_strategy != "direct_disk_object":
        return _mark_valid_items_uncertain(results, "profile_strategy_mismatch")
    if profile.files_namespace == profile.disk_namespace:
        return _mark_valid_items_uncertain(results, "namespace_collision")

    counts: dict[tuple[str, str], int] = {}
    for _, item in parsed:
        if item is not None:
            key = (item.namespace, item.value)
            counts[key] = counts.get(key, 0) + 1

    for position, item in parsed:
        if item is None:
            continue
        if item.namespace != profile.files_namespace:
            results[position] = Bitrix24CrmFilesItemResult.uncertain(
                index=position, item=item, code="item_namespace_mismatch"
            )
        elif counts[(item.namespace, item.value)] > 1:
            results[position] = Bitrix24CrmFilesItemResult.uncertain(
                index=position, item=item, code="duplicate_item_identity"
            )
        else:
            mapping = Bitrix24CrmDirectDiskObject(
                profile_id=profile.profile_id,
                profile_version=profile.profile_version,
                files_item=item,
                disk_namespace=profile.disk_namespace,
                disk_object_id=item.value,
            )
            results[position] = Bitrix24CrmFilesItemResult.mapped(
                index=position, item=item, mapping=mapping
            )
    return Bitrix24CrmFilesMappingResult(items=tuple(results))


def _parse_item(raw_item: object) -> Bitrix24CrmFilesItem | None:
    if not isinstance(raw_item, Mapping):
        return None
    namespace = raw_item.get("namespace")
    value = raw_item.get("value")
    if not isinstance(namespace, str) or not isinstance(value, str):
        return None
    try:
        return Bitrix24CrmFilesItem(namespace=namespace, value=value)
    except ValueError:
        return None


def bind_direct_disk_revision_evidence(
    mapping: Bitrix24CrmDirectDiskObject,
    *,
    profile: CrmRelationshipMappingProfile,
    connection_version_id: UUID,
    provider_revision: str,
) -> CrmMappedDiskRevisionEvidence:
    """Adapt one qualified T3 mapping to the inward source-match contract."""

    if (
        profile.mapping_strategy != "direct_disk_object"
        or mapping.profile_id != profile.profile_id
        or mapping.profile_version != profile.profile_version
        or mapping.files_item.namespace != profile.files_namespace
        or mapping.disk_namespace != profile.disk_namespace
    ):
        raise ValueError("direct Disk mapping does not match its scoped profile")
    return CrmMappedDiskRevisionEvidence(
        workspace_id=profile.workspace_id,
        environment_id=profile.environment_id,
        connection_id=profile.connection_id,
        connection_version_id=connection_version_id,
        mapping_strategy=mapping.mapping_strategy,
        mapping_profile_id=mapping.profile_id,
        mapping_profile_version=mapping.profile_version,
        files_item=CrmFilesItemIdentity(
            namespace=mapping.files_item.namespace,
            value=mapping.files_item.value,
        ),
        disk_revision=DiskRevisionIdentity(
            namespace=mapping.disk_namespace,
            object_id=mapping.disk_object_id,
            provider_revision=provider_revision,
        ),
    )


def _mark_valid_items_uncertain(
    results: list[Bitrix24CrmFilesItemResult],
    code: Bitrix24CrmFilesErrorCode,
) -> Bitrix24CrmFilesMappingResult:
    return Bitrix24CrmFilesMappingResult(
        items=tuple(
            (
                result
                if result.item is None
                else Bitrix24CrmFilesItemResult.uncertain(
                    index=result.index, item=result.item, code=code
                )
            )
            for result in results
        )
    )


__all__ = [
    "BITRIX24_CRM_FILES_FIELD",
    "Bitrix24CrmDirectDiskObject",
    "Bitrix24CrmFilesErrorCode",
    "Bitrix24CrmFilesItem",
    "Bitrix24CrmFilesItemResult",
    "Bitrix24CrmFilesMappingResult",
    "bind_direct_disk_revision_evidence",
    "map_bitrix24_crm_files",
]
