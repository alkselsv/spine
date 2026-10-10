from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Annotated, Any, Literal
from uuid import UUID

import pytest
from pydantic import (
    BaseModel,
    ConfigDict,
    PlainSerializer,
    ValidationError,
    computed_field,
    create_model,
    model_serializer,
)

from spine.application.diagnostics import (
    DiagnosticContext,
    DiagnosticEventRegistry,
    DiagnosticIdentifier,
    DiagnosticContextMismatchError,
    DiagnosticObjectReference,
    DiagnosticSeverity,
    FailureDiagnosticPayload,
    OutboxDeliveryDiagnosticPayload,
    UnsupportedDiagnosticEventError,
    emit_diagnostic_safely,
    validate_diagnostic_event_for_context,
)
from spine.infrastructure.diagnostics import RecordingDiagnosticSink


class UnsafeBytesEnum(Enum):
    VALUE = b"Bearer production-token"


class UnsafeEnumPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    safe_state: UnsafeBytesEnum


class UnsafeLiteralPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    safe_state: Literal[b"Bearer production-token"]


class UnsafeStringEnum(str, Enum):
    VALUE = "secret"


class UnsafeNamedEnumPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    raw_error_code: UnsafeStringEnum


class UnsafeNamedLiteralPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    raw_error_code: Literal["secret"]


def synthetic_uuid(value: int) -> UUID:
    return UUID(int=value)


@pytest.mark.asyncio
async def test_registered_event_is_recorded_as_a_detached_immutable_snapshot() -> None:
    registry = DiagnosticEventRegistry.with_default_families()
    sink = RecordingDiagnosticSink(registry)

    event = registry.build_event(
        event_type="failure.observed",
        schema_version=1,
        severity=DiagnosticSeverity.ERROR,
        occurred_at=datetime(2026, 10, 10, 12, 30, tzinfo=timezone.utc),
        diagnostic_context=DiagnosticContext(
            trace_id=synthetic_uuid(1),
            correlation_id=synthetic_uuid(2),
            causation_id=synthetic_uuid(3),
        ),
        workspace_id=synthetic_uuid(4),
        environment_id=synthetic_uuid(5),
        payload=FailureDiagnosticPayload(
            failure_code="authorization.unavailable",
        ),
    )

    await sink.emit(event)

    assert sink.events == (event,)
    assert sink.events[0] is not event
    assert sink.events[0].payload is not event.payload
    assert sink.events[0].payload_json() == {
        "failure_code": "authorization.unavailable",
    }
    with pytest.raises(ValidationError):
        sink.events[0].severity = DiagnosticSeverity.INFO  # type: ignore[misc]


def test_registry_rejects_payload_with_mutable_fields() -> None:
    class MutablePayload(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True)

        values: list[int]

    registry = DiagnosticEventRegistry()

    with pytest.raises(ValueError, match="deeply immutable"):
        registry.register(
            event_type="unsafe.mutable",
            schema_version=1,
            payload_type=MutablePayload,
        )


def test_registry_rejects_payload_with_unbounded_text() -> None:
    class ContentPayload(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True)

        message: str

    registry = DiagnosticEventRegistry()

    with pytest.raises(ValueError, match="bounded safe fields"):
        registry.register(
            event_type="unsafe.content",
            schema_version=1,
            payload_type=ContentPayload,
        )


def test_registry_rejects_content_bearing_identifier_field_name() -> None:
    class CredentialPayload(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True)

        raw_token: DiagnosticIdentifier

    registry = DiagnosticEventRegistry()

    with pytest.raises(ValueError, match="content-bearing"):
        registry.register(
            event_type="unsafe.credential",
            schema_version=1,
            payload_type=CredentialPayload,
        )


def test_secret_field_name_cannot_hide_behind_safe_identifier_suffix() -> None:
    class CredentialCodePayload(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True)

        raw_token_code: DiagnosticIdentifier

    registry = DiagnosticEventRegistry()

    with pytest.raises(ValueError, match="content-bearing"):
        registry.register(
            event_type="unsafe.credential_code",
            schema_version=1,
            payload_type=CredentialCodePayload,
        )


@pytest.mark.parametrize("payload_type", (UnsafeEnumPayload, UnsafeLiteralPayload))
def test_registry_rejects_secret_bearing_enum_or_literal_values(
    payload_type: type[BaseModel],
) -> None:
    registry = DiagnosticEventRegistry()

    with pytest.raises(ValueError, match="bounded safe fields"):
        registry.register(
            event_type="unsafe.encoded_secret",
            schema_version=1,
            payload_type=payload_type,
        )


