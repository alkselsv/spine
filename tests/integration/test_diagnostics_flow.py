from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
from uuid import UUID

import httpx
import pytest
from fastapi import Depends
from pydantic import BaseModel, ConfigDict

from spine.api.authorization import (
    ApiAuthorizationRuntime,
    add_protected_api_route,
    get_authorized_request_context,
)
from spine.api.main import create_app, get_diagnostic_context
from spine.application.diagnostics import (
    AuditEventRegistry,
    DiagnosticContext,
    DiagnosticEventRegistry,
    DiagnosticEvent,
)
from spine.application.persistence import PersistenceOperation, PersistencePurpose
from spine.application.outbox import OutboxConsumerSuccess
from spine.application.persistence import PersistenceUnavailableError
from spine.application.persistence.idempotency import IdempotencyKey
from spine.application.persistence.outbox import OutboxIntent
from spine.application.persistence.repositories import SourceObservationCommand
from spine.auth import (
    AccessTokenCredential,
    AuthenticatedAlias,
    AuthenticationAlias,
    AuthorizationRole,
    AuthorizationScopeRequirement,
    AuthorizedRequestContext,
    InMemoryAuthorizationDirectory,
    InMemoryAuthorizationEntry,
    RouteAuthorizationPolicy,
    RoutePolicyRegistry,
)
from spine.auth.in_memory import AuthorizationRecordState
from spine.infrastructure.auth import RequestAuthorizationComposer, TrustedRequestContextBoundary
from spine.domain.audit import AuditOutcome, CommandAuditPayload
from spine.domain.common import EnvironmentKind
from spine.domain.sources import (
    IdentityMode,
    RevisionKind,
    RevisionMetadata,
    SourceObject,
    SourceRevision,
    SourceRevisionProvenance,
    canonical_revision_metadata_digest,
)
from spine.domain.sources.canonicalization import content_revision
from spine.domain.workspaces import Environment
from spine.infrastructure.object_storage.contracts import ObjectReference
from spine.infrastructure.diagnostics import (
    JsonLoggingDiagnosticSink,
    RecordingDiagnosticSink,
)
from spine.infrastructure.persistence.contexts import TrustedContextBoundary
from spine.workers import OutboxConsumerBinding, create_outbox_worker
from tests.contracts.persistence.conftest import create_in_memory_adapter
from tests.contracts.persistence.ids import synthetic_uuid
from tests.contracts.persistence.test_outbox_writer_contract import intent, workspace
from tests.contracts.diagnostics.fixtures import (
    DIAGNOSTIC_LEAK_CORPUS,
    assert_downstream_diagnostic_context,
    assert_no_diagnostic_leaks,
)


NOW = datetime(2026, 10, 11, 14, tzinfo=timezone.utc)


def synthetic_source_observation(
    workspace_id: UUID,
    environment_id: UUID,
) -> tuple[SourceObject, SourceRevision, SourceRevisionProvenance, SourceObservationCommand]:
    source = SourceObject(
        source_object_id=synthetic_uuid(6787),
        workspace_id=workspace_id,
        environment_id=environment_id,
        source_kind="document",
        identity_mode=IdentityMode.UPLOAD,
        upload_identity="issue68-upload",
        created_at=NOW,
    )
    metadata = RevisionMetadata(embedded_title="Issue 68 synthetic document")
    revision = SourceRevision(
        revision_id=synthetic_uuid(6788),
        source_object_id=source.source_object_id,
        workspace_id=workspace_id,
        environment_id=environment_id,
        kind=RevisionKind.CONTENT,
        revision_digest="0" * 64,
        revision_schema_version="source-revision:v1",
        revision_metadata_schema="revision-metadata:r1-document-v1",
        revision_metadata=metadata,
        revision_metadata_digest=canonical_revision_metadata_digest(metadata),
        original_reference=ObjectReference(
            schema_version=1,
            object_id=synthetic_uuid(6789),
            storage_generation=synthetic_uuid(6790),
            digest_algorithm="sha256",
            digest_hex="c" * 64,
            byte_length=4,
        ),
        original_sha256="c" * 64,
        byte_length=4,
        media_type="text/plain",
        observed_at=NOW,
    )
    revision_values = revision.model_dump()
    revision_values.pop("revision_digest")
    revision = content_revision(**revision_values)
    provenance = SourceRevisionProvenance(
        provenance_id=synthetic_uuid(6791),
        source_object_id=source.source_object_id,
        revision_id=revision.revision_id,
        workspace_id=workspace_id,
        environment_id=environment_id,
        producer_kind="synthetic",
        producer_reference="issue68",
        event_identity="issue68-event-1",
        event_digest="d" * 64,
        observer_service="issue68-test",
        received_at=NOW,
        observed_at=NOW,
    )
    command = SourceObservationCommand.create(
        source=source,
        revision=revision,
        provenance=provenance,
        idempotency_key=IdempotencyKey("issue68-source-observation"),
    )
    return source, revision, provenance, command


class SyntheticOutboxCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    trace_id: UUID | None = None
    workspace_id: UUID | None = None
    environment_id: UUID | None = None
    actor_id: UUID | None = None
    protected_value: str | None = None


class RecordingConsumer:
    def __init__(self) -> None:
        self.intents: list[OutboxIntent] = []
        self.contexts: list[DiagnosticContext] = []

    async def consume(
        self,
        intent: OutboxIntent,
        diagnostic_context: DiagnosticContext,
    ) -> OutboxConsumerSuccess:
        self.intents.append(intent)
        self.contexts.append(diagnostic_context)
        return OutboxConsumerSuccess(diagnostic_context=diagnostic_context)


class RecordingAndLoggingSink:
    """Exercise the two production sink adapters with one validated event."""

    def __init__(self, registry: DiagnosticEventRegistry, logger: logging.Logger) -> None:
        self.recording = RecordingDiagnosticSink(registry)
        self.logging = JsonLoggingDiagnosticSink(registry, logger=logger)

    @property
    def events(self) -> tuple[DiagnosticEvent, ...]:
        return self.recording.events

    async def emit(self, event: DiagnosticEvent) -> None:
        await self.recording.emit(event)
        await self.logging.emit(event)


async def test_synthetic_asgi_to_worker_flow_preserves_trace_without_leaks() -> None:
    adapter = await create_in_memory_adapter()
    workspace_id = synthetic_uuid(6751)
    event_id = synthetic_uuid(6752)
    trusted_context = adapter.workspace_context(workspace_id)
    trace_id = trusted_context.trace_id
    untrusted_trace_id = synthetic_uuid(6753)
    untrusted_workspace_id = synthetic_uuid(6756)
    untrusted_environment_id = synthetic_uuid(6757)
    untrusted_actor_id = synthetic_uuid(6758)
    protected_value = "Bearer production-token protected customer document excerpt"
    async with adapter.uow_factory(trusted_context) as uow:
        await uow.workspaces.add(workspace(workspace_id))
        await uow.commit()

    diagnostic_events = DiagnosticEventRegistry.with_default_families()
    diagnostic_sink = RecordingDiagnosticSink(diagnostic_events)
    application = create_app(
        trace_id_source=lambda: trace_id,
        diagnostic_registry=diagnostic_events,
        diagnostic_sink=diagnostic_sink,
        clock=lambda: NOW,
    )

    @application.post("/api/v1/synthetic-outbox-command", status_code=202)
    async def submit_command(
        command: SyntheticOutboxCommand,
        diagnostic_context: DiagnosticContext = Depends(get_diagnostic_context),
    ) -> dict[str, str]:
        del command
        context = adapter.workspace_context(workspace_id)
        assert context.trace_id == diagnostic_context.trace_id
        async with adapter.uow_factory(context) as uow:
            await uow.outbox.append(
                intent(
                    adapter,
                    workspace_id=workspace_id,
                    event_id=event_id,
                    trace_id=diagnostic_context.trace_id,
                    producer_deduplication_id=None,
                )
            )
            await uow.commit()
        return {"event_id": str(event_id)}

    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/synthetic-outbox-command?trace_id=" + str(untrusted_trace_id),
            headers={"x-trace-id": str(untrusted_trace_id)},
            json={
                "trace_id": str(untrusted_trace_id),
                "workspace_id": str(untrusted_workspace_id),
                "environment_id": str(untrusted_environment_id),
                "actor_id": str(untrusted_actor_id),
                "protected_value": protected_value,
            },
        )

    consumer = RecordingConsumer()
    worker = create_outbox_worker(
        repository=adapter.delivery_repository,
        consumer_bindings=(
            OutboxConsumerBinding(
                consumer_name="workspace_projection",
                consumer=consumer,
            ),
        ),
        audit_events=adapter.audit_events,
        diagnostic_events=diagnostic_events,
        diagnostic_sink=diagnostic_sink,
        service_principal_id=synthetic_uuid(6754),
        clock=iter((NOW + timedelta(seconds=1), NOW + timedelta(seconds=2))).__next__,
        audit_event_id_source=lambda: synthetic_uuid(6755),
        retry_delay=lambda attempt: timedelta(seconds=attempt),
        maximum_retry_delay=timedelta(minutes=5),
        lease_duration=timedelta(seconds=30),
        handler_timeout=timedelta(seconds=5),
        maximum_attempts=3,
    )
    report = await worker.dispatch_batch(limit=1, worker_identity="worker_alpha")

    assert response.status_code == 202
    assert response.headers["x-trace-id"] == str(trace_id)
    assert response.json() == {"event_id": str(event_id)}
    assert report.delivered == 1
    assert set(report.model_dump()) == {
        "claimed",
        "delivered",
        "retry_scheduled",
        "quarantined",
        "outcome_codes",
    }
    assert len(consumer.contexts) == 1
    assert_downstream_diagnostic_context(
        consumer.contexts[0],
        trace_id=trace_id,
        causation_id=event_id,
    )
    assert consumer.intents[0].workspace_id == workspace_id
    assert {event.trace_id for event in diagnostic_sink.events} == {trace_id}
    payload_snapshot = consumer.intents[0].payload_snapshot
    assert payload_snapshot is not None
    assert_no_diagnostic_leaks(
        (
            response.text,
            *(event.model_dump_json() for event in diagnostic_sink.events),
        ),
        leak_corpus=(
            protected_value,
            str(untrusted_trace_id),
            str(untrusted_workspace_id),
            str(untrusted_environment_id),
            str(untrusted_actor_id),
            payload_snapshot.decode("utf-8"),
        ),
    )


