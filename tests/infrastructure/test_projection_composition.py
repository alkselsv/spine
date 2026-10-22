from __future__ import annotations

from uuid import UUID

import pytest

from spine.application.diagnostics import DiagnosticContext
from spine.application.document_ingestion import (
    PROJECTION_REQUEST_EVENT_TYPE,
    PROJECTION_REQUEST_SCHEMA_VERSION,
    ProjectionRequestPayload,
)
from spine.application.outbox import OutboxConsumerSuccess
from spine.application.projection_result_mapping import ProjectionPreparationStatus
from spine.application.persistence.outbox import (
    OpaqueObjectReference,
    OutboxEventRegistry,
)
from spine.application.persistence.projection import (
    PROJECTION_OUTBOX_SCHEMA_VERSION,
    PROJECTION_RECOVERY_REQUESTED_OUTBOX_EVENT_TYPE,
    ProjectionRecoveryAction,
    ProjectionRecoveryRequestedOutboxPayload,
    register_projection_outbox_events,
)
from spine.infrastructure.persistence.projection_composition import (
    PROJECTION_REQUEST_CONSUMER_NAME,
    build_postgresql_cognee_projection_delivery_binding,
    build_projection_delivery_route_binding,
    prepare_projection_delivery_binding,
)
from spine.infrastructure.persistence.parsed_artifacts import (
    InMemoryParsedArtifactRepository,
)
from spine.memory.fake import DeterministicContextProjection
from tests.application.test_projection_result_mapping import (
    _adapter_receipt,
    _command,
)
from tests.application.test_projection_request_resolution import (
    _Uow as ProjectionRequestUnitOfWork,
    _request as projection_request,
)
from tests.contracts.persistence.conftest import create_in_memory_adapter
from tests.contracts.persistence.test_projection_contract import (
    _prepare_projection_publication,
)
WORKSPACE_ID = UUID(int=53301)
ENVIRONMENT_ID = UUID(int=53302)


def _ordinary_intent():
    registry = OutboxEventRegistry()
    registry.register(
        event_type=PROJECTION_REQUEST_EVENT_TYPE,
        schema_version=PROJECTION_REQUEST_SCHEMA_VERSION,
        payload_type=ProjectionRequestPayload,
    )
    payload = ProjectionRequestPayload(
        ingestion_run_id=UUID(int=53303),
        ingestion_run_source_id=UUID(int=53304),
        source_revision_id=UUID(int=53305),
        processing_receipt_id=UUID(int=53306),
        processor_kind="document-ingestion",
        parser_version="parser:v1",
        parser_config_reference="parser-default:v1",
        locator_contract_version="locator:v1",
    )
    return registry.build_intent(
        event_id=UUID(int=53307),
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        event_type=PROJECTION_REQUEST_EVENT_TYPE,
        schema_version=PROJECTION_REQUEST_SCHEMA_VERSION,
        payload=payload,
        aggregate=OpaqueObjectReference(
            object_type="ingestion_run_source",
            object_id=payload.ingestion_run_source_id,
            schema_version=1,
        ),
        producer_deduplication_id="projection-request:53304",
        trace_id=UUID(int=53308),
    )


def _recovery_intent():
    registry = OutboxEventRegistry()
    register_projection_outbox_events(registry)
    command_id = UUID(int=53309)
    return registry.build_intent(
        event_id=UUID(int=53310),
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        event_type=PROJECTION_RECOVERY_REQUESTED_OUTBOX_EVENT_TYPE,
        schema_version=PROJECTION_OUTBOX_SCHEMA_VERSION,
        payload=ProjectionRecoveryRequestedOutboxPayload(
            command_id=command_id,
            projection_kind="context",
            action=ProjectionRecoveryAction.REBUILD,
            command_digest="5" * 64,
        ),
        aggregate=OpaqueObjectReference(
            object_type="projection_recovery_request",
            object_id=command_id,
            schema_version=1,
        ),
        producer_deduplication_id="projection-recovery:53309",
        trace_id=UUID(int=53311),
    )


class RecordingProjectionConsumer:
    def __init__(self) -> None:
        self.intents = []

    async def consume(self, intent, diagnostic_context):
        self.intents.append(intent)
        return OutboxConsumerSuccess(diagnostic_context=diagnostic_context)


class RecordingProjectionResults:
    def __init__(self) -> None:
        self.prepared = []
        self.mapped = []

    async def prepare(self, command):
        self.prepared.append(command)
        return ProjectionPreparationStatus.READY

    async def map(self, command, receipt):
        mapped = (command, receipt)
        self.mapped.append(mapped)
        return mapped


class RecordingCompletion:
    def __init__(self) -> None:
        self.mappings = []

    async def complete(self, mapping) -> None:
        self.mappings.append(mapping)


class SyntheticReceiptBuilder:
    async def build(self, command, result):
        del command
        return _adapter_receipt(_command()).model_copy(update={"result": result})


