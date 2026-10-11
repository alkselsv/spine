from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest

from spine.domain.artifacts import Artifact
from spine.domain.common import ActorKind, ActorRef, SchemaRef


def uid(value: int) -> UUID:
    return UUID(int=value)


def artifact_values() -> dict[str, object]:
    return {
        "id": uid(50),
        "workspace_id": uid(1),
        "environment_id": uid(2),
        "work_item_id": uid(30),
        "produced_by": ActorRef(kind=ActorKind.HUMAN, id=uid(3)),
        "artifact_schema": SchemaRef(name="answer_question.input", version="1"),
        "content": {"question": "What is the answer?"},
        "created_at": datetime(2026, 10, 11, tzinfo=timezone.utc),
    }


@pytest.mark.parametrize(
    "field_name",
    ("id", "workspace_id", "environment_id", "work_item_id"),
)
def test_artifact_rejects_zero_identity(field_name: str) -> None:
    values = artifact_values()
    values[field_name] = UUID(int=0)

    with pytest.raises(ValueError):
        Artifact(**values)


@pytest.mark.parametrize(
    "lineage",
    ((UUID(int=0),), (uid(60), uid(60)), (uid(50),)),
)
def test_artifact_rejects_invalid_lineage(lineage: tuple[UUID, ...]) -> None:
    values = artifact_values()
    values["upstream_artifact_ids"] = lineage

    with pytest.raises(ValueError):
        Artifact(**values)


def test_legacy_artifact_requires_explicit_environment_upgrade() -> None:
    legacy = artifact_values()
    del legacy["environment_id"]

    upgraded = Artifact.from_legacy(legacy, environment_id=uid(2))

    assert upgraded.environment_id == uid(2)
    with pytest.raises(ValueError):
        Artifact(**legacy)
