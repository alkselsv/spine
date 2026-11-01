"""Executable offline API composition for the issue #646 browser qualification."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import os
from typing import AsyncIterator, TypeVar
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import SecretStr
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
import uvicorn

from spine.agents.catalog import (
    AgentBindingResolver,
    DeploymentSelectionPolicy,
    HandlerRegistry,
)
from spine.agents.runtime.artifacts import PersistedAgentInvocationResult
from spine.application.extract_requirements import (
    RequirementsExecutionRequest,
    RequirementsExtractionExecutionBoundary,
    extract_requirements_agent_version,
    extract_requirements_capability,
    register_deterministic_extract_requirements_handler,
)
from spine.api.authorization import ApiAuthorizationReadiness, ApiAuthorizationRuntime
from spine.api.main import create_app
from spine.application.diagnostics import (
    AuditEvent,
    AuditEventRegistry,
    DiagnosticEventRegistry,
)
from spine.application.content_authorization import (
    AuthorizationDecisionKind,
    AuthorizationEvidence,
    AuthorizationReasonCode,
    ContentAuthorizationDecision,
)
from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
    TrustedPersistenceContext,
    WorkspaceScope,
)
from spine.application.persistence.idempotency import OpaqueResultReference
from spine.application.persistence.unit_of_work import UnitOfWorkFactory
from spine.application.persistence.workflow_runs import register_workflow_run_outbox_event
from spine.application.parsed_content_inspection import ParsedContentInspectionRequester
from spine.application.persisted_workflow_runs import PersistedWorkflowRunAdmissionService
from spine.application.requirements_acceptance import RequirementsAcceptanceService
from spine.application.requirements_artifacts import (
    PersistedRequirementsArtifactService,
    RequirementsExtractionClarificationRevalidator,
)
from spine.application.requirements_review import RequirementsReviewService
from spine.application.requirements_evidence import (
    RequirementsEvidenceResolution,
    RequirementsEvidenceResolutionStatus,
    RequirementsEvidenceValidationService,
)
from spine.application.workflow_runs import ConfiguredWorkflowVersionResolver, PinnedWorkflowVersion
from spine.auth import (
    AccessTokenCredential,
    AuthenticatedAlias,
    AuthenticationAlias,
    AuthorizationRole,
    InMemoryAuthorizationDirectory,
    InMemoryAuthorizationEntry,
    RoutePolicyRegistry,
)
from spine.auth.in_memory import AuthorizationRecordState
from spine.domain.agents import (
    AgentBinding,
    AgentDeployment,
    AgentResult,
    AgentRunStatus,
    AgentVersion,
    DeploymentStage,
)
from spine.domain.artifacts import Artifact, ArtifactRef, ParsedSegment, PhysicalLineLocator
from spine.domain.artifacts.models import segment_digest
from spine.domain.capabilities import (
    DocumentRequirementsEvidence,
    ExtractRequirementsInput,
    RequirementsExtractionOutcome,
    RequirementsExtractionOutput,
)
from spine.domain.common import ActorKind, ActorRef, EnvironmentKind
from spine.domain.workflows import (
    Requirement,
    RequirementCategory,
    RequirementEvidenceRef,
    RequirementEvidenceStatus,
    RequirementState,
    RequirementsArtifact,
)
from spine.domain.workspaces import Environment, Workspace
from spine.infrastructure.auth import RequestAuthorizationComposer, TrustedRequestContextBoundary
from spine.infrastructure.db.migrations import upgrade_database
from spine.infrastructure.db.operator import bootstrap_database_roles
from spine.infrastructure.db.settings import (
    POSTGRESQL_SEARCH_PATH_OPTIONS,
    MigrationDatabaseSettings,
    OperatorDatabaseSettings,
    TestDatabaseSettings,
)
from spine.infrastructure.db.test_harness import install_container_marker, provision_test_database
from spine.infrastructure.diagnostics import RecordingDiagnosticSink
from spine.infrastructure.persistence.contexts import TrustedContextBoundary
from spine.infrastructure.persistence.postgresql import PostgreSQLPersistence
from tests.contracts.persistence.adapter import PersistenceAdapter
from tests.contracts.persistence.outbox_events import create_outbox_event_registry
from tests.contracts.persistence.test_canonical_acceptance_contract import prepare_case


WORKSPACE_ID = UUID("10000000-0000-4000-8000-000000000001")
ENVIRONMENT_ID = UUID("20000000-0000-4000-8000-000000000001")
IDENTITY_ID = UUID("b0000000-0000-4000-8000-000000000015")
SERVICE_PRINCIPAL_ID = UUID("b0000000-0000-4000-8000-000000000016")
WORKFLOW_VERSION_ID = UUID("b0000000-0000-4000-8000-000000000005")
SOURCE_OBJECT_ID = UUID("10000000-0000-4000-8000-000000001b5a")
SOURCE_REVISION_ID = UUID("00000000-0000-0000-0000-00000009db72")
PROVENANCE_ID = UUID("10000000-0000-4000-8000-000000001bbe")
PARSED_ARTIFACT_ID = UUID("10000000-0000-4000-8000-000000001bc0")
EVIDENCE_VALIDATION_ID = UUID("10000000-0000-4000-8000-000000001bc2")
AUTHORIZATION_DECISION_ID = UUID("10000000-0000-4000-8000-000000001bc3")
AUTHENTICATION_ALIAS = AuthenticationAlias(
    issuer="https://identity.issue646.test",
    subject="synthetic-qualification-manager",
)
_MIGRATION_ROLE = "spine_migrator"
_RUNTIME_ROLE = "spine_runtime"
_DISPATCHER_ROLE = "spine_dispatcher"
_MIGRATION_PASSWORD = "issue646-migration-password"
_RUNTIME_PASSWORD = "issue646-runtime-password"
_DISPATCHER_PASSWORD = "issue646-dispatcher-password"
_ResultT = TypeVar("_ResultT")


class _StaticAuthentication:
    async def authenticate(
        self, credentials: AccessTokenCredential, diagnostic_context: object
    ) -> AuthenticatedAlias:
        del credentials, diagnostic_context
        return AuthenticatedAlias(
            alias=AUTHENTICATION_ALIAS,
            authentication_configuration_version="issue646-auth:v1",
        )


class _ImmediateAuditGate:
    async def release_after_audit(
        self,
        *,
        context: TrustedPersistenceContext,
        event: AuditEvent,
        release: Callable[[UUID], Awaitable[_ResultT]],
    ) -> _ResultT:
        del context, event
        return await release(UUID("b0000000-0000-4000-8000-000000000017"))


_EVIDENCE_LOCATOR = PhysicalLineLocator(
    kind="physical_line", line_number=1, start_codepoint=0, end_codepoint=6
)
_EVIDENCE_TEXT_DIGEST = sha256("Запуск".encode()).hexdigest()
_EVIDENCE_SEGMENT_DIGEST = segment_digest(
    artifact_digest="b" * 64,
    locator=_EVIDENCE_LOCATOR,
    text_digest=_EVIDENCE_TEXT_DIGEST,
    heading_path=(),
)
_EVIDENCE_SEGMENT = ParsedSegment(
    segment_id=uuid5(
        NAMESPACE_URL, f"spine:parsed-segment:v1:{_EVIDENCE_SEGMENT_DIGEST}"
    ),
    segment_digest=_EVIDENCE_SEGMENT_DIGEST,
    artifact_digest="b" * 64,
    text="Запуск",
    text_digest=_EVIDENCE_TEXT_DIGEST,
    locator=_EVIDENCE_LOCATOR,
)


class _ExactEvidenceResolver:
    async def resolve(
        self, *, context: object, evidence: RequirementEvidenceRef
    ) -> RequirementsEvidenceResolution:
        del context
        if (
            evidence.source_ref.source_object_id == SOURCE_OBJECT_ID
            and evidence.source_ref.source_revision_id == SOURCE_REVISION_ID
            and evidence.source_ref.provenance_id == PROVENANCE_ID
            and evidence.parsed_artifact_ref is not None
            and evidence.parsed_artifact_ref.id == PARSED_ARTIFACT_ID
            and evidence.segment_id == _EVIDENCE_SEGMENT.segment_id
            and evidence.segment_digest == _EVIDENCE_SEGMENT.segment_digest
            and evidence.locator == _EVIDENCE_SEGMENT.locator
        ):
            return RequirementsEvidenceResolution(
                RequirementsEvidenceResolutionStatus.VERIFIED, _EVIDENCE_SEGMENT
            )
        return RequirementsEvidenceResolution(
            RequirementsEvidenceResolutionStatus.FABRICATED
        )


class _ExactEvidenceAuthorizer:
    async def decide_read_content(
        self,
        *,
        context: TrustedPersistenceContext,
        requester: ParsedContentInspectionRequester,
        source_object_id: UUID,
        source_revision_id: UUID,
    ) -> ContentAuthorizationDecision:
        del requester
        return ContentAuthorizationDecision(
            allowed=True,
            decision_kind=AuthorizationDecisionKind.HUMAN_DISCLOSURE,
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
            source_object_ids=(source_object_id,),
            policy_evidence=(
                AuthorizationEvidence(
                    source_object_id=source_object_id,
                    source_revision_id=source_revision_id,
                    policy_version_id=UUID("10000000-0000-4000-8000-000000001bc4"),
                    policy_generation=1,
                ),
            ),
            membership_generation=1,
            authority_generation=1,
            authority_snapshot_reference=UUID(
                "10000000-0000-4000-8000-000000001bc5"
            ),
            purpose=context.purpose.value,
            operation=context.operation.value,
            reason_code=AuthorizationReasonCode.ALLOWED,
            decision_reference=AUTHORIZATION_DECISION_ID,
            trace_id=context.trace_id,
            evaluated_at=datetime(2026, 11, 1, 9, tzinfo=timezone.utc),
        )


class _ExactEvidenceDecisionAudit:
    async def record_decision(self, **kwargs: object) -> UUID:
        del kwargs
        return EVIDENCE_VALIDATION_ID


@dataclass(frozen=True)
class _RequirementsCatalog:
    version: AgentVersion
    binding_id: UUID
    deployment_id: UUID

    def list_bindings(self, workspace_id, environment, capability_key):
        return (
            AgentBinding(
                id=self.binding_id,
                workspace_id=workspace_id,
                environment=environment,
                capability=capability_key,
                agent_version_id=self.version.id,
            ),
        )

    def get_agent_version(self, agent_version_id):
        return self.version if agent_version_id == self.version.id else None

    def list_deployments(self, workspace_id, environment, agent_version_id):
        return (
            AgentDeployment(
                id=self.deployment_id,
                workspace_id=workspace_id,
                environment=environment,
                agent_version_id=agent_version_id,
                stage=DeploymentStage.DEVELOPMENT,
            ),
        )


class _RequirementsArtifactReader:
    def __init__(self, *artifacts: Artifact) -> None:
        self._artifacts = {artifact.id: artifact for artifact in artifacts}

    async def load(
        self,
        artifact_id: UUID,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        work_item_id: UUID,
    ) -> Artifact | None:
        artifact = self._artifacts.get(artifact_id)
        if artifact is None or (
            artifact.workspace_id,
            artifact.environment_id,
            artifact.work_item_id,
        ) != (workspace_id, environment_id, work_item_id):
            return None
        return artifact


class _RequirementsAgentRuntimeFake:
    def __init__(self, *, output_artifact_id: UUID, agent_run_id: UUID) -> None:
        self._output_artifact_id = output_artifact_id
        self._agent_run_id = agent_run_id

    async def invoke_with_reference(self, version, invocation):
        del version, invocation
        return PersistedAgentInvocationResult(
            result=AgentResult(
                status=AgentRunStatus.SUCCEEDED,
                output_artifact_ids=(self._output_artifact_id,),
            ),
            reference=OpaqueResultReference(
                result_type="agent_invocation",
                result_id=self._agent_run_id,
                schema_version=1,
            ),
        )


class _RequirementsRuntimeProvider:
    def __init__(self, runtime: _RequirementsAgentRuntimeFake) -> None:
        self._runtime = runtime

    def runtime_for(self, **kwargs):
        del kwargs
        return self._runtime


class _FakeBackedRequirementsArtifactService:
    """Route first-candidate production through the existing Agent Runtime boundary."""

    def __init__(
        self,
        *,
        delegate: PersistedRequirementsArtifactService,
        uow_factory: UnitOfWorkFactory,
    ) -> None:
        self._delegate = delegate
        self._uow_factory = uow_factory

    def __getattr__(self, name: str):
        return getattr(self._delegate, name)

    async def produce_first_candidate(
        self, *, context: TrustedPersistenceContext, run_id: UUID
    ) -> RequirementsArtifact | None:
        existing = await self._delegate.get_for_run(context=context, run_id=run_id)
        if existing is not None:
            return existing
        scope = context.scope
        if not isinstance(scope, EnvironmentScope):
            return None
        async with self._uow_factory(context) as uow:
            run = await uow.workflow_runs.get_run(
                workspace_id=scope.workspace_id,
                environment_id=scope.environment_id,
                run_id=run_id,
            )
        if run is None:
            return None

        capability = extract_requirements_capability()
        version = extract_requirements_agent_version()
        input_artifact_id = uuid5(run.run_id, "issue646:requirements-input")
        output_artifact_id = uuid5(run.run_id, "issue646:requirements-output")
        step_run_id = uuid5(run.run_id, "issue646:requirements-step")
        agent_run_id = uuid5(run.run_id, "issue646:requirements-agent-run")
        value = ExtractRequirementsInput(
            workspace_id=run.workspace_id,
            environment_id=run.environment_id,
            environment=EnvironmentKind.PRODUCTION,
            workflow_run_id=run.run_id,
            work_item_id=run.work_item_id,
            client_request=run.client_request,
            authorized_source_refs=run.client_request.accepted_sources,
            evidence=(
                DocumentRequirementsEvidence(
                    reference=RequirementEvidenceRef(
                        source_ref=run.client_request.accepted_sources[0],
                        parsed_artifact_ref=ArtifactRef(
                            id=PARSED_ARTIFACT_ID,
                            schema_name="parsed-document",
                            schema_version="parsed-document:v1",
                        ),
                        segment_id=_EVIDENCE_SEGMENT.segment_id,
                        segment_digest=_EVIDENCE_SEGMENT.segment_digest,
                        locator=_EVIDENCE_SEGMENT.locator,
                    ),
                    text=_EVIDENCE_SEGMENT.text,
                ),
            ),
        )
        extraction = RequirementsExtractionOutput(
            workspace_id=run.workspace_id,
            environment_id=run.environment_id,
            workflow_run_id=run.run_id,
            work_item_id=run.work_item_id,
            outcome=RequirementsExtractionOutcome.PARTIAL,
            requirements=(
                Requirement(
                    requirement_id=uuid5(run.run_id, "requirements:1"),
                    category=RequirementCategory.UNRESOLVED_INFORMATION,
                    statement=run.client_request.message,
                    state=RequirementState.PARTIAL,
                    uncertainty=(
                        "The request does not contain enough detail to extract a complete requirement."
                    ),
                    missing_markers=("scope", "success_criteria"),
                    ambiguous_markers=("request_intent",),
                    source_refs=run.client_request.accepted_sources,
                    evidence_refs=(
                        RequirementEvidenceRef(
                            source_ref=run.client_request.accepted_sources[0],
                            parsed_artifact_ref=ArtifactRef(
                                id=PARSED_ARTIFACT_ID,
                                schema_name="parsed-document",
                                schema_version="parsed-document:v1",
                            ),
                            segment_id=_EVIDENCE_SEGMENT.segment_id,
                            segment_digest=_EVIDENCE_SEGMENT.segment_digest,
                            locator=_EVIDENCE_SEGMENT.locator,
                        ),
                    ),
                    evidence_status=RequirementEvidenceStatus.UNVERIFIED,
                ),
            ),
        )
        input_artifact = Artifact(
            id=input_artifact_id,
            workspace_id=run.workspace_id,
            environment_id=run.environment_id,
            work_item_id=run.work_item_id,
            produced_by=run.started_by,
            artifact_schema=capability.input_schema,
            content=value.model_dump(mode="json"),
        )
        output_artifact = Artifact(
            id=output_artifact_id,
            workspace_id=run.workspace_id,
            environment_id=run.environment_id,
            work_item_id=run.work_item_id,
            produced_by=ActorRef(kind=ActorKind.AGENT, id=version.agent_id),
            produced_by_agent_version_id=version.id,
            artifact_schema=capability.output_schema,
            content=extraction.model_dump(mode="json"),
            upstream_artifact_ids=(input_artifact_id,),
        )
        handlers = HandlerRegistry()
        register_deterministic_extract_requirements_handler(handlers)
        handlers.finalize()
        resolver = AgentBindingResolver(
            catalog=_RequirementsCatalog(
                version=version,
                binding_id=uuid5(run.run_id, "issue646:requirements-binding"),
                deployment_id=uuid5(run.run_id, "issue646:requirements-deployment"),
            ),
            registry=handlers,
            approved_deployment_policy=DeploymentSelectionPolicy(
                allowed_stages=frozenset({DeploymentStage.DEVELOPMENT})
            ),
        )
        boundary = RequirementsExtractionExecutionBoundary(
            capability=capability,
            binding_resolver=resolver,
            runtime_provider=_RequirementsRuntimeProvider(
                _RequirementsAgentRuntimeFake(
                    output_artifact_id=output_artifact_id,
                    agent_run_id=agent_run_id,
                )
            ),
            artifacts=_RequirementsArtifactReader(input_artifact, output_artifact),
            requirements_artifacts=self._delegate,
        )
        result = await boundary.execute(
            request=RequirementsExecutionRequest(
                workspace_id=run.workspace_id,
                environment_id=run.environment_id,
                environment=EnvironmentKind.PRODUCTION,
                workflow_run_id=run.run_id,
                work_item_id=run.work_item_id,
                step_run_id=step_run_id,
                agent_run_id=agent_run_id,
                input_artifact_id=input_artifact_id,
                acting_on_behalf_of=run.started_by,
                idempotency_key=f"issue646:requirements:{run.run_id}",
            ),
            persistence_context=context,
        )
        return result.artifact


async def _authorization_ready() -> ApiAuthorizationReadiness:
    return ApiAuthorizationReadiness(ready=True, code="authorization.ready")


@asynccontextmanager
async def _transactions_available() -> AsyncIterator[None]:
    yield


def _role_url(url: SecretStr, role: str, password: str) -> SecretStr:
    value = make_url(url.get_secret_value()).set(username=role, password=password)
    return SecretStr(value.render_as_string(hide_password=False))


async def _serve() -> None:
    provision = provision_test_database(
        TestDatabaseSettings(), run_id=f"issue646-{uuid4().hex[:12]}"
    )
    if not provision.is_explicit:
        await install_container_marker(provision)
    try:
        operator = OperatorDatabaseSettings(
            url=provision.url,
            database_name=provision.target.database_name,
            migration_role=_MIGRATION_ROLE,
            migration_password=_MIGRATION_PASSWORD,
            runtime_role=_RUNTIME_ROLE,
            runtime_password=_RUNTIME_PASSWORD,
            dispatcher_role=_DISPATCHER_ROLE,
            dispatcher_password=_DISPATCHER_PASSWORD,
        )
        await bootstrap_database_roles(operator)
        migration_url = _role_url(provision.url, _MIGRATION_ROLE, _MIGRATION_PASSWORD)
        await asyncio.to_thread(
            upgrade_database,
            MigrationDatabaseSettings(
                url=migration_url,
                migration_role=_MIGRATION_ROLE,
                runtime_role=_RUNTIME_ROLE,
                dispatcher_role=_DISPATCHER_ROLE,
            ),
        )
        engine = create_async_engine(
            _role_url(provision.url, _RUNTIME_ROLE, _RUNTIME_PASSWORD).get_secret_value(),
            pool_pre_ping=True,
            pool_reset_on_return="rollback",
            hide_parameters=True,
            connect_args={"options": POSTGRESQL_SEARCH_PATH_OPTIONS},
        )
        sessions = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        persistence_boundary = TrustedContextBoundary.for_testing(
            issuer_id=UUID("b0000000-0000-4000-8000-000000000018"),
            secret=b"issue646-persistence-boundary-secret",
        )
        request_boundary = TrustedRequestContextBoundary.for_testing(
            persistence_boundary=persistence_boundary,
            issuer_id=UUID("b0000000-0000-4000-8000-000000000019"),
            secret=b"issue646-request-boundary-secret-0001",
        )
        outbox_events = create_outbox_event_registry()
        register_workflow_run_outbox_event(outbox_events)
        persistence = PostgreSQLPersistence(
            session_factory=sessions,
            context_verifier=request_boundary,
            outbox_events=outbox_events,
        )
        setup_context = persistence_boundary.worker(
            scope=WorkspaceScope(workspace_id=WORKSPACE_ID),
            service_principal_id=SERVICE_PRINCIPAL_ID,
            purpose=PersistencePurpose("qualification"),
            operation=PersistenceOperation("workspace_repository"),
            trace_id=UUID("b0000000-0000-4000-8000-000000000020"),
        )
        async with persistence.uow_factory(setup_context) as uow:
            await uow.workspaces.add(
                Workspace(id=WORKSPACE_ID, slug="issue-646", display_name="Issue 646")
            )
            await uow.environments.add(
                Environment(
                    id=ENVIRONMENT_ID,
                    workspace_id=WORKSPACE_ID,
                    kind=EnvironmentKind.PRODUCTION,
                    display_name="Offline qualification",
                )
            )
            await uow.commit()

        def worker_context(
            workspace_id: UUID,
            environment_id: UUID | None,
            operation: str,
        ):
            scope = (
                WorkspaceScope(workspace_id=workspace_id)
                if environment_id is None
                else EnvironmentScope(
                    workspace_id=workspace_id,
                    environment_id=environment_id,
                )
            )
            return persistence_boundary.worker(
                scope=scope,
                service_principal_id=SERVICE_PRINCIPAL_ID,
                purpose=PersistencePurpose("contract_test"),
                operation=PersistenceOperation(operation),
                trace_id=UUID("b0000000-0000-4000-8000-000000000020"),
            )

        fixture_adapter = PersistenceAdapter(
            uow_factory=persistence.uow_factory,
            workspace_context=lambda workspace_id: worker_context(
                workspace_id, None, "workspace_repository"
            ),
            worker_workspace_context=lambda workspace_id: worker_context(
                workspace_id, None, "workspace_repository"
            ),
            environment_context=lambda workspace_id, environment_id: worker_context(
                workspace_id, environment_id, "environment_repository"
            ),
            hold_transactions=_transactions_available,
            outbox_events=outbox_events,
            source_context=lambda workspace_id, environment_id: worker_context(
                workspace_id, environment_id, "source_observation"
            ),
            ingestion_context=lambda workspace_id, environment_id, operation: worker_context(
                workspace_id, environment_id, operation
            ),
        )
        source_case = await prepare_case(
            fixture_adapter,
            646000,
            scope=(WORKSPACE_ID, ENVIRONMENT_ID),
            create_scope=False,
        )
        assert source_case.source.source_object_id == SOURCE_OBJECT_ID
        assert source_case.revision.revision_id == SOURCE_REVISION_ID
        assert source_case.provenance.provenance_id == PROVENANCE_ID
        async with persistence.uow_factory(source_case.context) as uow:
            await uow.canonical.accept(source_case.command)
            await uow.commit()

        diagnostic_registry = DiagnosticEventRegistry.with_default_families()
        diagnostic_sink = RecordingDiagnosticSink(diagnostic_registry)
        route_policies = RoutePolicyRegistry()
        authorization_directory = InMemoryAuthorizationDirectory(
            entries=(
                InMemoryAuthorizationEntry(
                    alias=AUTHENTICATION_ALIAS,
                    canonical_human_identity_id=IDENTITY_ID,
                    identity_state=AuthorizationRecordState.ACTIVE,
                    alias_state=AuthorizationRecordState.ACTIVE,
                    workspace_id=WORKSPACE_ID,
                    workspace_membership_state=AuthorizationRecordState.ACTIVE,
                    environment_id=ENVIRONMENT_ID,
                    environment_workspace_id=WORKSPACE_ID,
                    environment_membership_state=AuthorizationRecordState.ACTIVE,
                    roles=frozenset({AuthorizationRole.ADMINISTRATOR}),
                    role_binding_state=AuthorizationRecordState.ACTIVE,
                    snapshot_generation=1,
                    current_generation=1,
                ),
            )
        )
        authorization = ApiAuthorizationRuntime(
            composer=RequestAuthorizationComposer(
                authentication=_StaticAuthentication(),
                authorization_directory=authorization_directory,
                route_policies=route_policies,
                request_boundary=request_boundary,
                audit_registry=AuditEventRegistry.with_default_families(),
                audit_gate=_ImmediateAuditGate(),
                diagnostic_registry=diagnostic_registry,
                diagnostic_sink=diagnostic_sink,
                clock=lambda: datetime(2026, 11, 1, 9, tzinfo=timezone.utc),
                request_id_source=uuid4,
            ),
            route_policies=route_policies,
            readiness_probe=_authorization_ready,
        )
        requirements_evidence = RequirementsEvidenceValidationService(
            resolver=_ExactEvidenceResolver(),
            authorizer=_ExactEvidenceAuthorizer(),
            decision_audit=_ExactEvidenceDecisionAudit(),
            validation_reference_factory=lambda: EVIDENCE_VALIDATION_ID,
            clock=lambda: datetime(2026, 11, 1, 9, tzinfo=timezone.utc),
        )
        artifacts = PersistedRequirementsArtifactService(
            persistence.uow_factory,
            clarification_revalidator=RequirementsExtractionClarificationRevalidator(),
        )
        fake_backed_artifacts = _FakeBackedRequirementsArtifactService(
            delegate=artifacts,
            uow_factory=persistence.uow_factory,
        )
        application = create_app(
            authorization_runtime=authorization,
            diagnostic_registry=diagnostic_registry,
            diagnostic_sink=diagnostic_sink,
            workflow_run_admission_service=PersistedWorkflowRunAdmissionService(
                persistence.uow_factory
            ),
            requirements_artifact_service=fake_backed_artifacts,
            requirements_review_service=RequirementsReviewService(persistence.uow_factory),
            requirements_acceptance_service=RequirementsAcceptanceService(
                persistence.uow_factory,
                evidence_validator=requirements_evidence,
            ),
            requirements_evidence_service=requirements_evidence,
            workflow_version_resolver=ConfiguredWorkflowVersionResolver(
                (
                    PinnedWorkflowVersion(
                        workflow_key="commercial.requirements",
                        workflow_version_id=WORKFLOW_VERSION_ID,
                        version="1.0.0",
                    ),
                )
            ),
            workflow_run_cursor_secret=b"issue646-workflow-cursor-secret",
        )
        server = uvicorn.Server(
            uvicorn.Config(
                application,
                host="127.0.0.1",
                port=int(os.getenv("SPINE_ISSUE646_API_PORT", "4176")),
                log_level="warning",
            )
        )
        try:
            await server.serve()
        finally:
            await engine.dispose()
    finally:
        if provision.container is not None:
            provision.container.stop()


if __name__ == "__main__":
    asyncio.run(_serve())