def test_registry_rejects_computed_payload_fields() -> None:
    class ComputedLeakPayload(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True)

        count: int

        @computed_field
        @property
        def credential(self) -> str:
            return "production-token"

    registry = DiagnosticEventRegistry()

    with pytest.raises(ValueError, match="computed fields"):
        registry.register(
            event_type="unsafe.computed",
            schema_version=1,
            payload_type=ComputedLeakPayload,
        )


def test_registry_rejects_custom_payload_serializers() -> None:
    class CustomSerializerPayload(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True)

        count: int

        @model_serializer
        def serialize(self) -> dict[str, object]:
            return {"count": self.count, "credential": "production-token"}

    registry = DiagnosticEventRegistry()

    with pytest.raises(ValueError, match="custom serializers"):
        registry.register(
            event_type="unsafe.serializer",
            schema_version=1,
            payload_type=CustomSerializerPayload,
        )


def test_registry_rejects_annotated_payload_serializers() -> None:
    class AnnotatedSerializerPayload(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True)

        count: Annotated[
            int,
            PlainSerializer(
                lambda _: "production-token",
                return_type=str,
                when_used="json",
            ),
        ]

    registry = DiagnosticEventRegistry()

    with pytest.raises(ValueError, match="custom serializers"):
        registry.register(
            event_type="unsafe.annotated_serializer",
            schema_version=1,
            payload_type=AnnotatedSerializerPayload,
        )


