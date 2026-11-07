"""Stable canonical identity constants for Bitrix24 Disk sources."""

from hashlib import sha256
from uuid import UUID

BITRIX24_DISK_OBJECT_NAMESPACE = "bitrix24-disk-object"
BITRIX24_DISK_SOURCE_GENERATION = "bitrix24-disk-object:v1"
BITRIX24_DISK_SOURCE_NAMESPACE = "bitrix24-disk"


def canonical_bitrix24_disk_external_object_id(
    connection_id: UUID, disk_object_id: str
) -> str:
    """Derive the canonical S2 identity emitted for one provider Disk object."""

    if connection_id.int == 0 or not disk_object_id:
        raise ValueError("Bitrix24 Disk source identity is incomplete")
    digest = sha256(f"{connection_id}|{disk_object_id}".encode()).hexdigest()
    return f"b24:{digest[:60]}"


__all__ = [
    "BITRIX24_DISK_OBJECT_NAMESPACE",
    "BITRIX24_DISK_SOURCE_GENERATION",
    "BITRIX24_DISK_SOURCE_NAMESPACE",
    "canonical_bitrix24_disk_external_object_id",
]
