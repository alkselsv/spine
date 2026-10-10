from __future__ import annotations

from enum import Enum
from uuid import UUID

import pytest
from pydantic import (
    BaseModel,
    ConfigDict,
    PrivateAttr,
    ValidationError,
    computed_field,
)

from spine.application.persistence.outbox import (
    OpaqueObjectReference,
    OutboxEventRegistry,
    UnsupportedOutboxEventError,
)


class WorkspaceTransitionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace: OpaqueObjectReference
    previous_state: str
    current_state: str


class UnsafeWorkspaceTransitionPayload(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    workspace: OpaqueObjectReference


class MutableWorkspaceTransitionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace: OpaqueObjectReference


class ShallowFrozenWorkspaceTransitionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace: OpaqueObjectReference
    labels: list[str]


class MutableValueEnum(Enum):
    LABELS = []


class FrozenPayloadWithMutableEnumValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    value: MutableValueEnum


class FrozenTuplePayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    labels: tuple[str, ...]


class FrozenPayloadWithPrivateComputedState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    _labels: list[str] = PrivateAttr(default_factory=list)

    @computed_field
    @property
    def labels(self) -> list[str]:
        return self._labels


class FrozenFloatPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    value: float


WORKSPACE_ID = UUID("10000000-0000-0000-0000-000000000001")
EVENT_ID = UUID("20000000-0000-0000-0000-000000000001")
TRACE_ID = UUID("30000000-0000-0000-0000-000000000001")


def registry() -> OutboxEventRegistry:
    events = OutboxEventRegistry()
    events.register(
        event_type="workspace.lifecycle_changed",
        schema_version=1,
        payload_type=WorkspaceTransitionPayload,
    )
    return events


def payload() -> WorkspaceTransitionPayload:
    return WorkspaceTransitionPayload(
        workspace=OpaqueObjectReference(
            object_type="workspace",
            object_id=WORKSPACE_ID,
            schema_version=1,
        ),
        previous_state="provisioning",
        current_state="active",
    )


def test_registered_payload_builds_immutable_versioned_intent() -> None:
    intent = registry().build_intent(
        event_id=EVENT_ID,
        workspace_id=WORKSPACE_ID,
        event_type="workspace.lifecycle_changed",
        schema_version=1,
        payload=payload(),
        producer_deduplication_id="workspace:activate:v1",
        trace_id=TRACE_ID,
    )

    assert intent.event_id == EVENT_ID
    assert intent.event_type == "workspace.lifecycle_changed"
    assert intent.schema_version == 1
    assert intent.payload == payload()
    assert intent.payload_json() == {
        "workspace": {
            "object_type": "workspace",
            "object_id": str(WORKSPACE_ID),
            "schema_version": 1,
        },
        "previous_state": "provisioning",
        "current_state": "active",
    }
    with pytest.raises(ValidationError):
        intent.event_type = "workspace.other"  # type: ignore[misc]


def test_unregistered_type_or_version_is_rejected() -> None:
    events = registry()

    for event_type, schema_version in (
        ("workspace.unknown", 1),
        ("workspace.lifecycle_changed", 2),
    ):
        with pytest.raises(UnsupportedOutboxEventError, match="not registered"):
            events.build_intent(
                event_id=EVENT_ID,
                workspace_id=WORKSPACE_ID,
                event_type=event_type,
                schema_version=schema_version,
                payload=payload(),
                trace_id=TRACE_ID,
            )


def test_registry_rejects_payload_type_without_forbid_extra_policy() -> None:
    events = OutboxEventRegistry()

    with pytest.raises(ValueError, match='extra="forbid"'):
        events.register(
            event_type="workspace.unsafe",
            schema_version=1,
            payload_type=UnsafeWorkspaceTransitionPayload,
        )


def test_registry_rejects_mutable_payload_type() -> None:
    events = OutboxEventRegistry()

    with pytest.raises(ValueError, match="must be frozen"):
        events.register(
            event_type="workspace.mutable",
            schema_version=1,
            payload_type=MutableWorkspaceTransitionPayload,
        )


def test_registry_rejects_shallow_frozen_payload_with_mutable_nested_value() -> None:
    events = OutboxEventRegistry()

    with pytest.raises(ValueError, match="deeply immutable"):
        events.register(
            event_type="workspace.shallow_frozen",
            schema_version=1,
            payload_type=ShallowFrozenWorkspaceTransitionPayload,
        )


def test_registry_rejects_enum_with_mutable_member_value() -> None:
    events = OutboxEventRegistry()

    with pytest.raises(ValueError, match="deeply immutable"):
        events.register(
            event_type="workspace.mutable_enum",
            schema_version=1,
            payload_type=FrozenPayloadWithMutableEnumValue,
        )


def test_registry_strictly_revalidates_constructed_payload_values() -> None:
    events = OutboxEventRegistry()
    events.register(
        event_type="workspace.tuple_payload",
        schema_version=1,
        payload_type=FrozenTuplePayload,
    )
    bypassed = FrozenTuplePayload.model_construct(labels=[])

    with pytest.raises(UnsupportedOutboxEventError, match="intent values"):
        events.build_intent(
            event_id=EVENT_ID,
            workspace_id=WORKSPACE_ID,
            event_type="workspace.tuple_payload",
            schema_version=1,
            payload=bypassed,
            trace_id=TRACE_ID,
        )


def test_payload_snapshot_is_stable_when_private_computed_state_changes() -> None:
    events = OutboxEventRegistry()
    events.register(
        event_type="workspace.computed_payload",
        schema_version=1,
        payload_type=FrozenPayloadWithPrivateComputedState,
    )
    intent = events.build_intent(
        event_id=EVENT_ID,
        workspace_id=WORKSPACE_ID,
        event_type="workspace.computed_payload",
        schema_version=1,
        payload=FrozenPayloadWithPrivateComputedState(name="workspace"),
        trace_id=TRACE_ID,
    )

    intent.payload._labels.append("changed")  # type: ignore[attr-defined]

    assert intent.payload_json() == {"labels": [], "name": "workspace"}


@pytest.mark.parametrize("value", (float("nan"), float("inf"), float("-inf")))
def test_registry_rejects_non_finite_json_numbers(value: float) -> None:
    events = OutboxEventRegistry()
    events.register(
        event_type="workspace.float_payload",
        schema_version=1,
        payload_type=FrozenFloatPayload,
    )

    with pytest.raises(UnsupportedOutboxEventError, match="intent values"):
        events.build_intent(
            event_id=EVENT_ID,
            workspace_id=WORKSPACE_ID,
            event_type="workspace.float_payload",
            schema_version=1,
            payload=FrozenFloatPayload(value=value),
            trace_id=TRACE_ID,
        )


def test_registered_event_rejects_wrong_typed_payload() -> None:
    class DifferentPayload(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True)

        workspace: OpaqueObjectReference

    with pytest.raises(UnsupportedOutboxEventError, match="payload schema"):
        registry().build_intent(
            event_id=EVENT_ID,
            workspace_id=WORKSPACE_ID,
            event_type="workspace.lifecycle_changed",
            schema_version=1,
            payload=DifferentPayload(
                workspace=OpaqueObjectReference(
                    object_type="workspace",
                    object_id=WORKSPACE_ID,
                    schema_version=1,
                )
            ),
            trace_id=TRACE_ID,
        )


def test_payload_schema_cannot_represent_undeclared_protected_content() -> None:
    with pytest.raises(ValidationError):
        WorkspaceTransitionPayload.model_validate(
            {
                "workspace": {
                    "object_type": "workspace",
                    "object_id": str(WORKSPACE_ID),
                    "schema_version": 1,
                },
                "previous_state": "provisioning",
                "current_state": "active",
                "document_text": "protected content",
                "credential": "secret",
                "raw_provider_payload": {"body": "not allowed"},
            }
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("object_type", "Workspace Display Name"),
        ("object_id", UUID(int=0)),
        ("schema_version", 0),
    ),
)
def test_opaque_reference_rejects_invalid_identity_metadata(
    field: str,
    value: object,
) -> None:
    values: dict[str, object] = {
        "object_type": "workspace",
        "object_id": WORKSPACE_ID,
        "schema_version": 1,
    }
    values[field] = value

    with pytest.raises(ValidationError):
        OpaqueObjectReference.model_validate(values)