async def test_issue68_composed_path_covers_all_safe_surfaces(
    caplog: pytest.LogCaptureFixture,
) -> None:
    adapter = await create_in_memory_adapter()
    workspace_id = synthetic_uuid(6771)
    event_id = synthetic_uuid(6772)
    command_audit_id = synthetic_uuid(6773)
    delivery_audit_id = synthetic_uuid(6774)
    correlation_id = synthetic_uuid(6776)
    environment_id = synthetic_uuid(6780)
    setup_context = adapter.workspace_context(workspace_id)
    trace_id = adapter.source_context(workspace_id, environment_id).trace_id
    protected_values = tuple(DIAGNOSTIC_LEAK_CORPUS)
    protected_input = " | ".join(protected_values)
    untrusted_trace_id = synthetic_uuid(6777)
    untrusted_workspace_id = synthetic_uuid(6778)
    untrusted_environment_id = synthetic_uuid(6779)

    async with adapter.uow_factory(setup_context) as uow:
        await uow.workspaces.add(workspace(workspace_id))
        await uow.environments.add(
            Environment(
                id=environment_id,
                workspace_id=workspace_id,
                kind=EnvironmentKind.PRODUCTION,
                display_name="Issue 68 environment",
            )
        )
        await uow.commit()

    registry = DiagnosticEventRegistry.with_default_families()
    logger = logging.getLogger("spine.tests.issue68.composed")
    sink = RecordingAndLoggingSink(registry, logger)

    class StaticAuthentication:
        async def authenticate(
            self,
            credentials: AccessTokenCredential,
            diagnostic_context: DiagnosticContext,
        ) -> AuthenticatedAlias:
            del credentials, diagnostic_context
            return AuthenticatedAlias(
                alias=AuthenticationAlias(
                    issuer="https://identity.example.test",
                    subject="issue68-synthetic-administrator",
                ),
                authentication_configuration_version="oidc-2026-10-11",
            )

    class ImmediateAuditGate:
        async def release_after_audit(
            self,
            *,
            context: object,
            event: object,
            release: object,
        ) -> object:
            del context, event
            return await release(synthetic_uuid(6781))  # type: ignore[operator]

    auth_alias = AuthenticationAlias(
        issuer="https://identity.example.test",
        subject="issue68-synthetic-administrator",
    )
    route_policies = RoutePolicyRegistry()
    request_boundary = TrustedRequestContextBoundary.for_testing(
        persistence_boundary=TrustedContextBoundary.for_testing(
            issuer_id=synthetic_uuid(6782),
            secret=b"issue68-persistence-context-secret-0001",
        ),
        issuer_id=synthetic_uuid(6783),
        secret=b"issue68-request-context-secret-000001",
    )

    async def _ready() -> bool:
        return True

    command_policy = RouteAuthorizationPolicy(
        required_roles=frozenset({AuthorizationRole.ADMINISTRATOR}),
        required_scope=AuthorizationScopeRequirement.WORKSPACE_ENVIRONMENT,
        purpose=PersistencePurpose("knowledge_control"),
        operation=PersistenceOperation("list_sources"),
        service_principal_id=synthetic_uuid(6784),
    )
    authorization_directory = InMemoryAuthorizationDirectory(
        entries=(
            InMemoryAuthorizationEntry(
                alias=auth_alias,
                canonical_human_identity_id=synthetic_uuid(6785),
                identity_state=AuthorizationRecordState.ACTIVE,
                alias_state=AuthorizationRecordState.ACTIVE,
                workspace_id=workspace_id,
                workspace_membership_state=AuthorizationRecordState.ACTIVE,
                environment_id=environment_id,
                environment_workspace_id=workspace_id,
                environment_membership_state=AuthorizationRecordState.ACTIVE,
                roles=frozenset({AuthorizationRole.ADMINISTRATOR}),
                role_binding_state=AuthorizationRecordState.ACTIVE,
                snapshot_generation=1,
                current_generation=1,
            ),
        )
    )
    composer = RequestAuthorizationComposer(
        authentication=StaticAuthentication(),  # type: ignore[arg-type]
        authorization_directory=authorization_directory,
        route_policies=route_policies,
        request_boundary=request_boundary,
        audit_registry=AuditEventRegistry.with_default_families(),
        audit_gate=ImmediateAuditGate(),
        diagnostic_registry=registry,
        diagnostic_sink=sink,
        clock=lambda: NOW,
        request_id_source=lambda: synthetic_uuid(6786),
    )
    application = create_app(
        trace_id_source=lambda: trace_id,
        diagnostic_registry=registry,
        diagnostic_sink=sink,
        clock=lambda: NOW,
        authorization_runtime=ApiAuthorizationRuntime(
            composer=composer,
            route_policies=route_policies,
            readiness_probe=lambda: _ready(),
        ),
    )

    class ValidationCommand(BaseModel):
        count: int

    @application.post("/api/v1/issue68-validation")
    async def validation_command(body: ValidationCommand) -> dict[str, int]:
        return {"count": body.count}

    async def synthetic_command(
        body: SyntheticOutboxCommand,
        diagnostic_context: DiagnosticContext = Depends(get_diagnostic_context),
        authorized_context: AuthorizedRequestContext[DiagnosticContext] = Depends(
            get_authorized_request_context
        ),
    ) -> dict[str, str]:
        del body
        authorized_scope = authorized_context.scope
        assert authorized_scope.workspace_id == workspace_id
        assert authorized_scope.environment_id == environment_id
        assert authorized_context.diagnostic_context.trace_id == diagnostic_context.trace_id
        persistence_context = adapter.source_context(
            authorized_scope.workspace_id,
            authorized_scope.environment_id,
        )
        _, expected_revision, _, observation = synthetic_source_observation(
            authorized_scope.workspace_id,
            authorized_scope.environment_id,
        )
        async with adapter.uow_factory(persistence_context) as uow:
            canonical_mutation = await uow.sources.record_observation(observation)
            assert canonical_mutation.revision.revision_id == expected_revision.revision_id
            await uow.audit.append(
                adapter.audit_events.build_event(
                    audit_event_id=command_audit_id,
                    workspace_id=workspace_id,
                    environment_id=environment_id,
                    event_type="command.outcome",
                    schema_version=1,
                    payload=CommandAuditPayload(command_type="synthetic.command"),
                    origin=persistence_context.origin,
                    acting_subject_id=persistence_context.acting_subject_id,
                    service_principal_id=persistence_context.service_principal_id,
                    trace_id=diagnostic_context.trace_id,
                    occurred_at=NOW,
                    outcome=AuditOutcome.ACCEPTED,
                    reason="command.accepted",
                    producer_deduplication_id="command.synthetic",
                )
            )
            outbox_intent = intent(
                    adapter,
                    workspace_id=workspace_id,
                    event_id=event_id,
                    environment_id=environment_id,
                    trace_id=diagnostic_context.trace_id,
                    producer_deduplication_id=None,
                ).model_copy(
                    update={
                        "correlation_id": correlation_id,
                        "causation_id": command_audit_id,
                    }
                )
            await uow.outbox.append(outbox_intent)
            await uow.commit()
        return {"event_id": str(event_id)}

    add_protected_api_route(
        application,
        "/api/v1/issue68-command",
        synthetic_command,
        route_id="issue68.synthetic.command",
        policy=command_policy,
        methods=["POST"],
        status_code=202,
    )

    @application.get("/api/v1/issue68-failure")
    async def known_failure() -> None:
        raise PersistenceUnavailableError(protected_input)

    transport = httpx.ASGITransport(app=application)
    with caplog.at_level(logging.INFO, logger=logger.name):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/v1/issue68-command",
                params={"question": protected_input},
                headers={
                    "x-trace-id": str(untrusted_trace_id),
                    "Authorization": "Bearer synthetic-access-token",
                    "X-Workspace-ID": str(workspace_id),
                    "X-Environment-ID": str(environment_id),
                },
                json={
                    "trace_id": str(untrusted_trace_id),
                    "workspace_id": str(untrusted_workspace_id),
                    "environment_id": str(untrusted_environment_id),
                    "protected_value": protected_input,
                },
            )
            validation = await client.post(
                "/api/v1/issue68-validation",
                params={"question": protected_input},
                json={"count": protected_input},
            )
            failure = await client.get(
                "/api/v1/issue68-failure",
                params={"question": protected_input},
            )

    # The route intentionally persists only registered safe contracts: the
    # protected command body is accepted by ingress but never reaches storage.
    assert response.status_code == 202
    assert response.json() == {"event_id": str(event_id)}
    assert response.headers["x-trace-id"] == str(trace_id)
    assert validation.status_code == 422
    assert failure.status_code == 503
    assert validation.headers["x-trace-id"] == str(trace_id)
    assert failure.headers["x-trace-id"] == str(trace_id)
    assert failure.json() == {
        "schema_version": 1,
        "code": "spine.persistence.unavailable",
        "safe_message": "Persistence is temporarily unavailable.",
        "retryability": "after_delay",
        "trace_id": str(trace_id),
    }

    consumer = RecordingConsumer()
    worker = create_outbox_worker(
        repository=adapter.delivery_repository,
        consumer_bindings=(
            OutboxConsumerBinding(
                consumer_name="workspace_projection",
                consumer=consumer,
            ),
        ),
        audit_events=adapter.audit_events,
        diagnostic_events=registry,
        diagnostic_sink=sink,
        service_principal_id=synthetic_uuid(6775),
        clock=iter((NOW + timedelta(seconds=1), NOW + timedelta(seconds=2))).__next__,
        audit_event_id_source=lambda: delivery_audit_id,
        retry_delay=lambda attempt: timedelta(seconds=attempt),
        maximum_retry_delay=timedelta(minutes=5),
        lease_duration=timedelta(seconds=30),
        handler_timeout=timedelta(seconds=5),
        maximum_attempts=3,
    )
    with caplog.at_level(logging.INFO, logger=logger.name):
        report = await worker.dispatch_batch(
            limit=1,
            worker_identity="worker_alpha",
        )
    delivery = await adapter.delivery_repository.resolve(event_id)
    command_audit = await adapter.read_audit_event(
        adapter.source_context(workspace_id, environment_id),
        command_audit_id,
    )
    delivery_audit = await adapter.read_audit_event(
        adapter.source_context(workspace_id, environment_id),
        delivery_audit_id,
    )
    async with adapter.uow_factory(
        adapter.source_context(workspace_id, environment_id)
    ) as reader:
        canonical_revision = await reader.sources.resolve_revision(synthetic_uuid(6788))

    assert report.delivered == 1
    assert len(sink.events) >= 3
    assert len(caplog.records) >= 3
    assert {event.event_type for event in sink.events} >= {
        "http.request",
        "outbox.delivery",
    }
    assert all(event.trace_id == trace_id for event in sink.events)
    http_diagnostics = [event for event in sink.events if event.event_type == "http.request"]
    assert http_diagnostics
    # HTTP middleware runs before authentication and therefore has no trusted
    # tenant scope. Tenant-owned events below are emitted after the protected
    # request-context seam has resolved workspace and environment.
    assert all(
        event.workspace_id is None and event.environment_id is None
        for event in http_diagnostics
    )
    scoped_diagnostics = [event for event in sink.events if event.workspace_id is not None]
    assert scoped_diagnostics
    assert all(
        event.workspace_id == workspace_id
        and event.environment_id == environment_id
        for event in scoped_diagnostics
    )
    delivery_diagnostics = [
        event for event in sink.events if event.event_type == "outbox.delivery"
    ]
    assert delivery_diagnostics
    assert all(event.correlation_id == correlation_id for event in delivery_diagnostics)
    assert all(event.causation_id == event_id for event in delivery_diagnostics)
    assert consumer.contexts[0].correlation_id == correlation_id
    logged_snapshots = [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name == logger.name
    ]
    expected_snapshots = [
        {
            "event_type": event.event_type,
            "schema_version": event.schema_version,
            "severity": event.severity.value,
            "occurred_at": event.occurred_at.isoformat(),
            "trace_id": str(event.trace_id),
            "correlation_id": (
                str(event.correlation_id) if event.correlation_id is not None else None
            ),
            "causation_id": (
                str(event.causation_id) if event.causation_id is not None else None
            ),
            "workspace_id": (
                str(event.workspace_id) if event.workspace_id is not None else None
            ),
            "environment_id": (
                str(event.environment_id) if event.environment_id is not None else None
            ),
            "payload": event.payload_json(),
        }
        for event in sink.events
    ]
    assert logged_snapshots == expected_snapshots
    assert delivery is not None
    assert delivery.workspace_id == workspace_id
    assert delivery.environment_id == environment_id
    assert delivery.status.value == "delivered"
    assert command_audit is not None
    assert delivery_audit is not None
    assert canonical_revision is not None
    assert canonical_revision.workspace_id == workspace_id
    assert canonical_revision.environment_id == environment_id
    assert canonical_revision.revision_id == synthetic_uuid(6788)
    assert command_audit.trace_id == trace_id
    assert command_audit.workspace_id == workspace_id
    assert command_audit.environment_id == environment_id
    assert command_audit.correlation_id is None
    assert command_audit.causation_id is None
    assert consumer.intents[0].trace_id == trace_id
    assert consumer.intents[0].workspace_id == workspace_id
    assert consumer.intents[0].environment_id == environment_id
    assert consumer.intents[0].correlation_id == correlation_id
    assert consumer.intents[0].causation_id == command_audit_id
    assert delivery_audit.trace_id == trace_id
    assert delivery_audit.workspace_id == workspace_id
    assert delivery_audit.environment_id == environment_id
    assert delivery_audit.correlation_id == correlation_id
    assert delivery_audit.causation_id == event_id
    assert consumer.contexts[0].trace_id == trace_id
    assert consumer.contexts[0].causation_id == event_id
    delivery_lineage = (
        consumer.intents[0].trace_id,
        consumer.intents[0].correlation_id,
        consumer.intents[0].workspace_id,
        consumer.intents[0].environment_id,
    )
    assert delivery_lineage == (
        delivery_audit.trace_id,
        delivery_audit.correlation_id,
        delivery_audit.workspace_id,
        delivery_audit.environment_id,
    )
    assert consumer.intents[0].causation_id == command_audit_id
    assert delivery_audit.causation_id == event_id

    captures = (
        response.text,
        validation.text,
        failure.text,
        report.model_dump_json(),
        delivery.model_dump_json(),
        command_audit.model_dump_json(),
        delivery_audit.model_dump_json(),
        consumer.intents[0].model_dump_json(),
        consumer.intents[0].payload_snapshot.decode("utf-8"),
        consumer.contexts[0].model_dump_json(),
        *(event.model_dump_json() for event in sink.events),
        caplog.text,
    )
    assert_no_diagnostic_leaks(captures, leak_corpus=protected_values)
