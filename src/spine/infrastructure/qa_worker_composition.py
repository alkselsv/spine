"""Deployable governed-Q&A capability graph used by the Temporal worker.

The graph is deliberately fail closed until authorized retrieval has produced
an eligible ContextBundle.  Its concrete PostgreSQL repositories and exact
production application types are shared with the real worker rather than
replaced by workflow-specific shortcuts.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import text

from spine.agents.catalog import (
    AgentBindingResolver,
    AgentImplementationRegistration,
    DeploymentSelectionPolicy,
    HandlerRegistry,
)
from spine.application.answer_disclosure import (
    AnswerDisclosureGate,
    InMemoryAcceptedAnswerOutputPort,
)
from spine.application.answer_question_context import AnswerQuestionContextService
from spine.application.answer_question_execution import AnswerQuestionExecutionBoundary
from spine.application.authorized_context_broker import (
    AuthorizedContextRequest,
    AuthorizedContextBrokerApplicationService,
    ContextBrokerReadModel,
)
from spine.application.authorized_retrieval_scope import (
    AuthorizedRetrievalScope,
    AuthorizedRetrievalScopeResolver,
)
from spine.application.content_authorization import (
    AuthorizationReasonCode,
    ContentAuthorizationDecision,
    ContentAuthorizationDecisionService,
    CurrentContentAuthorizationSnapshot,
)
from spine.application.context_profiles import ServerApprovedContextProfileResolver
from spine.application.citation_validation import (
    CitationValidationFacts,
    CitationValidationReasonCode,
    RejectedCitation,
    ValidCitation,
)
from spine.application.disclosure_revalidation import DisclosureRevalidationService
from spine.application.evidence_lineage_verification import (
    EvidenceVerificationDiagnostic,
    EvidenceVerificationReasonCode,
    RejectedEvidenceItem,
    VerifiedEvidenceItem,
)
from spine.application.projection_locator import canonical_projection_locator_digest
from spine.application.production_qa_capability import (
    AnswerQuestionEvaluationLineage,
    AnswerQuestionValidationPlan,
    AnswerQuestionContextReferences,
    ExistingAnswerQuestionCandidateValidator,
    PersistedAnswerQuestionCandidateRunner,
    ProductionAnswerQuestionCapability,
)
from spine.application.semantic_support import SemanticSupportValidationService
from spine.application.content_authorization import (
    AuthorizationContextMode,
    TrustedAuthorizationContext,
)
from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
)
from spine.application.persistence.errors import ContextBundleNotFoundError
from spine.domain.agents import (
    AgentBinding,
    AgentDeployment,
    AgentRuntimeKind,
    AgentVersion,
    DeploymentStage,
)
from spine.domain.capabilities import CapabilityDefinition
from spine.domain.capabilities.answer_evaluation import (
    AttemptProjectionBoundary,
    CanonicalViewMember,
    ClaimAuthorizationStatus,
    ClaimEvidenceValidation,
    ClaimGroundingStatus,
    CoverageMemberStatus,
    GroundedClaimAssessment,
    GroundedConflict,
    MissingCoverageImpact,
    ObservedCoverage,
    ObservedCoverageMember,
    PartialClaimUtility,
    PartialFallbackAuthorization,
    PartialFallbackAuthority,
    TargetCompletenessScope,
    TargetScopeMember,
)
from spine.domain.capabilities.evaluate_answer import (
    AnswerEvaluationRequest,
    evaluate_answer_candidate,
)
from spine.domain.capabilities.answer_question import ClaimEvidenceRelationshipKind
from spine.domain.capabilities.semantic_support import (
    SemanticSupportEvidence,
    SemanticSupportPolicy,
    SemanticSupportRequest,
    SemanticSupportOutcome,
)
from spine.domain.capabilities.answer_question_execution import (
    PinnedAgentVersion,
    PinnedConfigurationVersion,
    PinnedContextProfile,
    PinnedProjection,
    TrustedAnswerQuestionConfiguration,
    agent_version_digest,
)
from spine.domain.common import EnvironmentKind, SchemaRef
from spine.domain.context_profiles import (
    CompletenessRequirement,
    ContextProfileVersion,
    PartialAnswerFallback,
)
from spine.domain.projections import PublishedActivePublication
from spine.domain.sources import RevisionKind
from spine.domain.context_bundles import (
    RevalidationCheckpointResult,
    RevalidationDisposition,
)
from spine.domain.disclosure import (
    DisclosureDependencyObservation,
    DisclosurePublicationImpact,
    DisclosurePublicationObservation,
    DisclosureRevalidationObservation,
)
from spine.memory.contracts import (
    ContextResponse,
    EvidenceReference,
    ProjectionChunk,
    ProjectionOutcome,
    ProjectionRetrievalResult,
    StableLocator,
)
from spine.memory.read_plan import PhysicalReadPlanBuilder
from spine.memory.retrieval import PhysicalReadPlanExecutor
from spine.memory.namespace import ProjectionNamespaceRouter
from spine.auth.contracts import RequestedAuthorizationScope
from spine.infrastructure.persistence.postgresql_authorization import (
    PostgreSQLQualificationAuthorityResolver,
)
from spine.infrastructure.persistence.contexts import TrustedContextBoundary
from spine.infrastructure.qa_agent_runtime import (
    ConfiguredGroundedConflictValidator,
    ConfiguredMissingCoverageImpactValidator,
    ConfiguredLLMRuntimeProvider,
    ConfiguredSemanticSupportValidator,
    PostgreSQLQAArtifactStore,
    SEMANTIC_SUPPORT_POLICY_BINDING,
    SEMANTIC_SUPPORT_POLICY_DIGEST,
    SEMANTIC_SUPPORT_POLICY_VERSION,
)


_LOGGER = logging.getLogger(__name__)


_CAPABILITY_ID = uuid5(NAMESPACE_URL, "spine:capability:answer-question:v1")
_AGENT_ID = uuid5(NAMESPACE_URL, "spine:agent:governed-qa:v1")
_AGENT_VERSION_ID = uuid5(NAMESPACE_URL, "spine:agent-version:governed-qa:2026.10")
_CAPABILITY = CapabilityDefinition(
    id=_CAPABILITY_ID,
    key="answer_question",
    display_name="Governed question answering",
    input_schema=SchemaRef(name="answer-question.input", version="v1"),
    output_schema=SchemaRef(name="answer-question.output", version="v2"),
)
_AGENT_VERSION = AgentVersion(
    id=_AGENT_VERSION_ID,
    agent_id=_AGENT_ID,
    version="2026.10",
    runtime=AgentRuntimeKind.LLM,
    capabilities=("answer_question",),
    prompt_version="qa.prompt:r1",
    model_profile="qa.model:configured",
    context_profile="qa.context:r1",
    runtime_config={"provider_family": "configured"},
)


@dataclass(frozen=True, slots=True)
class _ActiveRequestScope:
    workspace_id: UUID
    environment_id: UUID
    trace_id: UUID


@dataclass(frozen=True, slots=True)
class _ActiveCompletenessIntent:
    allow_partial_answer: bool | None
    command_id: UUID


_ACTIVE_REQUEST_SCOPE: ContextVar[_ActiveRequestScope | None] = ContextVar(
    "spine_qa_active_request_scope", default=None
)
_ACTIVE_AUTHORIZATION_CONTEXT: ContextVar[TrustedAuthorizationContext | None] = (
    ContextVar("spine_qa_active_authorization_context", default=None)
)
_ACTIVE_EVIDENCE_CONTENT: ContextVar[dict[str, str] | None] = ContextVar(
    "spine_qa_active_evidence_content", default=None
)
_ACTIVE_RETRIEVAL_SCOPE: ContextVar[AuthorizedRetrievalScope | None] = ContextVar(
    "spine_qa_active_retrieval_scope", default=None
)
_ACTIVE_COMPLETENESS_INTENT: ContextVar[_ActiveCompletenessIntent | None] = (
    ContextVar("spine_qa_active_completeness_intent", default=None)
)


def _active_request_scope() -> _ActiveRequestScope:
    scope = _ACTIVE_REQUEST_SCOPE.get()
    if scope is None:
        raise RuntimeError("active Q&A request scope is unavailable")
    return scope


def _admitted_authority_generation() -> int:
    context = _ACTIVE_AUTHORIZATION_CONTEXT.get()
    if context is None:
        raise RuntimeError("active Q&A authorization context is unavailable")
    return context.authority_generation


class _NeverDirectHandler:
    async def invoke(self, request: object, context: object) -> object:
        del request, context
        raise RuntimeError("Q&A handler requires the governed runtime boundary")


class _ScopedAgentCatalog:
    def list_bindings(
        self, workspace_id: UUID, environment: EnvironmentKind, capability_key: str
    ) -> tuple[AgentBinding, ...]:
        if capability_key != "answer_question":
            return ()
        return (
            AgentBinding(
                id=uuid5(workspace_id, f"answer-question-binding:{environment.value}"),
                workspace_id=workspace_id,
                environment=environment,
                capability=capability_key,
                agent_version_id=_AGENT_VERSION.id,
            ),
        )

    def get_agent_version(self, agent_version_id: UUID) -> AgentVersion | None:
        return _AGENT_VERSION if agent_version_id == _AGENT_VERSION.id else None

    def list_deployments(
        self,
        workspace_id: UUID,
        environment: EnvironmentKind,
        agent_version_id: UUID,
    ) -> tuple[AgentDeployment, ...]:
        if agent_version_id != _AGENT_VERSION.id:
            return ()
        return (
            AgentDeployment(
                id=uuid5(workspace_id, f"answer-question-deployment:{environment.value}"),
                workspace_id=workspace_id,
                environment=environment,
                agent_version_id=agent_version_id,
                stage=DeploymentStage.PRODUCTION,
            ),
        )


class _ScopedConfigurationCatalog:
    def __init__(
        self,
        *,
        session_factory: object,
        uow_factory: object,
        boundary: TrustedContextBoundary,
        service_principal_id: UUID,
    ) -> None:
        self._session_factory = session_factory
        self._uow_factory = uow_factory
        self._boundary = boundary
        self._service_principal_id = service_principal_id
        self._configurations: dict[
            tuple[UUID, UUID], TrustedAnswerQuestionConfiguration
        ] = {}

    async def prepare(self, command: object) -> None:
        """Pin the current canonical publication before attempt allocation."""

        workspace_id = getattr(command, "workspace_id", None)
        environment_id = getattr(command, "environment_id", None)
        trace_id = getattr(command, "run_id", None)
        if not all(isinstance(value, UUID) for value in (workspace_id, environment_id, trace_id)):
            raise RuntimeError("Q&A execution scope is unavailable")
        context = self._boundary.worker(
            scope=EnvironmentScope(
                workspace_id=workspace_id,
                environment_id=environment_id,
            ),
            service_principal_id=self._service_principal_id,
            purpose=PersistencePurpose("governed_qa.configuration.v1"),
            operation=PersistenceOperation("resolve_active_projection"),
            trace_id=trace_id,
        )
        async with self._uow_factory(context) as uow:  # type: ignore[operator]
            publication = await uow.projections.resolve_active_publication("qa")
            await uow.commit()
        if not isinstance(publication, PublishedActivePublication):
            raise RuntimeError("active Q&A publication is unavailable")
        profile = await self._current_profile(workspace_id, environment_id)
        self._configurations[(workspace_id, environment_id)] = self._configuration(
            publication=publication,
            profile=profile,
        )

    async def _current_profile(
        self, workspace_id: UUID, environment_id: UUID
    ) -> ContextProfileVersion:
        session = self._session_factory()  # type: ignore[operator]
        async with session:
            async with session.begin():
                await session.execute(
                    __import__("sqlalchemy").text(
                        "SELECT set_config('spine.workspace_id', :workspace_id, true), "
                        "set_config('spine.environment_id', :environment_id, true)"
                    ),
                    {
                        "workspace_id": str(workspace_id),
                        "environment_id": str(environment_id),
                    },
                )
                rows = (
                    await session.execute(
                        __import__("sqlalchemy").text(
                            "SELECT * FROM spine.context_profiles "
                            "WHERE workspace_id = :workspace_id "
                            "AND environment_id = :environment_id "
                            "AND server_approved IS TRUE AND revoked IS FALSE "
                            "ORDER BY profile_version DESC LIMIT 2"
                        ),
                        {
                            "workspace_id": str(workspace_id),
                            "environment_id": str(environment_id),
                        },
                    )
                ).mappings().all()
        if len(rows) != 1:
            raise RuntimeError("approved Q&A context profile is unavailable")
        return ContextProfileVersion.model_validate(dict(rows[0]))

    def _configuration(
        self,
        *,
        publication: PublishedActivePublication,
        profile: ContextProfileVersion,
    ) -> TrustedAnswerQuestionConfiguration:
        workspace_id = publication.workspace_id
        environment_id = publication.environment_id
        generation = publication.activation_generation

        def scoped(name: str) -> UUID:
            return uuid5(workspace_id, f"{environment_id}:governed-qa:{name}:v1")

        common = {
            "workspace_id": workspace_id,
            "environment_id": environment_id,
            "configuration_generation": generation,
        }
        projection = publication.projection_config_version
        return TrustedAnswerQuestionConfiguration(
            configuration_generation=generation,
            capability_id=_CAPABILITY.id,
            input_schema=_CAPABILITY.input_schema,
            output_schema=_CAPABILITY.output_schema,
            agent_version=PinnedAgentVersion(
                **common,
                agent_version_id=_AGENT_VERSION.id,
                agent_id=_AGENT_VERSION.agent_id,
                version=_AGENT_VERSION.version,
                digest=agent_version_digest(_AGENT_VERSION),
            ),
            prompt_version=PinnedConfigurationVersion(
                **common,
                identity_id=scoped("prompt"),
                binding_key=_AGENT_VERSION.prompt_version,
                version="r1",
                digest=uuid5(NAMESPACE_URL, "qa.prompt:r1").hex * 2,
            ),
            model_profile=PinnedConfigurationVersion(
                **common,
                identity_id=scoped("model"),
                binding_key=_AGENT_VERSION.model_profile,
                version="configured",
                digest=uuid5(NAMESPACE_URL, "qa.model:configured").hex * 2,
            ),
            context_profile=PinnedContextProfile(
                **common,
                binding_key=_AGENT_VERSION.context_profile,
                profile=profile.reference,
            ),
            projection=PinnedProjection(
                **common,
                projection_snapshot_id=publication.projection_snapshot.projection_snapshot_id,
                snapshot_manifest_digest=publication.projection_snapshot.manifest_digest,
                projection_activation_id=publication.projection_activation.projection_activation_id,
                activation_generation=publication.activation_generation,
                projection_config_version_id=projection.projection_config_version_id,
                projection_config_digest=projection.config_digest,
                canonical_boundary_id=publication.canonical_boundary_id,
                canonical_boundary_digest=publication.canonical_boundary_manifest_digest,
            ),
            semantic_support_policy=PinnedConfigurationVersion(
                **common,
                identity_id=scoped("semantic-policy"),
                binding_key=SEMANTIC_SUPPORT_POLICY_BINDING,
                version=SEMANTIC_SUPPORT_POLICY_VERSION,
                digest=SEMANTIC_SUPPORT_POLICY_DIGEST,
            ),
            approved=True,
        )

    def resolve_for_agent_version(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        environment: EnvironmentKind,
        capability_key: str,
        agent_version_id: UUID,
    ) -> TrustedAnswerQuestionConfiguration | None:
        if (
            environment is not EnvironmentKind.PRODUCTION
            or capability_key != "answer_question"
            or agent_version_id != _AGENT_VERSION.id
        ):
            return None
        return self._configurations.get((workspace_id, environment_id))

    def verify_configuration(
        self, configuration: TrustedAnswerQuestionConfiguration
    ) -> bool:
        return configuration.approved


class _UnavailableRuntimeProvider:
    def runtime_for(self, **_: object) -> object:
        raise RuntimeError("configured Q&A model runtime is unavailable")


class _TrustedContextRegistry:
    def __init__(self, service_principal_id: UUID) -> None:
        self._service_principal_id = service_principal_id
        self._commands: dict[UUID, object] = {}
        self._contexts: dict[UUID, TrustedAuthorizationContext] = {}

    def register(self, reference: UUID, command: object) -> None:
        self._commands[reference] = command

    def register_context(
        self, reference: UUID, context: TrustedAuthorizationContext
    ) -> None:
        if (
            type(context) is not TrustedAuthorizationContext
            or context.authority_snapshot_reference != reference
            or context.service_principal_id != self._service_principal_id
            or reference in self._commands
            or reference in self._contexts
        ):
            raise ValueError("qualified Q&A authorization context is invalid")
        self._contexts[reference] = context

    def unregister_context(self, reference: UUID) -> None:
        self._contexts.pop(reference, None)

    def contains_context(self, context: TrustedAuthorizationContext) -> bool:
        return (
            type(context) is TrustedAuthorizationContext
            and self._contexts.get(context.authority_snapshot_reference) == context
        )

    async def resolve(self, request: object) -> TrustedAuthorizationContext | None:
        reference = getattr(request, "authorization_context_ref", None)
        issued = self._contexts.get(reference)
        if issued is not None:
            diagnostic = getattr(request, "diagnostic_context", None)
            command_id = getattr(request, "request_id", None)
            if (
                getattr(request, "workspace_id", None) != issued.workspace_id
                or getattr(request, "environment_id", None) != issued.environment_id
                or getattr(diagnostic, "trace_id", None) != issued.trace_id
                or not isinstance(command_id, UUID)
            ):
                return None
            self._activate(
                issued,
                allow_partial_answer=getattr(request, "allow_partial_answer", None),
                command_id=command_id,
            )
            return issued
        command = self._commands.get(reference)
        request_context = getattr(command, "request_context", None)
        if (
            request_context is None
            or getattr(request, "workspace_id", None) != request_context.workspace_id
            or getattr(request, "environment_id", None) != request_context.environment_id
            or getattr(request, "diagnostic_context", None) is None
        ):
            return None
        command_id = getattr(command, "command_id", None)
        if not isinstance(command_id, UUID):
            raise RuntimeError("active Q&A command identity is unavailable")
        context = TrustedAuthorizationContext(
            mode=AuthorizationContextMode.INTERACTIVE_PROCESSING,
            workspace_id=request_context.workspace_id,
            environment_id=request_context.environment_id,
            canonical_human_id=request_context.subject_id,
            service_principal_id=self._service_principal_id,
            purpose="answer_question",
            operation="retrieve_context",
            membership_generation=request_context.authorization_generation,
            authority_generation=request_context.authorization_generation,
            service_authority_generation=request_context.authorization_generation,
            authority_snapshot_reference=reference,
            trace_id=request_context.trace_id,
            issued_at=datetime.now(timezone.utc),
        )
        self._activate(
            context,
            allow_partial_answer=getattr(command, "allow_partial_answer", None),
            command_id=command_id,
        )
        return context

    @staticmethod
    def _activate(
        context: TrustedAuthorizationContext,
        *,
        allow_partial_answer: bool | None,
        command_id: UUID,
    ) -> None:
        _ACTIVE_REQUEST_SCOPE.set(
            _ActiveRequestScope(
                workspace_id=context.workspace_id,
                environment_id=context.environment_id,
                trace_id=context.trace_id,
            )
        )
        _ACTIVE_COMPLETENESS_INTENT.set(
            _ActiveCompletenessIntent(
                allow_partial_answer=allow_partial_answer,
                command_id=command_id,
            )
        )
        _ACTIVE_AUTHORIZATION_CONTEXT.set(context)


def _noninteractive_authority_reference(reference: UUID) -> UUID:
    return uuid5(reference, "spine:qualified-context-broker:noninteractive:v1")


class OneShotQualifiedContextBroker:
    """Issue current service authority and perform at most one Broker request.

    This operator-only module is not wired to an HTTP route or worker startup.
    Binding creation and revocation remain outside its interface.
    """

    def __init__(
        self,
        *,
        broker: object,
        trusted_contexts: _TrustedContextRegistry,
        authority_resolver: object,
        service_principal_id: UUID,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._broker = broker
        self._trusted_contexts = trusted_contexts
        self._authority_resolver = authority_resolver
        self._service_principal_id = service_principal_id
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._used = False
        self._lock = asyncio.Lock()

    async def retrieve(self, request: AuthorizedContextRequest) -> ContextResponse:
        if type(request) is not AuthorizedContextRequest:
            raise TypeError("qualified Context Broker request is invalid")
        async with self._lock:
            if self._used:
                raise RuntimeError("qualified Context Broker request was already used")
            generation = await self._authority_resolver.resolve_service_authority(
                principal_id=self._service_principal_id,
                requested_scope=RequestedAuthorizationScope(
                    workspace_id=request.workspace_id,
                    environment_id=request.environment_id,
                ),
                purpose="answer_question",
                operation="retrieve_context",
            )
            if (
                not isinstance(generation, int)
                or isinstance(generation, bool)
                or generation < 1
            ):
                raise RuntimeError("qualified Q&A service authority is unavailable")
            context = TrustedAuthorizationContext(
                mode=AuthorizationContextMode.NONINTERACTIVE_PROCESSING,
                workspace_id=request.workspace_id,
                environment_id=request.environment_id,
                canonical_human_id=None,
                service_principal_id=self._service_principal_id,
                purpose="answer_question",
                operation="retrieve_context",
                membership_generation=generation,
                authority_generation=generation,
                service_authority_generation=generation,
                authority_snapshot_reference=request.authorization_context_ref,
                trace_id=request.diagnostic_context.trace_id,
                issued_at=self._clock(),
                noninteractive_authority_reference=(
                    _noninteractive_authority_reference(
                        request.authorization_context_ref
                    )
                ),
            )
            self._trusted_contexts.register_context(
                request.authorization_context_ref, context
            )
            self._used = True
            try:
                response = await self._broker.retrieve(request)
                if type(response) is not ContextResponse:
                    raise RuntimeError("qualified Context Broker response is invalid")
                return response
            finally:
                self._trusted_contexts.unregister_context(
                    request.authorization_context_ref
                )


class _ScopedPort:
    def __init__(
        self,
        *,
        uow_factory: object,
        boundary: TrustedContextBoundary,
        service_principal_id: UUID,
    ) -> None:
        self._uow_factory = uow_factory
        self._boundary = boundary
        self._service_principal_id = service_principal_id

    def context(self):
        scope = _active_request_scope()
        return _context_broker_persistence_context(
            boundary=self._boundary,
            workspace_id=scope.workspace_id,
            environment_id=scope.environment_id,
            service_principal_id=self._service_principal_id,
            trace_id=scope.trace_id,
        )


def _context_broker_persistence_context(
    *,
    boundary: TrustedContextBoundary,
    workspace_id: UUID,
    environment_id: UUID,
    service_principal_id: UUID,
    trace_id: UUID,
):
    """Issue the single qualified authority used by one Broker request.

    Repository method names are implementation details, not independently
    delegated operations. Qualification is bound to the Context Broker's
    capability-level retrieval operation for every persistence unit of work.
    """

    return boundary.worker(
        scope=EnvironmentScope(
            workspace_id=workspace_id,
            environment_id=environment_id,
        ),
        service_principal_id=service_principal_id,
        purpose=PersistencePurpose("answer_question"),
        operation=PersistenceOperation("retrieve_context"),
        trace_id=trace_id,
    )


class _ProfileRepository(_ScopedPort):
    async def resolve_for_scope(self, **kwargs: object) -> ContextProfileVersion:
        async with self._uow_factory(self.context()) as uow:  # type: ignore[operator]
            result = await uow.context_profiles.resolve_for_scope(**kwargs)
            await uow.commit()
            return result


class _AllowContextRetrieval:
    async def allows(self, request: object) -> bool:
        scope = _active_request_scope()
        return (
            getattr(request, "workspace_id", None) == scope.workspace_id
            and getattr(request, "environment_id", None) == scope.environment_id
        )


class _ActivePublication(_ScopedPort):
    async def resolve_active_publication(self, projection_kind: str) -> object:
        async with self._uow_factory(self.context()) as uow:  # type: ignore[operator]
            result = await uow.projections.resolve_active_publication(projection_kind)
            await uow.commit()
            return result


class _SnapshotMembers(_ScopedPort):
    async def list_snapshot_members(self, projection_snapshot_id: UUID) -> tuple[object, ...]:
        async with self._uow_factory(self.context()) as uow:  # type: ignore[operator]
            result = await uow.projections.list_snapshot_members(projection_snapshot_id)
            await uow.commit()
            return result


class _CanonicalBoundaryMembers(_ScopedPort):
    async def resolve_members(self, boundary_id: UUID) -> tuple[object, ...]:
        async with self._uow_factory(self.context()) as uow:  # type: ignore[operator]
            result = await uow.canonical_boundaries.resolve_members(boundary_id)
            await uow.commit()
            return result

    async def resolve_current_members(self) -> tuple[object, ...]:
        """Read the current canonical heads independently of a pinned projection."""

        async with self._uow_factory(self.context()) as uow:  # type: ignore[operator]
            heads = await uow.canonical.list_current()
            await uow.commit()
            return heads


class _CurrentContentAuthorization(_ScopedPort):
    async def resolve_current_content_authorization(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        source_object_id: UUID,
    ) -> CurrentContentAuthorizationSnapshot | None:
        scope = _active_request_scope()
        if (workspace_id, environment_id) != (scope.workspace_id, scope.environment_id):
            return None
        async with self._uow_factory(self.context()) as uow:  # type: ignore[operator]
            source = await uow.sources.resolve_source(source_object_id)
            head = await uow.canonical.resolve_current(source_object_id)
            current_policy = await uow.access_policies.resolve_current(source_object_id)
            revision = (
                None
                if head is None
                else await uow.sources.resolve_revision(head.current_revision_id)
            )
            policy = (
                None
                if current_policy is None
                else await uow.access_policies.resolve_policy(
                    current_policy.access_policy_version_id
                )
            )
            provenance = (
                ()
                if revision is None
                else await uow.sources.list_provenance(revision.revision_id)
            )
            await uow.commit()
        if source is None or head is None or revision is None:
            return None
        return CurrentContentAuthorizationSnapshot(
            workspace_id=workspace_id,
            environment_id=environment_id,
            source_object_id=source_object_id,
            current_revision_id=head.current_revision_id,
            current_revision_kind=head.current_revision_kind,
            current_revision_observed_at=revision.observed_at,
            retention_eligible=head.current_revision_kind is RevisionKind.CONTENT,
            provenance_complete=bool(provenance),
            current_policy=current_policy,
            policy=policy,
        )


class _CurrentIdentityAuthority:
    def __init__(self, session_factory: object) -> None:
        self._resolver = PostgreSQLQualificationAuthorityResolver(session_factory)  # type: ignore[arg-type]

    async def revalidate(
        self, context: TrustedAuthorizationContext
    ) -> TrustedAuthorizationContext:
        scope = _active_request_scope()
        if (
            context.workspace_id != scope.workspace_id
            or context.environment_id != scope.environment_id
        ):
            raise RuntimeError("Q&A authority scope changed")
        requested_scope = RequestedAuthorizationScope(
            workspace_id=context.workspace_id,
            environment_id=context.environment_id,
        )
        human_generation: int | None = None
        if context.canonical_human_id is not None:
            human_generation = await self._resolver.resolve_human_authority(
                principal_id=context.canonical_human_id,
                requested_scope=requested_scope,
            )
            if human_generation is None:
                raise RuntimeError("Q&A human authority is unavailable")
        service_generation: int | None = None
        if context.service_principal_id is not None:
            service_generation = await self._resolver.resolve_service_authority(
                principal_id=context.service_principal_id,
                requested_scope=requested_scope,
                purpose=context.purpose,
                operation=context.operation,
            )
            if service_generation is None:
                raise RuntimeError("Q&A service authority is unavailable")
        current_generation = human_generation or service_generation
        if current_generation is None:
            raise RuntimeError("Q&A authority is unavailable")
        return context.model_copy(
            update={
                "membership_generation": current_generation,
                "authority_generation": current_generation,
                "service_authority_generation": service_generation,
            }
        )


class _CurrentNoninteractiveAuthority:
    """Revalidate the exact server-issued service-only Broker authority."""

    def __init__(
        self,
        resolver: object,
        service_principal_id: UUID,
        trusted_contexts: _TrustedContextRegistry,
    ) -> None:
        self._resolver = resolver
        self._service_principal_id = service_principal_id
        self._trusted_contexts = trusted_contexts

    async def authorize(self, context: TrustedAuthorizationContext) -> bool:
        if (
            type(context) is not TrustedAuthorizationContext
            or context.mode is not AuthorizationContextMode.NONINTERACTIVE_PROCESSING
            or context.canonical_human_id is not None
            or context.service_principal_id != self._service_principal_id
            or context.purpose != "answer_question"
            or context.operation != "retrieve_context"
            or not self._trusted_contexts.contains_context(context)
            or context.noninteractive_authority_reference
            != _noninteractive_authority_reference(
                context.authority_snapshot_reference
            )
        ):
            return False
        generation = await self._resolver.resolve_service_authority(
            principal_id=self._service_principal_id,
            requested_scope=RequestedAuthorizationScope(
                workspace_id=context.workspace_id,
                environment_id=context.environment_id,
            ),
            purpose=context.purpose,
            operation=context.operation,
        )
        return (
            isinstance(generation, int)
            and not isinstance(generation, bool)
            and generation >= 1
            and context.membership_generation == generation
            and context.authority_generation == generation
            and context.service_authority_generation == generation
        )


class _CurrentQAServiceAuthority:
    def __init__(self, session_factory: object, service_principal_id: UUID) -> None:
        self._resolver = PostgreSQLQualificationAuthorityResolver(session_factory)  # type: ignore[arg-type]
        self._service_principal_id = service_principal_id

    async def __call__(self, workspace_id: UUID, environment_id: UUID) -> int | None:
        return await self._resolver.resolve_service_authority(
            principal_id=self._service_principal_id,
            requested_scope=RequestedAuthorizationScope(
                workspace_id=workspace_id,
                environment_id=environment_id,
            ),
            purpose="governed_qa.execution.v1",
            operation="answer_question_run",
        )


class _CanonicalSourceIsolation:
    async def can_express(self, scope: object) -> bool:
        if isinstance(scope, AuthorizedRetrievalScope):
            _ACTIVE_RETRIEVAL_SCOPE.set(scope)
        constraints = getattr(scope, "source_constraints", ())
        return bool(constraints)


class _ContextReadModel(_ScopedPort):
    async def load(self, *, publication: object, scope: object) -> ContextBrokerReadModel:
        del scope
        if not isinstance(publication, PublishedActivePublication):
            raise RuntimeError("active publication is unavailable")
        run_id = publication.projection_snapshot.projection_run_id
        async with self._uow_factory(self.context()) as uow:  # type: ignore[operator]
            run = await uow.projections.resolve_run(run_id)
            members = await uow.projections.list_snapshot_members(
                publication.projection_snapshot.projection_snapshot_id
            )
            included = tuple(item for item in members if item.disposition.value == "included")
            partitions = await uow.projections.list_partitions(run_id)
            receipts = await uow.projections.list_receipts(run_id)
            references = await uow.projections.list_source_references(run_id)
            revisions_by_id = {}
            for reference in references:
                revision = await uow.sources.resolve_revision(
                    reference.source_revision_id
                )
                if revision is not None:
                    revisions_by_id[revision.revision_id] = revision
            boundary = await uow.canonical_boundaries.resolve(
                publication.canonical_boundary_id
            )
            await uow.commit()
        if run is None or boundary is None:
            raise RuntimeError("canonical projection read model is unavailable")
        return ContextBrokerReadModel(
            projection_run=run,
            snapshot_members=included,
            partitions=partitions,
            receipts=receipts,
            source_references=references,
            source_revisions=tuple(revisions_by_id.values()),
            canonical_boundary=boundary,
            provider_capability_reference="postgresql.parsed-projection:v1",
        )


class _CanonicalNamespaceRegistry:
    def require(self, namespace: object, context: object) -> object:
        return ProjectionNamespaceRouter().validate(namespace, context)  # type: ignore[arg-type]


class _ParsedProjectionRetrieval:
    """Read only segments proven by the canonical physical read command."""

    supports_source_isolation = True

    def __init__(self, session_factory: object) -> None:
        self._session_factory = session_factory

    async def retrieve(self, command: object) -> ProjectionRetrievalResult:
        workspace_id = getattr(command, "workspace_id")
        environment_id = getattr(command, "environment_id")
        sources = tuple(getattr(command, "retrieval_sources"))
        if not sources:
            raise RuntimeError("parsed projection retrieval requires isolated sources")
        by_revision = {source.source_revision_id: source for source in sources}
        session = self._session_factory()  # type: ignore[operator]
        query = text(
            "SELECT d.source_revision_id, d.source_object_id, s.segment_id, "
            "s.segment_digest, s.text, r.locator_reference, "
            "r.locator_digest, r.projected_kind, r.projected_object_id, "
            "r.segment_id AS evidence_id, "
            "revision.revision_digest, "
            "boundary_member.accepted_generation AS revision_number "
            "FROM spine.parsed_documents d "
            "JOIN spine.parsed_document_segments s ON s.artifact_id = d.artifact_id "
            "JOIN spine.source_projection_references r "
            "ON r.source_revision_id = d.source_revision_id "
            "AND r.segment_id = CONCAT('segment-', REPLACE(CAST(s.segment_id AS text), '-', '')) "
            "JOIN spine.source_revisions revision "
            "ON revision.workspace_id = d.workspace_id "
            "AND revision.environment_id = d.environment_id "
            "AND revision.source_object_id = d.source_object_id "
            "AND revision.revision_id = d.source_revision_id "
            "JOIN spine.projection_snapshot_members snapshot_member "
            "ON snapshot_member.workspace_id = d.workspace_id "
            "AND snapshot_member.environment_id = d.environment_id "
            "AND snapshot_member.source_object_id = d.source_object_id "
            "AND snapshot_member.source_revision_id = d.source_revision_id "
            "JOIN spine.canonical_boundary_members boundary_member "
            "ON boundary_member.workspace_id = snapshot_member.workspace_id "
            "AND boundary_member.environment_id = snapshot_member.environment_id "
            "AND boundary_member.boundary_id = snapshot_member.canonical_boundary_id "
            "AND boundary_member.source_object_id = snapshot_member.source_object_id "
            "AND boundary_member.source_revision_id = snapshot_member.source_revision_id "
            "WHERE d.workspace_id = :workspace_id AND d.environment_id = :environment_id "
            "AND d.source_revision_id = ANY(CAST(:source_revision_ids AS uuid[])) "
            "AND r.projection_run_id = :projection_run_id "
            "AND snapshot_member.projection_snapshot_id = :projection_snapshot_id "
            "AND length(trim(s.text)) > 0 ORDER BY s.segment_ordinal LIMIT :limit"
        )
        async with session:
            async with session.begin():
                await session.execute(
                    text(
                        "SELECT set_config('spine.workspace_id', :workspace_id, true), "
                        "set_config('spine.environment_id', :environment_id, true)"
                    ),
                    {
                        "workspace_id": str(workspace_id),
                        "environment_id": str(environment_id),
                    },
                )
                rows = (
                    await session.execute(
                        query,
                        {
                            "workspace_id": str(workspace_id),
                            "environment_id": str(environment_id),
                            "source_revision_ids": [str(item) for item in by_revision],
                            "projection_run_id": str(getattr(command, "projection_run_id")),
                            "projection_snapshot_id": str(
                                getattr(command, "projection_candidate").snapshot_id
                            ),
                            "limit": getattr(command, "limit"),
                        },
                    )
                ).mappings().all()
        chunks = []
        evidence_content: dict[str, str] = {}
        for row in rows:
            source = by_revision.get(row.source_revision_id)
            if source is None or source.source_object_id != row.source_object_id:
                raise RuntimeError("retrieval returned a source outside the read plan")
            kind, value = str(row.locator_reference).split(":", 1)
            reference = EvidenceReference(
                evidence_id=str(row.evidence_id),
                workspace_id=workspace_id,
                environment_id=environment_id,
                source_object_id=source.source_object_id,
                source_revision_id=source.source_revision_id,
                revision_digest=row.revision_digest,
                revision_number=row.revision_number,
                processing_receipt_id=source.processing_receipt_id,
                locator_contract_version=source.locator_contract_version,
                locator=StableLocator(kind=kind, value=value),
                evidence_digest=row.segment_digest,
            )
            chunks.append(
                ProjectionChunk(
                    chunk_id=str(row.projected_object_id),
                    text=row.text,
                    evidence=(reference,),
                )
            )
            evidence_content[str(row.evidence_id)] = row.text
        _ACTIVE_EVIDENCE_CONTENT.set(evidence_content)
        return ProjectionRetrievalResult(
            outcome=(ProjectionOutcome.SUCCEEDED if chunks else ProjectionOutcome.MISSING),
            command_identity=getattr(command, "command_identity"),
            command_digest=getattr(command, "command_digest"),
            chunks=tuple(chunks),
            evidence=tuple(item.evidence[0] for item in chunks),
        )


class _CanonicalEvidenceVerifier(_ScopedPort):
    def __init__(self, *, authorization: ContentAuthorizationDecisionService, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._authorization = authorization

    async def verify(
        self,
        *,
        item: object,
        publication_pin: object,
        authorization_context: TrustedAuthorizationContext,
    ) -> object:
        decision_id = uuid5(
            NAMESPACE_URL,
            f"spine:qa-evidence-verification:{authorization_context.trace_id}:"
            f"{getattr(item, 'chunk_id', getattr(item, 'entity_id', getattr(item, 'relation_id', 'unknown')))}",
        )
        try:
            evidence = tuple(getattr(item, "evidence"))
            if not evidence:
                raise ValueError
            source_ids = tuple(dict.fromkeys(value.source_object_id for value in evidence))
            decision = await self._decide(authorization_context, source_ids)
            if not decision.allowed:
                return RejectedEvidenceItem(
                    reason_code=EvidenceVerificationReasonCode.AUTHORIZATION_DENIED,
                    decision_reference=decision_id,
                    diagnostic=EvidenceVerificationDiagnostic(),
                )
            snapshot_id = publication_pin.projection_snapshot.projection_snapshot_id
            run_id = publication_pin.projection_snapshot.projection_run_id
            async with self._uow_factory(self.context()) as uow:  # type: ignore[operator]
                references = await uow.projections.list_source_references(run_id)
                members = await uow.projections.list_snapshot_members(snapshot_id)
                revisions = {
                    reference.source_revision_id: await uow.sources.resolve_revision(
                        reference.source_revision_id
                    )
                    for reference in references
                }
                await uow.commit()
            item_kind, item_id = (
                ("chunk", item.chunk_id)
                if hasattr(item, "chunk_id")
                else ("entity", item.entity_id)
                if hasattr(item, "entity_id")
                else ("relation", item.relation_id)
            )
            for claim in evidence:
                matches = tuple(
                    reference
                    for reference in references
                    if reference.projected_kind == item_kind
                    and reference.projected_object_id == item_id
                    and reference.source_object_id == claim.source_object_id
                    and reference.source_revision_id == claim.source_revision_id
                    and reference.processing_receipt_id == claim.processing_receipt_id
                    and reference.locator_schema_version
                    == claim.locator_contract_version
                    and reference.locator_digest
                    == canonical_projection_locator_digest(
                        locator_contract_version=claim.locator_contract_version,
                        locator=claim.locator,
                    )
                )
                revision = revisions.get(claim.source_revision_id)
                member_current = any(
                    member.source_object_id == claim.source_object_id
                    and member.source_revision_id == claim.source_revision_id
                    and member.disposition.value == "included"
                    for member in members
                )
                if (
                    len(matches) != 1
                    or revision is None
                    or revision.revision_digest != claim.revision_digest
                    or not member_current
                ):
                    raise ValueError
            return VerifiedEvidenceItem(
                item=item,
                publication_pin_id=publication_pin.pin_id,
                authorization_decision_reference=decision.decision_reference,
                decision_reference=decision_id,
            )
        except Exception:
            return RejectedEvidenceItem(
                reason_code=EvidenceVerificationReasonCode.EVIDENCE_UNVERIFIED,
                decision_reference=decision_id,
                diagnostic=EvidenceVerificationDiagnostic(),
            )

    async def _decide(
        self,
        context: TrustedAuthorizationContext,
        source_ids: tuple[UUID, ...],
    ) -> ContentAuthorizationDecision:
        if context.mode is AuthorizationContextMode.HUMAN_DISCLOSURE:
            return await self._authorization.decide_human_disclosure(
                context, source_ids
            )
        if context.mode is AuthorizationContextMode.INTERACTIVE_PROCESSING:
            return await self._authorization.decide_interactive_processing(
                context, source_ids
            )
        return await self._authorization.decide_noninteractive_processing(
            context, source_ids
        )


class _CurrentBundleRevalidator(_ScopedPort):
    async def revalidate(self, request: object) -> RevalidationCheckpointResult:
        bundle = request.bundle
        safe = True
        async with self._uow_factory(self.context()) as uow:  # type: ignore[operator]
            publication = await uow.projections.resolve_active_publication("qa")
            for evidence in bundle.evidence_references:
                head = await uow.canonical.resolve_current(evidence.source_object_id)
                policy = await uow.access_policies.resolve_current(
                    evidence.source_object_id
                )
                if (
                    head is None
                    or head.current_revision_id != evidence.source_revision_id
                    or head.current_revision_kind is not RevisionKind.CONTENT
                    or policy is None
                ):
                    safe = False
            await uow.commit()
        safe = safe and isinstance(publication, PublishedActivePublication) and (
            publication.projection_snapshot.projection_snapshot_id
            == bundle.projection_snapshot.projection_snapshot_id
            and publication.activation_generation == bundle.activation_generation
        )
        return RevalidationCheckpointResult(
            disposition=(
                RevalidationDisposition.SAFE_REPLAY
                if safe
                else RevalidationDisposition.REJECTED
            ),
            checkpoint=request.checkpoint,
            workspace_id=request.workspace_id,
            environment_id=request.environment_id,
            bundle_reference=bundle.reference if safe else None,
            bundle_outcome=bundle.outcome if safe else None,
            idempotency_key=request.idempotency_key,
            command_digest=request.command_digest,
            retry_attempt=request.retry_attempt,
        )


class _CurrentCitationValidator(_ScopedPort):
    """Recheck an exact bundle citation against current canonical authority."""

    def __init__(self, *, authorization: ContentAuthorizationDecisionService, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._authorization = authorization

    async def validate(
        self,
        *,
        citation: object,
        expected_context_bundle: object,
        authorization_context: TrustedAuthorizationContext,
    ) -> object:
        reference_id = uuid5(
            NAMESPACE_URL,
            f"spine:qa-citation-validation:{getattr(citation, 'citation_id', 'invalid')}",
        )
        try:
            scope = _active_request_scope()
            if (
                getattr(citation, "context_bundle", None) != expected_context_bundle
                or getattr(citation, "workspace_id", None) != scope.workspace_id
                or getattr(citation, "environment_id", None) != scope.environment_id
            ):
                raise ValueError
            bundle_ref = expected_context_bundle.bundle
            async with self._uow_factory(self.context()) as uow:  # type: ignore[operator]
                bundle = await uow.context_bundles.resolve(
                    bundle_id=bundle_ref.bundle_id,
                    bundle_version=bundle_ref.bundle_version,
                    bundle_digest=bundle_ref.bundle_digest,
                )
                evidence = tuple(
                    item
                    for item in bundle.evidence_references
                    if item.evidence_id == citation.evidence_id
                )
                if len(evidence) != 1 or evidence[0] != citation.evidence_reference:
                    raise ValueError
                run_id = bundle.projection_snapshot.projection_run_id
                references = await uow.projections.list_source_references(run_id)
                members = await uow.projections.list_snapshot_members(
                    bundle.projection_snapshot.projection_snapshot_id
                )
                revision = await uow.sources.resolve_revision(
                    citation.evidence_reference.source_revision_id
                )
                await uow.commit()
            source_reference = citation.evidence_reference
            exact_projection = tuple(
                item
                for item in references
                if _projection_reference_matches_evidence(item, source_reference)
            )
            exact_member = tuple(
                item
                for item in members
                if item.source_object_id == source_reference.source_object_id
                and item.source_revision_id == source_reference.source_revision_id
                and item.disposition.value == "included"
            )
            if (
                len(exact_projection) != 1
                or len(exact_member) != 1
                or revision is None
                or revision.kind is not RevisionKind.CONTENT
                or revision.revision_digest != source_reference.revision_digest
                or revision.original_sha256 != source_reference.original_sha256
            ):
                raise ValueError
            decision = await self._authorization.decide_interactive_processing(
                authorization_context, (source_reference.source_object_id,)
            )
            if not decision.allowed:
                return RejectedCitation(
                    reason_code=CitationValidationReasonCode.AUTHORIZATION_DENIED,
                    validation_reference=reference_id,
                )
            generations = tuple(
                item
                for item in bundle.policy_decision_generations
                if item.source_object_id == source_reference.source_object_id
            )
            if len(generations) != 1:
                raise ValueError
            return ValidCitation(
                validation_reference=reference_id,
                facts=CitationValidationFacts(
                    citation_id=citation.citation_id,
                    context_bundle_id=bundle.bundle_id,
                    context_bundle_version=bundle.bundle_version,
                    context_bundle_digest=bundle.bundle_digest,
                    policy_decision_generations=generations,
                ),
            )
        except Exception:
            return RejectedCitation(
                reason_code=CitationValidationReasonCode.VALIDATION_UNAVAILABLE,
                validation_reference=reference_id,
                retryable=True,
            )


def _projection_reference_matches_evidence(
    reference: object, evidence: object
) -> bool:
    """Match canonical segment evidence while keeping physical object IDs opaque."""

    return (
        getattr(reference, "segment_id", None) == getattr(evidence, "evidence_id", None)
        and getattr(reference, "projected_kind", None)
        == getattr(evidence, "projected_kind", None)
        and getattr(reference, "source_object_id", None)
        == getattr(evidence, "source_object_id", None)
        and getattr(reference, "source_revision_id", None)
        == getattr(evidence, "source_revision_id", None)
        and getattr(reference, "processing_receipt_id", None)
        == getattr(evidence, "processing_receipt_id", None)
        and getattr(reference, "locator_digest", None)
        == getattr(evidence, "locator_digest", None)
    )


class _ValidationPlans:
    def __init__(
        self,
        *,
        boundary_members: _CanonicalBoundaryMembers,
        authorization: ContentAuthorizationDecisionService,
        conflict_validator: object | None = None,
        impact_validator: object | None = None,
    ) -> None:
        self._boundary_members = boundary_members
        self._authorization = authorization
        self._conflict_validator = conflict_validator
        self._impact_validator = impact_validator

    def create_plan(
        self, *, question: object, authorized_context: object, candidate: object
    ) -> AnswerQuestionValidationPlan:
        authorization = _ACTIVE_AUTHORIZATION_CONTEXT.get()
        content = _ACTIVE_EVIDENCE_CONTENT.get()
        if authorization is None or content is None:
            raise RuntimeError("authorized validation inputs are unavailable")
        answer = candidate.answer
        requests = []
        for claim in answer.claims:
            relationships = tuple(
                item for item in answer.relationships if item.claim_id == claim.claim_id
            )
            citation_ids = {
                citation_id
                for relationship in relationships
                for citation_id in relationship.citation_ids
            }
            citations = tuple(
                item for item in answer.citations if item.citation_id in citation_ids
            )
            evidence = tuple(
                SemanticSupportEvidence(
                    citation=item,
                    content=content[item.evidence_id],
                )
                for item in citations
            )
            requests.append(
                SemanticSupportRequest(
                    policy=SemanticSupportPolicy(
                        policy_id=uuid5(
                            NAMESPACE_URL,
                            f"spine:{SEMANTIC_SUPPORT_POLICY_BINDING}",
                        ),
                        policy_version=SEMANTIC_SUPPORT_POLICY_BINDING,
                        policy_digest=SEMANTIC_SUPPORT_POLICY_DIGEST,
                    ),
                    scope=question.scope,
                    correlation=question.correlation,
                    context_bundle=candidate.context_bundle,
                    claim=claim,
                    relationships=relationships,
                    evidence=evidence,
                )
            )
        return AnswerQuestionValidationPlan(
            citation_authorization_context=authorization,
            semantic_requests=tuple(requests),
            evaluation=None,
        )

    async def evaluate(
        self,
        *,
        question: object,
        authorized_context: object,
        candidate: object,
        citation_results: tuple[object, ...],
        semantic_results: tuple[object, ...],
    ) -> AnswerQuestionEvaluationLineage:
        """Build boundary-pinned facts and invoke the deterministic evaluator."""

        scope = _ACTIVE_RETRIEVAL_SCOPE.get()
        if scope is None:
            raise RuntimeError("authorized completeness scope is unavailable")
        bundle = authorized_context.bundle
        boundary = AttemptProjectionBoundary(
            workspace_id=question.scope.workspace_id,
            environment_id=question.scope.environment_id,
            run_id=question.correlation.run_id,
            attempt_id=question.correlation.attempt_id,
            projection_snapshot_id=bundle.projection_snapshot.projection_snapshot_id,
            activation_generation=bundle.activation_generation,
            canonical_boundary_id=bundle.canonical_boundary.boundary_id,
        )
        constraints = tuple(
            sorted(scope.source_constraints, key=lambda item: item.source_object_id.hex)
        )
        constraints_by_source = {item.source_object_id: item for item in constraints}
        authorization = _ACTIVE_AUTHORIZATION_CONTEXT.get()
        if authorization is None:
            raise RuntimeError("active Q&A authorization context is unavailable")
        evidence_content = _ACTIVE_EVIDENCE_CONTENT.get()
        if evidence_content is None:
            raise RuntimeError("active Q&A evidence content is unavailable")
        all_current_members = tuple(
            sorted(
                await self._boundary_members.resolve_current_members(),
                key=lambda item: item.source_object_id.hex,
            )
        )
        canonical_view_digest = hashlib.sha256(
            json.dumps(
                [
                    [
                        str(item.source_object_id),
                        str(item.current_revision_id),
                        item.current_revision_kind.value,
                        item.accepted_generation,
                        item.cas_version,
                    ]
                    for item in all_current_members
                ],
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        canonical_view_members = tuple(
            CanonicalViewMember(
                source_object_id=item.source_object_id,
                source_revision_id=item.current_revision_id,
                revision_kind=item.current_revision_kind,
                accepted_generation=item.accepted_generation,
                cas_version=item.cas_version,
            )
            for item in all_current_members
        )
        canonical_members = all_current_members
        if scope.completeness_requirement is CompletenessRequirement.BEST_EFFORT:
            constrained_source_ids = set(constraints_by_source)
            canonical_members = tuple(
                member
                for member in canonical_members
                if member.source_object_id in constrained_source_ids
            )
        target_members: list[TargetScopeMember] = []
        coverage_members: list[ObservedCoverageMember] = []
        for member in canonical_members:
            revision_kind = getattr(
                member, "current_revision_kind", getattr(member, "revision_kind", None)
            )
            source_revision_id = getattr(
                member, "current_revision_id", getattr(member, "source_revision_id", None)
            )
            if revision_kind is not RevisionKind.CONTENT:
                continue
            decision = await self._authorization.decide_interactive_processing(
                authorization, (member.source_object_id,)
            )
            if not decision.allowed:
                if decision.reason_code is AuthorizationReasonCode.AUTHORIZATION_DENIED:
                    continue
                raise RuntimeError("canonical completeness authorization is unavailable")
            if (
                decision.source_object_ids != (member.source_object_id,)
                or len(decision.policy_evidence) != 1
                or decision.policy_evidence[0].source_revision_id
                != source_revision_id
            ):
                raise RuntimeError("canonical completeness source is not current")
            target_members.append(
                TargetScopeMember(
                    source_object_id=member.source_object_id,
                    source_revision_id=source_revision_id,
                    authorization_decision_reference=decision.decision_reference,
                )
            )
            represented = constraints_by_source.get(member.source_object_id)
            status = (
                CoverageMemberStatus.PROJECTION_GAP
                if represented is None
                else CoverageMemberStatus.STALE
                if represented.source_revision_id != source_revision_id
                else CoverageMemberStatus.REPRESENTED
            )
            coverage_members.append(
                ObservedCoverageMember(
                    source_object_id=member.source_object_id,
                    source_revision_id=source_revision_id,
                    status=status,
                    proof_reference=(
                        represented.projection_snapshot_member_id
                        if represented is not None
                        and represented.source_revision_id == source_revision_id
                        else None
                    ),
                )
            )
        current_members_after_authorization = tuple(
            sorted(
                await self._boundary_members.resolve_current_members(),
                key=lambda item: item.source_object_id.hex,
            )
        )
        if current_members_after_authorization != all_current_members:
            raise RuntimeError("canonical completeness view changed during evaluation")
        revalidated_target_members = []
        for target_member in target_members:
            decision = await self._authorization.decide_interactive_processing(
                authorization, (target_member.source_object_id,)
            )
            if (
                not decision.allowed
                or decision.source_object_ids != (target_member.source_object_id,)
                or len(decision.policy_evidence) != 1
                or decision.policy_evidence[0].source_revision_id
                != target_member.source_revision_id
            ):
                raise RuntimeError(
                    "canonical completeness authorization changed during evaluation"
                )
            revalidated_target_members.append(
                target_member.model_copy(
                    update={
                        "authorization_decision_reference": decision.decision_reference
                    }
                )
            )
        target_members = revalidated_target_members
        source_constraint_digest = hashlib.sha256(
            json.dumps(
                [
                    [
                        str(item.source_object_id),
                        str(item.source_revision_id),
                        str(item.authorization_decision_reference),
                    ]
                    for item in target_members
                ],
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        target_scope_id = uuid5(
            bundle.bundle_id,
            f"target-completeness-scope:v2:{canonical_view_digest}:{source_constraint_digest}",
        )
        target_scope = TargetCompletenessScope(
            target_scope_id=target_scope_id,
            boundary=boundary,
            derivation_reference=uuid5(
                bundle.bundle_id, f"target-scope-derivation:v2:{source_constraint_digest}"
            ),
            canonical_view_reference=uuid5(
                bundle.bundle_id, f"target-canonical-view:v1:{canonical_view_digest}"
            ),
            canonical_view_digest=canonical_view_digest,
            canonical_view_generation=max(
                (item.accepted_generation for item in canonical_view_members),
                default=0,
            ),
            canonical_view_members=canonical_view_members,
            source_constraint_digest=source_constraint_digest,
            members=tuple(target_members),
        )
        observed_coverage = ObservedCoverage(
            observed_coverage_id=uuid5(target_scope_id, "observed-coverage:v2"),
            target_scope_id=target_scope_id,
            boundary=boundary,
            proof_reference=uuid5(bundle.bundle_id, "coverage-proof:v1"),
            members=tuple(coverage_members),
        )
        partial_fallback = None
        if scope.partial_answer_fallback is PartialAnswerFallback.ALLOWED:
            completeness_intent = _ACTIVE_COMPLETENESS_INTENT.get()
            if completeness_intent is None:
                raise RuntimeError("active Q&A completeness intent is unavailable")
            current_request = completeness_intent.allow_partial_answer is True
            if completeness_intent.allow_partial_answer is not False:
                partial_fallback = PartialFallbackAuthorization(
                    boundary=boundary,
                    authority=(
                        PartialFallbackAuthority.CURRENT_REQUEST
                        if current_request
                        else PartialFallbackAuthority.ACCEPTED_PREFERENCE
                    ),
                    authorization_reference=uuid5(
                        completeness_intent.command_id
                        if current_request
                        else scope.profile_ref.profile_id,
                        "partial-fallback-authorization:v2:"
                        + (
                            "current-request"
                            if current_request
                            else f"{scope.profile_ref.profile_version}:{scope.profile_ref.profile_digest}"
                        ),
                    ),
                    policy_version=(
                        "qa.ask-admission-request:v3"
                        if current_request
                        else (
                            f"context-profile:{scope.profile_ref.profile_version}:"
                            f"{scope.profile_ref.profile_digest}"
                        )
                    ),
                    applicability_reference=(
                        completeness_intent.command_id
                        if current_request
                        else scope.profile_ref.profile_id
                    ),
                )

        citations = tuple(candidate.answer.citations)
        citation_results_by_id = {
            citation.citation_id: result
            for citation, result in zip(citations, citation_results, strict=True)
        }
        citation_sources = {
            citation.citation_id: citation.evidence_reference.source_object_id
            for citation in citations
        }
        semantic_by_claim = {item.claim_id: item for item in semantic_results}
        target_by_source = {
            item.source_object_id: item for item in target_scope.members
        }
        assessments = []
        coverage_is_complete = bool(coverage_members) and all(
            item.status is CoverageMemberStatus.REPRESENTED
            for item in coverage_members
        )
        for claim in candidate.answer.claims:
            relationships = tuple(
                item
                for item in candidate.answer.relationships
                if item.claim_id == claim.claim_id
                and item.kind is not ClaimEvidenceRelationshipKind.CONTRADICTION
            )
            citation_ids = tuple(
                citation_id
                for relationship in relationships
                for citation_id in relationship.citation_ids
            )
            semantic = semantic_by_claim.get(claim.claim_id)
            validations = tuple(
                ClaimEvidenceValidation(
                    citation_id=citation_id,
                    citation_validation_reference=citation_results_by_id[
                        citation_id
                    ].validation_reference,
                    semantic_support_validation_reference=semantic.validation_record_id,
                    authorization_validation_reference=(
                        target_by_source[citation_sources[citation_id]].authorization_decision_reference
                        if citation_sources[citation_id] in target_by_source
                        else uuid5(
                            citation_results_by_id[citation_id].validation_reference,
                            "current-authorization-rejected:v1",
                        )
                    ),
                )
                for citation_id in citation_ids
            )
            citations_current = bool(validations) and all(
                isinstance(citation_results_by_id[citation_id], ValidCitation)
                for citation_id in citation_ids
            )
            supported = (
                semantic is not None
                and semantic.outcome is SemanticSupportOutcome.SUPPORTED
            )
            independently_validated = (
                supported
                and citations_current
                and bool(relationships)
                and all(
                    relationship.kind
                    is ClaimEvidenceRelationshipKind.INDEPENDENT
                    for relationship in relationships
                )
            )
            missing_coverage_impact = MissingCoverageImpact.UNKNOWN
            partial_validation_reference = (
                uuid5(
                    semantic.validation_record_id,
                    "independent-partial-claim-policy:v1",
                )
                if semantic is not None
                else uuid5(
                    bundle.bundle_id,
                    f"partial-claim-validation-unavailable:v1:{claim.claim_id}",
                )
            )
            if independently_validated and coverage_is_complete:
                missing_coverage_impact = MissingCoverageImpact.IMMATERIAL
            elif independently_validated:
                if self._impact_validator is None:
                    raise RuntimeError("missing coverage impact validator is unavailable")
                impact_validation = await self._impact_validator.assess(
                    question=question.subject.text,
                    claim=claim,
                    evidence=tuple(
                        evidence_content[citation.evidence_id]
                        for relationship in relationships
                        for citation in citations
                        if citation.citation_id in relationship.citation_ids
                    ),
                )
                missing_coverage_impact = impact_validation.impact
                partial_validation_reference = impact_validation.validation_reference
            assessments.append(
                GroundedClaimAssessment(
                    boundary=boundary,
                    claim_id=claim.claim_id,
                    grounding_status=(
                        ClaimGroundingStatus.GROUNDED
                        if supported
                        else ClaimGroundingStatus.REJECTED
                    ),
                    authorization_status=(
                        ClaimAuthorizationStatus.CURRENT
                        if citations_current
                        else ClaimAuthorizationStatus.REJECTED
                    ),
                    partial_utility=(
                        PartialClaimUtility.INDEPENDENTLY_USEFUL
                        if independently_validated
                        else PartialClaimUtility.DEPENDENT
                    ),
                    missing_coverage_impact=missing_coverage_impact,
                    evidence_validations=validations,
                    partial_validation_reference=partial_validation_reference,
                )
            )
        conflict_relationships: dict[tuple[UUID, UUID], list[UUID]] = {}
        for item in candidate.answer.relationships:
            if (
                item.kind is not ClaimEvidenceRelationshipKind.CONTRADICTION
                or item.related_claim_id is None
            ):
                continue
            pair = tuple(
                sorted((item.claim_id, item.related_claim_id), key=lambda value: value.hex)
            )
            conflict_relationships.setdefault(pair, []).append(item.relationship_id)
        assessment_by_claim = {item.claim_id: item for item in assessments}
        validated_claims = tuple(
            claim
            for claim in candidate.answer.claims
            if assessment_by_claim[claim.claim_id].grounding_status
            is ClaimGroundingStatus.GROUNDED
            and assessment_by_claim[claim.claim_id].authorization_status
            is ClaimAuthorizationStatus.CURRENT
        )
        if len(validated_claims) > 1:
            if self._conflict_validator is None:
                raise RuntimeError("grounded conflict validator is unavailable")
            evidence_by_claim = {
                claim.claim_id: tuple(
                    evidence_content[citation.evidence_id]
                    for relationship in candidate.answer.relationships
                    if relationship.claim_id == claim.claim_id
                    and relationship.kind
                    is not ClaimEvidenceRelationshipKind.CONTRADICTION
                    for citation in citations
                    if citation.citation_id in relationship.citation_ids
                )
                for claim in validated_claims
            }
            conflict_validation = await self._conflict_validator.detect(
                claims=validated_claims,
                evidence=evidence_by_claim,
            )
            grounded_conflict_pairs = {
                pair: tuple(
                    sorted(
                        conflict_relationships.get(
                            pair,
                            (
                                uuid5(
                                    conflict_validation.validation_reference,
                                    f"missing-candidate-conflict-relationship:v1:{pair[0]}:{pair[1]}",
                                ),
                            ),
                        ),
                        key=lambda value: value.hex,
                    )
                )
                for pair in conflict_validation.conflict_pairs
            }
            conflict_validation_reference = conflict_validation.validation_reference
            conflict_policy_version = conflict_validation.policy_version
        else:
            grounded_conflict_pairs = {}
            conflict_validation_reference = uuid5(
                bundle.bundle_id, "grounded-conflict-single-claim:v1"
            )
            conflict_policy_version = "qa.grounded-conflict:r1"
        conflicts = tuple(
            GroundedConflict(
                conflict_id=uuid5(
                    bundle.bundle_id, f"grounded-conflict:v1:{left}:{right}"
                ),
                boundary=boundary,
                claim_ids=(left, right),
                relationship_ids=relationship_ids,
                detection_reference=uuid5(
                    conflict_validation_reference,
                    "grounded-conflict-detection:v1:"
                    + ":".join(
                        str(value)
                        for value in (
                            left,
                            right,
                            assessment_by_claim[left].partial_validation_reference,
                            assessment_by_claim[right].partial_validation_reference,
                            *(
                                reference
                                for claim_id in (left, right)
                                for validation in assessment_by_claim[
                                    claim_id
                                ].evidence_validations
                                for reference in (
                                    validation.citation_validation_reference,
                                    validation.semantic_support_validation_reference,
                                    validation.authorization_validation_reference,
                                )
                            ),
                            *relationship_ids,
                        )
                    ),
                ),
                detection_policy_version=conflict_policy_version,
            )
            for (left, right), relationship_ids in sorted(
                grounded_conflict_pairs.items(), key=lambda item: item[0][0].hex
            )
        )
        evaluation_payload = {
            "answer": candidate.answer.model_dump(mode="json"),
            "target_scope": target_scope.model_dump(mode="json"),
            "observed_coverage": observed_coverage.model_dump(mode="json"),
            "claim_assessments": [item.model_dump(mode="json") for item in assessments],
            "partial_fallback": (
                None
                if partial_fallback is None
                else partial_fallback.model_dump(mode="json")
            ),
            "conflicts": [item.model_dump(mode="json") for item in conflicts],
        }
        evaluation_digest = hashlib.sha256(
            json.dumps(evaluation_payload, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        request = AnswerEvaluationRequest(
            answer=candidate.answer,
            target_scope=target_scope,
            observed_coverage=observed_coverage,
            claim_assessments=tuple(assessments),
            partial_fallback=partial_fallback,
            conflicts=conflicts,
            evaluation_lineage_reference=uuid5(
                bundle.bundle_id, f"answer-evaluation:v2:{evaluation_digest}"
            ),
        )
        decision = evaluate_answer_candidate(request)
        return AnswerQuestionEvaluationLineage(
            target_scope=target_scope,
            observed_coverage=observed_coverage,
            partial_fallback=partial_fallback,
            claim_assessments=tuple(assessments),
            conflicts=conflicts,
            decision=decision,
        )


class _CurrentDisclosureStateProvider(_ScopedPort):
    def __init__(self, *, authorization: ContentAuthorizationDecisionService, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._authorization = authorization

    async def resolve(self, request: object) -> DisclosureRevalidationObservation:
        active = _ACTIVE_AUTHORIZATION_CONTEXT.get()
        if active is None or active.canonical_human_id != request.requester_id:
            raise RuntimeError("current disclosure authority is unavailable")
        human_values = active.model_dump(mode="python")
        human_values.update(
            {
                "mode": AuthorizationContextMode.HUMAN_DISCLOSURE,
                "service_principal_id": None,
                "service_authority_generation": None,
                "purpose": "answer_question",
                "operation": "disclose_answer",
            }
        )
        human = TrustedAuthorizationContext.model_validate(human_values)
        dependencies = tuple(request.dependencies.dependencies)
        decision = await self._authorization.decide_human_disclosure(
            human, tuple(item.source_object_id for item in dependencies)
        )
        evidence_by_source = {item.source_object_id: item for item in decision.policy_evidence}
        observations = []
        async with self._uow_factory(self.context()) as uow:  # type: ignore[operator]
            publication = await uow.projections.resolve_active_publication("qa")
            for dependency in dependencies:
                head = await uow.canonical.resolve_current(dependency.source_object_id)
                revision = (
                    None
                    if head is None
                    else await uow.sources.resolve_revision(head.current_revision_id)
                )
                policy = await uow.access_policies.resolve_current(
                    dependency.source_object_id
                )
                auth = evidence_by_source.get(dependency.source_object_id)
                observations.append(
                    DisclosureDependencyObservation(
                        dependency_id=dependency.dependency_id,
                        source_object_id=dependency.source_object_id,
                        current_policy_version_id=(
                            None if policy is None else policy.access_policy_version_id
                        ),
                        current_policy_generation=(
                            None if policy is None else policy.policy_generation
                        ),
                        membership_generation=decision.membership_generation,
                        authority_generation=decision.authority_generation,
                        service_authority_generation=(
                            dependency.service_authority_generation
                            if decision.allowed
                            else None
                        ),
                        contributor_authorized=decision.allowed and auth is not None,
                        current_revision_id=None if revision is None else revision.revision_id,
                        current_revision_digest=(
                            None if revision is None else revision.revision_digest
                        ),
                        revision_current=(
                            revision is not None
                            and revision.revision_id == dependency.source_revision_id
                        ),
                        superseded=(
                            revision is not None
                            and revision.revision_id != dependency.source_revision_id
                        ),
                        tombstoned=(
                            head is not None
                            and head.current_revision_kind is not RevisionKind.CONTENT
                        ),
                        retention_eligible=(
                            head is not None
                            and head.current_revision_kind is RevisionKind.CONTENT
                        ),
                    )
                )
            await uow.commit()
        first = dependencies[0]
        current = publication if isinstance(publication, PublishedActivePublication) else None
        exact = current is not None and (
            current.projection_snapshot.projection_snapshot_id
            == first.projection_snapshot_id
            and current.activation_generation == first.activation_generation
            and current.canonical_boundary_id == first.canonical_boundary_id
        )
        return DisclosureRevalidationObservation(
            workspace_id=request.workspace_id,
            environment_id=request.environment_id,
            requester_authorized=decision.allowed,
            publication=DisclosurePublicationObservation(
                pinned_snapshot_id=first.projection_snapshot_id,
                pinned_activation_generation=first.activation_generation,
                pinned_boundary_id=first.canonical_boundary_id,
                pinned_as_of_boundary_id=first.canonical_as_of_boundary_id,
                pinned_as_of_generation=first.canonical_as_of_generation,
                current_snapshot_id=(
                    None
                    if current is None
                    else current.projection_snapshot.projection_snapshot_id
                ),
                current_activation_generation=(
                    None if current is None else current.activation_generation
                ),
                current_boundary_id=(
                    None if current is None else current.canonical_boundary_id
                ),
                current_as_of_boundary_id=(
                    None if current is None else current.canonical_boundary_id
                ),
                current_as_of_generation=(
                    None
                    if current is None
                    else current.accepted_through_generation
                ),
                impact=(
                    DisclosurePublicationImpact.NONE
                    if exact
                    else DisclosurePublicationImpact.MATERIAL
                ),
            ),
            dependencies=tuple(observations),
        )


class _UnusedPort:
    def __getattr__(self, name: str):
        async def unavailable(*args: object, **kwargs: object) -> object:
            del args, kwargs
            raise RuntimeError(f"Q&A port is unavailable: {name}")

        return unavailable


class _ScopedRepositoryProxy:
    def __init__(
        self,
        *,
        uow_factory: object,
        repository_name: str,
        boundary: TrustedContextBoundary,
        service_principal_id: UUID,
    ) -> None:
        self._uow_factory = uow_factory
        self._repository_name = repository_name
        self._boundary = boundary
        self._service_principal_id = service_principal_id

    def __getattr__(self, method_name: str):
        async def invoke(*args: object, **kwargs: object) -> object:
            owned = args[0] if args else None
            workspace_id = kwargs.get("workspace_id") or getattr(owned, "workspace_id", None)
            environment_id = kwargs.get("environment_id") or getattr(owned, "environment_id", None)
            if not isinstance(workspace_id, UUID) or not isinstance(environment_id, UUID):
                raise RuntimeError("scoped Q&A repository identity is unavailable")
            trace_id = (
                getattr(owned, "request_id", None)
                or getattr(owned, "run_id", None)
                or uuid5(workspace_id, f"{environment_id}:{self._repository_name}:{method_name}")
            )
            context = self._boundary.worker(
                scope=EnvironmentScope(
                    workspace_id=workspace_id,
                    environment_id=environment_id,
                ),
                service_principal_id=self._service_principal_id,
                purpose=PersistencePurpose("governed_qa.capability.v1"),
                operation=PersistenceOperation(self._repository_name),
                trace_id=trace_id,
            )
            async with self._uow_factory(context) as uow:  # type: ignore[operator]
                repository = getattr(uow, self._repository_name)
                result = await getattr(repository, method_name)(*args, **kwargs)
                await uow.commit()
                return result

        return invoke


class _ScopedContextBundleRepository:
    """Adapt the canonical tenant repository to the broker/downstream ports."""

    def __init__(
        self,
        *,
        uow_factory: object,
        boundary: TrustedContextBoundary,
        service_principal_id: UUID,
    ) -> None:
        self._uow_factory = uow_factory
        self._boundary = boundary
        self._service_principal_id = service_principal_id

    def _context(self, workspace_id: UUID, environment_id: UUID, trace_id: UUID):
        return _context_broker_persistence_context(
            boundary=self._boundary,
            workspace_id=workspace_id,
            environment_id=environment_id,
            service_principal_id=self._service_principal_id,
            trace_id=trace_id,
        )

    async def resolve_attempt(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        run_id: UUID,
        attempt_id: UUID,
        idempotency_key: str,
    ) -> object | None:
        context = self._context(workspace_id, environment_id, run_id)
        async with self._uow_factory(context) as uow:  # type: ignore[operator]
            try:
                result = await uow.context_bundles.resolve_attempt(
                    run_id=run_id,
                    attempt_id=attempt_id,
                    idempotency_key=idempotency_key,
                )
            except ContextBundleNotFoundError:
                return None
            await uow.commit()
            return result

    async def create_or_replay(self, bundle: object) -> object:
        context = self._context(
            bundle.workspace_id, bundle.environment_id, bundle.run_id
        )
        try:
            async with self._uow_factory(context) as uow:  # type: ignore[operator]
                result = await uow.context_bundles.create(bundle)
                await uow.commit()
                return result
        except Exception as error:
            _LOGGER.warning(
                "governed Q&A context bundle persistence unavailable: %s",
                type(error).__name__,
            )
            raise

    async def resolve(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        reference: object,
    ) -> object:
        context = self._context(workspace_id, environment_id, reference.bundle_id)
        async with self._uow_factory(context) as uow:  # type: ignore[operator]
            result = await uow.context_bundles.resolve(
                bundle_id=reference.bundle_id,
                bundle_version=reference.bundle_version,
                bundle_digest=reference.bundle_digest,
            )
            await uow.commit()
            return result

    async def load(self, **kwargs: object) -> object:
        return await self.resolve(**kwargs)  # type: ignore[arg-type]


class _ContextReferences:
    def __init__(self, registry: _TrustedContextRegistry) -> None:
        self._registry = registry

    def for_command(self, command: object) -> AnswerQuestionContextReferences:
        run_id = getattr(command, "run_id", None)
        if not isinstance(run_id, UUID):
            raise ValueError("admitted run identity is unavailable")
        references = AnswerQuestionContextReferences(
            trusted_request_context_ref=uuid5(run_id, "trusted-request-context"),
            authorization_context_ref=uuid5(run_id, "authorization-context"),
        )
        self._registry.register(references.authorization_context_ref, command)
        return references


class _UnavailableArtifacts:
    def __getattr__(self, name: str):
        async def unavailable(*args: object, **kwargs: object) -> object:
            del args, kwargs
            raise RuntimeError(f"Q&A artifact persistence is unavailable: {name}")

        return unavailable


class _UnavailableCanceller:
    async def cancel(self, command: object) -> object:
        del command
        raise RuntimeError("Q&A cancellation runtime is unavailable")


@dataclass(frozen=True, slots=True)
class ProductionQACapabilityGraph:
    capability: ProductionAnswerQuestionCapability
    execution: AnswerQuestionExecutionBoundary
    artifacts: Any
    configuration: Any
    qualified_context_broker: OneShotQualifiedContextBroker


async def build_production_qa_capability_graph(
    *,
    session_factory: object,
    persistence: object,
    boundary: TrustedContextBoundary,
    service_principal_id: UUID,
    llm_api_key: str,
    llm_model: str,
    llm_endpoint: str,
) -> ProductionQACapabilityGraph:
    """Build the exact production types used by durable Ask execution."""

    registry = HandlerRegistry()
    registry.register_handler(
        registration=AgentImplementationRegistration.from_version(
            version=_AGENT_VERSION,
            capability=_CAPABILITY,
            implementation_key="governed_qa.configured_provider",
        ),
        handler=_NeverDirectHandler(),
    )
    registry.finalize()
    configuration = _ScopedConfigurationCatalog(
        session_factory=session_factory,
        uow_factory=persistence.uow_factory,
        boundary=boundary,
        service_principal_id=service_principal_id,
    )
    artifacts = PostgreSQLQAArtifactStore(
        session_factory, service_principal_id=service_principal_id
    )
    execution = AnswerQuestionExecutionBoundary(
        capability=_CAPABILITY,
        binding_resolver=AgentBindingResolver(
            catalog=_ScopedAgentCatalog(),
            registry=registry,
            approved_deployment_policy=DeploymentSelectionPolicy(
                frozenset({DeploymentStage.PRODUCTION})
            ),
        ),
        configuration_catalog=configuration,
        runtime_provider=ConfiguredLLMRuntimeProvider(
            artifacts=artifacts,
            api_key=llm_api_key,
            model=llm_model,
            endpoint=llm_endpoint,
            authority_generation_resolver=_CurrentQAServiceAuthority(
                session_factory, service_principal_id
            ),
            admitted_authority_generation=_admitted_authority_generation,
        ),
    )
    uow_factory = persistence.uow_factory
    bundle_repository = _ScopedContextBundleRepository(
        uow_factory=uow_factory,
        boundary=boundary,
        service_principal_id=service_principal_id,
    )
    disclosure_repository = _ScopedRepositoryProxy(
        uow_factory=uow_factory,
        repository_name="answer_disclosures",
        boundary=boundary,
        service_principal_id=service_principal_id,
    )
    disclosure_validation_repository = _ScopedRepositoryProxy(
        uow_factory=uow_factory,
        repository_name="disclosure_validations",
        boundary=boundary,
        service_principal_id=service_principal_id,
    )
    trusted_contexts = _TrustedContextRegistry(service_principal_id)
    qualification_authority = PostgreSQLQualificationAuthorityResolver(
        session_factory  # type: ignore[arg-type]
    )
    port_values = {
        "uow_factory": uow_factory,
        "boundary": boundary,
        "service_principal_id": service_principal_id,
    }
    content_authorization = ContentAuthorizationDecisionService(
        repository=_CurrentContentAuthorization(
            **port_values
        ),
        identity_authority=_CurrentIdentityAuthority(session_factory),
        noninteractive_authority=_CurrentNoninteractiveAuthority(
            qualification_authority,
            service_principal_id,
            trusted_contexts,
        ),
    )
    snapshot_members = _SnapshotMembers(**port_values)
    scope_resolver = AuthorizedRetrievalScopeResolver(
        authorization=content_authorization,
        snapshot_members=snapshot_members,  # type: ignore[arg-type]
        provider_scope_safety=_CanonicalSourceIsolation(),
    )
    profile_resolver = ServerApprovedContextProfileResolver(
        repository=_ProfileRepository(**port_values),
        capability_policy=_AllowContextRetrieval(),
    )
    plan_builder = PhysicalReadPlanBuilder(
        namespace_registry=_CanonicalNamespaceRegistry()  # type: ignore[arg-type]
    )
    retrieval = PhysicalReadPlanExecutor(
        _ParsedProjectionRetrieval(session_factory)
    )
    broker = AuthorizedContextBrokerApplicationService(
        trusted_context=trusted_contexts,
        profile_resolver=profile_resolver,
        publication=_ActivePublication(**port_values),
        scope_resolver=scope_resolver,
        read_model=_ContextReadModel(**port_values),
        plan_builder=plan_builder,
        retrieval=retrieval,
        evidence_verifier=_CanonicalEvidenceVerifier(
            **port_values,
            authorization=content_authorization,
        ),
        bundles=bundle_repository,  # type: ignore[arg-type]
    )
    qualified_context_broker = OneShotQualifiedContextBroker(
        broker=broker,
        trusted_contexts=trusted_contexts,
        authority_resolver=qualification_authority,
        service_principal_id=service_principal_id,
    )
    revalidator = _CurrentBundleRevalidator(**port_values)
    disclosure_revalidator = DisclosureRevalidationService(
        state_provider=_CurrentDisclosureStateProvider(
            **port_values,
            authorization=content_authorization,
        ),
        result_repository=disclosure_validation_repository,  # type: ignore[arg-type]
    )
    context = AnswerQuestionContextService(
        broker=broker,
        bundles=bundle_repository,  # type: ignore[arg-type]
        revalidator=revalidator,
        disclosure_revalidator=disclosure_revalidator,
    )
    capability = ProductionAnswerQuestionCapability(
        context=context,
        execution=execution,
        context_references=_ContextReferences(trusted_contexts),
        disclosure_gate=AnswerDisclosureGate(
            repository=disclosure_repository,  # type: ignore[arg-type]
            accepted_output=InMemoryAcceptedAnswerOutputPort(),
        ),
        candidate_runner=PersistedAnswerQuestionCandidateRunner(
            input_artifacts=artifacts,
            artifacts=artifacts,
            canceller=_UnavailableCanceller(),
        ),
        validator=ExistingAnswerQuestionCandidateValidator(
            citations=_CurrentCitationValidator(  # type: ignore[arg-type]
                **port_values,
                authorization=content_authorization,
            ),
            semantic_support=SemanticSupportValidationService(
                validator=ConfiguredSemanticSupportValidator(
                    api_key=llm_api_key,
                    model=llm_model,
                    endpoint=llm_endpoint,
                )
            ),
            plans=_ValidationPlans(
                boundary_members=_CanonicalBoundaryMembers(
                    **port_values,
                ),
                authorization=content_authorization,
                conflict_validator=ConfiguredGroundedConflictValidator(
                    api_key=llm_api_key,
                    model=llm_model,
                    endpoint=llm_endpoint,
                ),
                impact_validator=ConfiguredMissingCoverageImpactValidator(
                    api_key=llm_api_key,
                    model=llm_model,
                    endpoint=llm_endpoint,
                ),
            ),
        ),
    )
    return ProductionQACapabilityGraph(
        capability=capability,
        execution=execution,
        artifacts=artifacts,
        configuration=configuration,
        qualified_context_broker=qualified_context_broker,
    )


__all__ = [
    "OneShotQualifiedContextBroker",
    "ProductionQACapabilityGraph",
    "build_production_qa_capability_graph",
]
