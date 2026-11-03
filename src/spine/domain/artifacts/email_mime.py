"""Immutable artifacts produced from an exact raw email message."""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import Field, StrictInt, model_validator

from spine.domain.common import DefinitionModel


class MimeParseDisposition(str, Enum):
    COMPLETE = "complete"
    MALFORMED = "malformed"
    LIMIT_EXCEEDED = "limit_exceeded"


class MimeHeader(DefinitionModel):
    schema_version: Literal["mime-header:v1"] = "mime-header:v1"
    name: str = Field(pattern=r"^[a-z0-9-]+$", max_length=998)
    value: str


class MimeDefect(DefinitionModel):
    """Content-free evidence of a defect at one stable MIME path."""

    schema_version: Literal["mime-defect:v1"] = "mime-defect:v1"
    part_path: str = Field(pattern=r"^1(?:\.[1-9][0-9]*)*$")
    code: str = Field(pattern=r"^[a-z0-9_]+$", max_length=128)


class MimePart(DefinitionModel):
    schema_version: Literal["mime-part:v1"] = "mime-part:v1"
    part_path: str = Field(pattern=r"^1(?:\.[1-9][0-9]*)*$")
    media_type: str = Field(pattern=r"^[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+$")
    headers: tuple[MimeHeader, ...]
    content_disposition: Literal["inline", "attachment"] | None = None
    content_id: str | None = None
    filename_present: bool = False
    decoded_byte_length: StrictInt = Field(ge=0)
    payload_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    text: str | None = None
    children: tuple["MimePart", ...] = ()


class MimeArtifact(DefinitionModel):
    """Deterministic, versioned part tree derived without changing raw MIME."""

    schema_version: Literal["mime-artifact:v1"] = "mime-artifact:v1"
    parser_profile: str = Field(pattern=r"^mime-parser:v1:[0-9a-f]{64}$")
    raw_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    raw_byte_length: StrictInt = Field(ge=0)
    disposition: MimeParseDisposition
    headers: tuple[MimeHeader, ...] = ()
    root: MimePart | None = None
    defects: tuple[MimeDefect, ...] = ()

    @model_validator(mode="after")
    def validate_disposition_shape(self) -> "MimeArtifact":
        if self.disposition is MimeParseDisposition.COMPLETE:
            if self.root is None or self.defects:
                raise ValueError("complete MIME artifact requires a defect-free part tree")
        elif self.disposition is MimeParseDisposition.MALFORMED:
            if self.root is None or not self.defects:
                raise ValueError("malformed MIME artifact requires a part tree and defects")
        elif self.root is not None or self.headers or not self.defects:
            raise ValueError("limit disposition must contain only content-free defects")
        return self
