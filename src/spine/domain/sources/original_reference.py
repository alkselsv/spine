"""Provider-neutral immutable reference to finalized original bytes."""

from __future__ import annotations

import re
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


_DIGEST = r"[0-9a-f]{64}"
PositiveInt = Annotated[int, Field(gt=0)]
NonNegativeInt = Annotated[int, Field(ge=0)]
DigestHex = Annotated[str, Field(pattern=_DIGEST)]


class StorageModel(BaseModel):
    """Shared strict immutable model for the Issue #6 contract family."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ObjectReference(StorageModel):
    """Opaque identity for one immutable physical byte generation."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: PositiveInt
    object_id: UUID
    storage_generation: UUID
    digest_algorithm: Literal["sha256"]
    digest_hex: DigestHex
    byte_length: NonNegativeInt

    @model_validator(mode="after")
    def validate_ids(self) -> "ObjectReference":
        if self.object_id.int == 0 or self.storage_generation.int == 0:
            raise ValueError("object identities must be non-zero")
        return self


__all__ = ["ObjectReference", "StorageModel"]
