"""HTTP composition roots for deployable authentication modes."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import inspect
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import httpx
from fastapi import FastAPI
from pydantic import ValidationError

from spine.api.authorization import ApiAuthorizationReadiness, ApiAuthorizationRuntime
from spine.application.authorized_ask import (
    AnswerQuestionAttemptApplication,
    AnswerQuestionCapability,
    AuthorizedAskService,
    PersistedAuthorizedAskService,
)
from spine.application.persistence.ask_admission import (
    AskAdmissionApplication,
    AskAdmissionCommand,
)
from spine.application.persistence.context import TrustedPersistenceContext
from spine.application.persistence.delivery import OutboxDeliveryRouteRegistry
from spine.application.diagnostics import (
    AuditEventRegistry,
    DiagnosticContext,
    DiagnosticEventRegistry,
    DiagnosticSink,
)
from spine.application.qa_observation.admission import ObserverAskAdmission
from spine.application.qa_observation.channel import CaptureChannel
from spine.application.worker_execution_observation import (
    WorkerExecutionElapsedCapturePort,
)
from spine.application.qa_sessions import QASessionService
from spine.application.workflows.ask_admission_dispatch import (
    AskAdmissionCommandLoader,
    AskAdmissionWorkflowClient,
    AskAdmissionWorkflowExecutor,
    AskAdmissionWorkflowInput,
)
from spine.auth import AuthenticationPort, AuthorizationDirectory, RoutePolicyRegistry
from spine.infrastructure.auth import (
    LocalAuthenticationAdapter,
    LocalAuthenticationConfigurationError,
    LocalAuthenticationSettings,
    OIDCAuthenticationRuntime,
    OIDCAuthenticationSettings,
    OIDCReadinessStatus,
    RequestAuthorizationComposer,
    TrustedRequestContextBoundary,
)
from spine.infrastructure.auth.composition import RequiredAuditGate
from spine.infrastructure.db.engine import SessionFactory
from spine.infrastructure.persistence.postgresql_authorization import (
    PostgreSQLAuthorizationDirectory,
    check_postgresql_authorization_store_readiness,
)
from spine.domain.common import EnvironmentKind
from spine.workflows.temporal.bootstrap import (
    build_ask_admission_activity_bindings,
    build_ask_admission_delivery_binding,
    build_ask_cancellation_delivery_binding,
    build_ask_admission_workflow_definition,
    register_ask_admission_delivery_route,
)
from spine.workers.composition import OutboxConsumerBinding

if TYPE_CHECKING:
    from spine.application.production_qa_composition import ProductionQAACompositionEvidence


@dataclass(frozen=True, slots=True)
class DurableAskComposition:
    """One injectable API, outbox and Temporal composition for durable Q&A."""

    admission: AskAdmissionApplication
    api_ask_service: AuthorizedAskService
    workflow_executor: AskAdmissionWorkflowExecutor
    delivery_bindings: tuple[OutboxConsumerBinding, ...]
    workflow_definition: type
    activity_bindings: tuple[object, object]
    observer_admission: ObserverAskAdmission | None = None
    sessions: QASessionService | None = None
    capture_channel: CaptureChannel | None = None

    @property
    def real_qualification_evidence(self) -> ProductionQAACompositionEvidence | None:
        """Attest the production adapter graph for the local qualification gate."""
        from spine.application.ask_admission import PersistedAskAdmissionService
        from spine.application.authorized_ask import PersistedAuthorizedAskService
        from spine.application.production_qa_capability import ProductionAnswerQuestionCapability
        from spine.application.production_qa_capability import (
            PersistedAnswerQuestionCandidateRunner,
            ExistingAnswerQuestionCandidateValidator,
        )
        from spine.application.answer_question_context import AnswerQuestionContextService
        from spine.application.answer_disclosure import AnswerDisclosureGate
        from spine.application.authorized_context_broker import AuthorizedContextBrokerApplicationService
        from spine.application.qa_observation.admission import ObserverAskAdmission
        from spine.application.qa_sessions import PersistedQASessionService
        from spine.application.production_qa_composition import (
            ProductionQAACompositionEvidence,
            _issue_production_qa_composition_evidence,
        )
        from spine.application.answer_question_execution import AnswerQuestionExecutionBoundary
        from spine.application.workflows.ask_admission_dispatch import AskAdmissionWorkflowExecutor
        from spine.infrastructure.auth.trusted_context import TrustedRequestContextBoundary
        from spine.infrastructure.persistence.postgresql import PostgreSQLTenantUnitOfWorkFactory
        from spine.infrastructure.qa_observation_capture.http import HttpCaptureChannel
        from spine.application.workflows.ask_admission_dispatch import (
            AskAdmissionDispatchConsumer,
            AskCancellationDispatchConsumer,
        )
        from spine.workflows.temporal.client import TemporalSdkWorkflowClient

        service = self.api_ask_service
        sessions = self.sessions
        channel = self.capture_channel
        observer_admission = self.observer_admission
        executor = self.workflow_executor
        if type(self.admission) is not PersistedAskAdmissionService:
            return None
        if type(self.admission.uow_factory) is not PostgreSQLTenantUnitOfWorkFactory:
            return None
        if type(self.admission.uow_factory._context_verifier) is not TrustedRequestContextBoundary:
            return None
        if type(service) is not PersistedAuthorizedAskService:
            return None
        if type(service.capability) is not ProductionAnswerQuestionCapability:
            return None
        if type(service.capability.execution) is not AnswerQuestionExecutionBoundary:
            return None
        capability = service.capability
        if (
            type(capability.context) is not AnswerQuestionContextService
            or type(capability.context._broker) is not AuthorizedContextBrokerApplicationService
            or type(capability.disclosure_gate) is not AnswerDisclosureGate
            or type(capability.candidate_runner) is not PersistedAnswerQuestionCandidateRunner
            or type(capability.validator) is not ExistingAnswerQuestionCandidateValidator
        ):
            return None
        if type(sessions) is not PersistedQASessionService:
            return None
        if type(channel) is not HttpCaptureChannel:
            return None
        if type(observer_admission) is not ObserverAskAdmission:
            return None
        if (
            observer_admission._admission is not self.admission
            or observer_admission._channel is not channel
        ):
            return None
        if type(executor) is not AskAdmissionWorkflowExecutor:
            return None
        if executor._service is not service:
            return None
        if self.workflow_definition is not build_ask_admission_workflow_definition():
            return None
        expected_activity_bindings = build_ask_admission_activity_bindings(executor)
        if len(self.activity_bindings) != 2 or any(
            not callable(binding)
            or binding.__code__ is not expected.__code__
            or inspect.getclosurevars(binding).nonlocals.get("service") is not executor
            for binding, expected in zip(
                self.activity_bindings,
                expected_activity_bindings,
                strict=True,
            )
        ):
            return None
        if len(self.delivery_bindings) != 2:
            return None
        admission_binding, cancellation_binding = self.delivery_bindings
        if (
            type(admission_binding.consumer) is not AskAdmissionDispatchConsumer
            or type(cancellation_binding.consumer) is not AskCancellationDispatchConsumer
        ):
            return None
        temporal_client = admission_binding.consumer._launcher._client
        if (
            type(temporal_client) is not TemporalSdkWorkflowClient
            or cancellation_binding.consumer._client is not temporal_client
        ):
            return None
        from temporalio.client import Client

        if type(temporal_client._client) is not Client:
            return None
        return _issue_production_qa_composition_evidence(
            self,
            self.admission.uow_factory._context_verifier,
        )

    def create_api_app(self, **dependencies: object) -> FastAPI:
        """Create the public API with the admission-only durable ask route."""

        from spine.api.main import create_app

        return create_app(
            authorized_ask_service=self.api_ask_service,
            ask_admission_service=self.admission,
            **dependencies,
        )


def compose_durable_ask(
    *,
    capability: AnswerQuestionCapability,
    sessions: QASessionService,
    admission: AskAdmissionApplication,
    attempts: AnswerQuestionAttemptApplication,
    command_loader: AskAdmissionCommandLoader,
    persistence_context_factory: Callable[
        [AskAdmissionWorkflowInput], TrustedPersistenceContext
    ],
    temporal_client: AskAdmissionWorkflowClient,
    delivery_routes: OutboxDeliveryRouteRegistry,
    monotonic_ns: Callable[[], int],
    worker_execution_elapsed_port: WorkerExecutionElapsedCapturePort,
    environment: EnvironmentKind = EnvironmentKind.PRODUCTION,
    authorization_generation: int = 1,
    run_starter: Callable[[AskAdmissionWorkflowInput], Awaitable[object]] | None = None,
    capture_channel: CaptureChannel | None = None,
    worker_capture_channel: CaptureChannel | None = None,
    capture_id_source: Callable[[AskAdmissionCommand], UUID] | None = None,
) -> DurableAskComposition:
    """Wire API admission to the canonical outbox and Temporal execution owner.

    Infrastructure dependencies (including the canonical PostgreSQL-backed
    admission application and authenticated Temporal client) are explicit
    inputs, so tests and deployments use the same composition path.
    """

    api_service = PersistedAuthorizedAskService(
        capability=capability,
        sessions=sessions,
        admission=None,
        attempts=attempts,
        environment=environment,
    )
    executor = AskAdmissionWorkflowExecutor(
        loader=command_loader,
        service=api_service,
        persistence_context_factory=persistence_context_factory,
        run_starter=run_starter,
        authorization_generation=authorization_generation,
        monotonic_ns=monotonic_ns,
        execution_elapsed_port=worker_execution_elapsed_port,
        capture_publisher=(
            worker_capture_channel
            if worker_capture_channel is not None
            else capture_channel
        ),
    )
    register_ask_admission_delivery_route(delivery_routes)
    delivery_routes.seal()
    return DurableAskComposition(
        admission=admission,
        api_ask_service=api_service,
        workflow_executor=executor,
        delivery_bindings=(
            build_ask_admission_delivery_binding(temporal_client),
            build_ask_cancellation_delivery_binding(temporal_client),
        ),
        workflow_definition=build_ask_admission_workflow_definition(),
        activity_bindings=build_ask_admission_activity_bindings(executor),
        observer_admission=(
            ObserverAskAdmission(
                admission=admission,
                channel=capture_channel,
                capture_id_source=capture_id_source,
            )
            if capture_channel is not None
            else None
        ),
        sessions=sessions,
        capture_channel=capture_channel,
    )


def _compose_request_authorization(
    *,
    authentication: AuthenticationPort[DiagnosticContext],
    authorization_directory: AuthorizationDirectory,
    route_policies: RoutePolicyRegistry,
    request_boundary: TrustedRequestContextBoundary,
    audit_registry: AuditEventRegistry,
    audit_gate: RequiredAuditGate,
    diagnostic_registry: DiagnosticEventRegistry,
    diagnostic_sink: DiagnosticSink,
    clock: Callable[[], datetime],
    request_id_source: Callable[[], UUID],
) -> RequestAuthorizationComposer:
    return RequestAuthorizationComposer(
        authentication=authentication,
        authorization_directory=authorization_directory,
        route_policies=route_policies,
        request_boundary=request_boundary,
        audit_registry=audit_registry,
        audit_gate=audit_gate,
        diagnostic_registry=diagnostic_registry,
        diagnostic_sink=diagnostic_sink,
        clock=clock,
        request_id_source=request_id_source,
    )


def compose_local_api_authorization(
    *,
    session_factory: SessionFactory,
    route_policies: RoutePolicyRegistry,
    request_boundary: TrustedRequestContextBoundary,
    audit_registry: AuditEventRegistry,
    audit_gate: RequiredAuditGate,
    diagnostic_registry: DiagnosticEventRegistry,
    diagnostic_sink: DiagnosticSink,
    settings: LocalAuthenticationSettings | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    request_id_source: Callable[[], UUID] = uuid4,
) -> ApiAuthorizationRuntime:
    """Build local HTTP auth with the production PostgreSQL authority lookup."""

    try:
        resolved_settings = settings or LocalAuthenticationSettings()
        authentication = LocalAuthenticationAdapter(resolved_settings)
    except (LocalAuthenticationConfigurationError, ValidationError):
        raise LocalAuthenticationConfigurationError() from None

    composer = _compose_request_authorization(
        authentication=authentication,
        authorization_directory=PostgreSQLAuthorizationDirectory(session_factory),
        route_policies=route_policies,
        request_boundary=request_boundary,
        audit_registry=audit_registry,
        audit_gate=audit_gate,
        diagnostic_registry=diagnostic_registry,
        diagnostic_sink=diagnostic_sink,
        clock=clock,
        request_id_source=request_id_source,
    )

    async def local_readiness() -> ApiAuthorizationReadiness:
        store_ready = await check_postgresql_authorization_store_readiness(
            session_factory
        )
        return ApiAuthorizationReadiness(
            ready=store_ready,
            code=(
                "local.ready"
                if store_ready
                else "authorization.store_unavailable"
            ),
            authentication_configuration_version=(
                resolved_settings.configuration_version
            ),
            authentication_mode="local",
        )

    return ApiAuthorizationRuntime(
        composer=composer,
        route_policies=route_policies,
        readiness_probe=local_readiness,
    )


def compose_oidc_api_authorization(
    *,
    settings: OIDCAuthenticationSettings,
    session_factory: SessionFactory,
    route_policies: RoutePolicyRegistry,
    request_boundary: TrustedRequestContextBoundary,
    audit_registry: AuditEventRegistry,
    audit_gate: RequiredAuditGate,
    diagnostic_registry: DiagnosticEventRegistry,
    diagnostic_sink: DiagnosticSink,
    transport: httpx.AsyncBaseTransport | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    request_id_source: Callable[[], UUID] = uuid4,
) -> ApiAuthorizationRuntime:
    """Build production OIDC HTTP auth with canonical PostgreSQL authority."""

    oidc_runtime = OIDCAuthenticationRuntime(
        settings,
        transport=transport,
        clock=clock,
    )
    composer = _compose_request_authorization(
        authentication=oidc_runtime.authentication,
        authorization_directory=PostgreSQLAuthorizationDirectory(session_factory),
        route_policies=route_policies,
        request_boundary=request_boundary,
        audit_registry=audit_registry,
        audit_gate=audit_gate,
        diagnostic_registry=diagnostic_registry,
        diagnostic_sink=diagnostic_sink,
        clock=clock,
        request_id_source=request_id_source,
    )

    async def oidc_readiness() -> ApiAuthorizationReadiness:
        readiness = await oidc_runtime.check_readiness()
        authentication_ready = readiness.status in {
            OIDCReadinessStatus.READY,
            OIDCReadinessStatus.DEGRADED,
        }
        if not authentication_ready:
            return ApiAuthorizationReadiness(
                ready=False,
                code=readiness.code.value,
                authentication_configuration_version=readiness.configuration_version,
                authentication_mode=readiness.mode,
            )
        store_ready = await check_postgresql_authorization_store_readiness(
            session_factory
        )
        return ApiAuthorizationReadiness(
            ready=store_ready,
            code=(
                readiness.code.value
                if store_ready
                else "authorization.store_unavailable"
            ),
            authentication_configuration_version=readiness.configuration_version,
            authentication_mode=readiness.mode,
        )

    return ApiAuthorizationRuntime(
        composer=composer,
        route_policies=route_policies,
        readiness_probe=oidc_readiness,
        startup_hook=oidc_runtime.startup,
        close_hook=oidc_runtime.aclose,
    )


__all__ = [
    "DurableAskComposition",
    "compose_durable_ask",
    "compose_local_api_authorization",
    "compose_oidc_api_authorization",
]
