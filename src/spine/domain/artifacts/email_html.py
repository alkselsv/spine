"""Immutable safe derivatives of HTML email body parts."""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import Field, StrictInt, model_validator

from spine.domain.common import DefinitionModel


class EmailHtmlDisposition(str, Enum):
    COMPLETE = "complete"
    INPUT_LIMIT_EXCEEDED = "input_limit_exceeded"
    OUTPUT_LIMIT_EXCEEDED = "output_limit_exceeded"
    TRAVERSAL_LIMIT_EXCEEDED = "traversal_limit_exceeded"
    SOURCE_UNAVAILABLE = "source_unavailable"


class SanitizedEmailHtml(DefinitionModel):
    """A safe display derivative tied to its immutable raw MIME source."""

    schema_version: Literal["sanitized-email-html:v1"] = "sanitized-email-html:v1"
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_part_path: str = Field(pattern=r"^1(?:\.[1-9][0-9]*)*$")
    sanitizer_profile: str = Field(pattern=r"^email-html-sanitizer:v1:[0-9a-f]{64}$")
    disposition: EmailHtmlDisposition
    sanitized_html: str | None = None
    plain_text: str | None = None

    @model_validator(mode="after")
    def validate_content_shape(self) -> "SanitizedEmailHtml":
        if self.disposition is EmailHtmlDisposition.COMPLETE:
            if self.sanitized_html is None or self.plain_text is None:
                raise ValueError("complete HTML derivative requires both output forms")
        elif self.sanitized_html is not None or self.plain_text is not None:
            raise ValueError("limited HTML derivative cannot contain source content")
        return self

class EmailHtmlSanitizerProfile(DefinitionModel):
    """Versioned resource limits for deriving safe email HTML."""

    schema_version: Literal["email-html-sanitizer-profile:v1"] = (
        "email-html-sanitizer-profile:v1"
    )
    max_total_input_bytes: StrictInt = Field(gt=0)
    max_total_output_bytes: StrictInt = Field(gt=0)
    max_parts: StrictInt = Field(gt=0)
    max_depth: StrictInt = Field(gt=0)