def _ordinary_production_intent():
    request = projection_request()
    registry = OutboxEventRegistry()
    registry.register(
        event_type=PROJECTION_REQUEST_EVENT_TYPE,
        schema_version=PROJECTION_REQUEST_SCHEMA_VERSION,
        payload_type=ProjectionRequestPayload,
    )
    return registry.build_intent(
        event_id=request.event_id,
        workspace_id=request.workspace_id,
        environment_id=request.environment_id,
        event_type=PROJECTION_REQUEST_EVENT_TYPE,
        schema_version=PROJECTION_REQUEST_SCHEMA_VERSION,
        payload=request.payload,
        aggregate=OpaqueObjectReference(
            object_type="ingestion_run_source",
            object_id=request.payload.ingestion_run_source_id,
            schema_version=1,
        ),
        producer_deduplication_id=(
            f"projection-request:{request.payload.ingestion_run_source_id}"
        ),
        trace_id=UUID(int=53313),
        correlation_id=request.payload.ingestion_run_id,
        causation_id=UUID(int=53317),
    )


def _recovery_production_intent(prepared):
    activation = prepared.activated.activation
    registry = OutboxEventRegistry()
    register_projection_outbox_events(registry)
    command_id = UUID(int=53314)
    return registry.build_intent(
        event_id=UUID(int=53315),
        workspace_id=activation.workspace_id,
        environment_id=activation.environment_id,
        event_type=PROJECTION_RECOVERY_REQUESTED_OUTBOX_EVENT_TYPE,
        schema_version=PROJECTION_OUTBOX_SCHEMA_VERSION,
        payload=ProjectionRecoveryRequestedOutboxPayload(
            command_id=command_id,
            projection_kind="qa",
            action=ProjectionRecoveryAction.REBUILD,
            command_digest="5" * 64,
        ),
        aggregate=OpaqueObjectReference(
            object_type="projection_recovery_request",
            object_id=command_id,
            schema_version=1,
        ),
        producer_deduplication_id=f"projection-recovery:{command_id}",
        trace_id=UUID(int=53316),
    )


@pytest.mark.asyncio
async def test_projection_delivery_prepares_only_ordinary_request_once() -> None:
    ordinary_consumer = RecordingProjectionConsumer()
    recovery_consumer = RecordingProjectionConsumer()
    prepared = []

    async def prepare(intent) -> None:
        prepared.append(intent)

    binding = prepare_projection_delivery_binding(
        build_projection_delivery_route_binding(
            projection_request_consumer=ordinary_consumer,
            recovery_request_consumer=recovery_consumer,
        ),
        prepare,
    )
    ordinary = _ordinary_intent()
    recovery = _recovery_intent()
    context = DiagnosticContext(trace_id=UUID(int=53312))

    ordinary_result = await binding.consumer.consume(ordinary, context)
    recovery_result = await binding.consumer.consume(recovery, context)

    assert binding.consumer_name == PROJECTION_REQUEST_CONSUMER_NAME
    assert prepared == [ordinary]
    assert ordinary_consumer.intents == [ordinary]
    assert recovery_consumer.intents == [recovery]
    assert ordinary_result == OutboxConsumerSuccess(diagnostic_context=context)
    assert recovery_result == OutboxConsumerSuccess(diagnostic_context=context)


@pytest.mark.asyncio
async def test_postgresql_cognee_delivery_binding_completes_request_and_recovery() -> None:
    adapter = await create_in_memory_adapter()
    prepared = await _prepare_projection_publication(adapter, seed=53320)
    request_uow = ProjectionRequestUnitOfWork()
    request_context = object()

    def uow_factory(context):
        if context is request_context:
            return request_uow
        return adapter.uow_factory(context)

    def projection_context(command):
        return adapter.ingestion_context(
            command.workspace_id,
            command.environment_id,
            "projection_run",
        )

    projection = DeterministicContextProjection()
    results = RecordingProjectionResults()
    completion = RecordingCompletion()
    binding = build_postgresql_cognee_projection_delivery_binding(
        session_factory=lambda: None,
        parsed_artifact_context_factory=lambda workspace_id, environment_id: None,
        context_verifier=object(),
        uow_factory=uow_factory,
        request_context_factory=lambda envelope: request_context,
        projection_context_factory=projection_context,
        recovery_context_factory=lambda envelope: adapter.ingestion_context(
            envelope.workspace_id,
            envelope.environment_id,
            "projection_run",
        ),
        projection=projection,
        clock=lambda: prepared.activated.activation.activated_at,
        completion=completion,
        parsed_artifacts=InMemoryParsedArtifactRepository(),
        receipt_builder=SyntheticReceiptBuilder(),
        result_bridge=results,
    )
    ordinary = _ordinary_production_intent()
    recovery = _recovery_production_intent(prepared)

    ordinary_context = DiagnosticContext(
        trace_id=ordinary.trace_id,
        correlation_id=ordinary.correlation_id,
        causation_id=ordinary.event_id,
    )
    recovery_context = DiagnosticContext(
        trace_id=recovery.trace_id,
        causation_id=recovery.event_id,
    )
    ordinary_result = await binding.consumer.consume(ordinary, ordinary_context)
    recovery_result = await binding.consumer.consume(recovery, recovery_context)

    assert ordinary_result.diagnostic_context == ordinary_context
    assert recovery_result.diagnostic_context == recovery_context
    assert [command.operation.value for command in projection.recorded_commands] == [
        "project",
        "rebuild",
    ]
    assert len(results.prepared) == 2
    assert len(results.mapped) == 2
    assert completion.mappings == [results.mapped[0]]
