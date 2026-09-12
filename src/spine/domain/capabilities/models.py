from __future__ import annotations

from uuid import UUID, uuid4

from pydantic import Field

from spine.domain.common import DefinitionModel, SchemaRef


class CapabilityDefinition(DefinitionModel):
    """A framework-independent unit of work an agent may implement."""

    id: UUID = Field(default_factory=uuid4)
    key: str
    display_name: str
    input_schema: SchemaRef
    output_schema: SchemaRef
    description: str = ""
