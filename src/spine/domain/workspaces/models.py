from __future__ import annotations

from uuid import UUID, uuid4

from pydantic import Field

from spine.domain.common import EnvironmentKind, SpineModel


class Workspace(SpineModel):
    id: UUID = Field(default_factory=uuid4)
    slug: str
    display_name: str


class Environment(SpineModel):
    id: UUID = Field(default_factory=uuid4)
    workspace_id: UUID
    kind: EnvironmentKind
    display_name: str