def test_registry_rejects_overridden_payload_dump() -> None:
    class OverriddenDumpPayload(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True)

        count: int

        def model_dump(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            if kwargs.get("mode") == "json":
                return {"count": "production-token"}
            return super().model_dump(*args, **kwargs)

    registry = DiagnosticEventRegistry()

    with pytest.raises(ValueError, match="custom serializers"):
        registry.register(
            event_type="unsafe.overridden_dump",
            schema_version=1,
            payload_type=OverriddenDumpPayload,
        )


@pytest.mark.parametrize(
    "payload_type, values",
    (
        (FailureDiagnosticPayload, {"failure_code": "production-token"}),
        (
            OutboxDeliveryDiagnosticPayload,
            {"delivery_state": "production-token", "attempt_number": 1},
        ),
    ),
)
def test_default_payloads_reject_unregistered_identifier_shaped_values(
    payload_type: type[BaseModel],
    values: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        payload_type.model_validate(values)


@pytest.mark.parametrize(
    "payload_type",
    (UnsafeNamedEnumPayload, UnsafeNamedLiteralPayload),
)
def test_content_bearing_enum_or_literal_field_names_are_rejected(
    payload_type: type[BaseModel],
) -> None:
    registry = DiagnosticEventRegistry()

    with pytest.raises(ValueError, match="content-bearing"):
        registry.register(
            event_type="unsafe.raw_error",
            schema_version=1,
            payload_type=payload_type,
        )


@pytest.mark.parametrize(
    "field_name",
    (
        "protected_content",
        "secret_value",
        "raw_failure",
        "sql_text",
        "prompt_text",
        "answer_text",
        "chain_of_thought",
    ),
)
def test_negative_schema_corpus_is_rejected(field_name: str) -> None:
    payload_type = create_model(
        "UnsafeCorpusPayload",
        __config__=ConfigDict(extra="forbid", frozen=True),
        **{field_name: (DiagnosticIdentifier, ...)},
    )
    registry = DiagnosticEventRegistry()

    with pytest.raises(ValueError, match="content-bearing"):
        registry.register(
            event_type="unsafe.corpus",
            schema_version=1,
            payload_type=payload_type,
        )


def test_default_registry_supports_safe_outbox_delivery_observations() -> None:
    registry = DiagnosticEventRegistry.with_default_families()

    event = registry.build_event(
        event_type="outbox.delivery",
        schema_version=1,
        severity=DiagnosticSeverity.WARNING,
        occurred_at=datetime(2026, 10, 10, 12, 30, tzinfo=timezone.utc),
        diagnostic_context=DiagnosticContext(trace_id=synthetic_uuid(10)),
        workspace_id=synthetic_uuid(11),
        payload=OutboxDeliveryDiagnosticPayload(
            delivery_state="retry_scheduled",
            attempt_number=2,
            retry_delay=timedelta(seconds=30),
        ),
    )

    assert event.payload_json() == {
        "attempt_number": 2,
        "delivery_state": "retry_scheduled",
        "retry_delay": "PT30S",
    }


@pytest.mark.parametrize(
    "substitution",
    (
        {"trace_id": synthetic_uuid(20)},
        {"workspace_id": synthetic_uuid(21)},
        {"environment_id": synthetic_uuid(22)},
    ),
)
def test_context_or_scope_substitution_is_rejected_before_emission(
    substitution: dict[str, object],
) -> None:
    registry = DiagnosticEventRegistry.with_default_families()
    context = DiagnosticContext(
        trace_id=synthetic_uuid(1),
        correlation_id=synthetic_uuid(2),
        causation_id=synthetic_uuid(3),
    )
    event = registry.build_event(
        event_type="failure.observed",
        schema_version=1,
        severity=DiagnosticSeverity.ERROR,
        occurred_at=datetime(2026, 10, 10, 12, 30, tzinfo=timezone.utc),
        diagnostic_context=context,
        workspace_id=synthetic_uuid(4),
        environment_id=synthetic_uuid(5),
        payload=FailureDiagnosticPayload(failure_code="authorization.denied"),
    ).model_copy(update=substitution)

    with pytest.raises(DiagnosticContextMismatchError):
        validate_diagnostic_event_for_context(
            event,
            registry=registry,
            diagnostic_context=context,
            workspace_id=synthetic_uuid(4),
            environment_id=synthetic_uuid(5),
        )


@pytest.mark.asyncio
async def test_sink_failure_does_not_rewrite_a_committed_outcome() -> None:
    class FailingSink:
        async def emit(self, event: object) -> None:
            del event
            raise RuntimeError("provider payload must remain private")

    registry = DiagnosticEventRegistry.with_default_families()
    event = registry.build_event(
        event_type="failure.observed",
        schema_version=1,
        severity=DiagnosticSeverity.ERROR,
        occurred_at=datetime(2026, 10, 10, 12, 30, tzinfo=timezone.utc),
        diagnostic_context=DiagnosticContext(trace_id=synthetic_uuid(30)),
        payload=FailureDiagnosticPayload(failure_code="internal.unexpected"),
    )
    canonical_outcome = "committed"

    await emit_diagnostic_safely(FailingSink(), event)

    assert canonical_outcome == "committed"


def test_registry_accepts_opaque_versioned_object_references() -> None:
    class ReferencePayload(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True)

        target: DiagnosticObjectReference

    registry = DiagnosticEventRegistry()
    registry.register(
        event_type="object.observed",
        schema_version=1,
        payload_type=ReferencePayload,
    )
    registry.seal()

    event = registry.build_event(
        event_type="object.observed",
        schema_version=1,
        severity=DiagnosticSeverity.INFO,
        occurred_at=datetime(2026, 10, 10, 12, 30, tzinfo=timezone.utc),
        diagnostic_context=DiagnosticContext(trace_id=synthetic_uuid(40)),
        payload=ReferencePayload(
            target=DiagnosticObjectReference(
                object_type="outbox_event",
                object_id=synthetic_uuid(41),
                schema_version=1,
            )
        ),
    )

    assert event.payload_json() == {
        "target": {
            "object_id": str(synthetic_uuid(41)),
            "object_type": "outbox_event",
            "schema_version": 1,
        }
    }


@pytest.mark.parametrize("payload", ({"failure_code": "safe"}, RuntimeError("raw")))
def test_event_construction_rejects_non_model_payloads(payload: object) -> None:
    registry = DiagnosticEventRegistry.with_default_families()

    with pytest.raises(UnsupportedDiagnosticEventError):
        registry.build_event(
            event_type="failure.observed",
            schema_version=1,
            severity=DiagnosticSeverity.ERROR,
            occurred_at=datetime(2026, 10, 10, 12, 30, tzinfo=timezone.utc),
            diagnostic_context=DiagnosticContext(trace_id=synthetic_uuid(50)),
            payload=payload,  # type: ignore[arg-type]
        )


def test_payload_construction_rejects_undeclared_fields() -> None:
    with pytest.raises(ValidationError):
        FailureDiagnosticPayload(
            failure_code="authorization.denied",
            raw_token="secret",  # type: ignore[call-arg]
        )
