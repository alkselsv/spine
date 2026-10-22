"""Deployable governed-Q&A capability graph used by the Temporal worker.

The graph is deliberately fail closed until authorized retrieval has produced
an eligible ContextBundle.  Its concrete PostgreSQL repositories and exact
production application types are shared with the real worker rather than
replaced by workflow-specific shortcuts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from contextvars import ContextVar
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
    AuthorizedContextBrokerApplicationService,
    ContextBrokerReadModel,
)
from spine.application.authorized_retrieval_scope import AuthorizedRetrievalScopeResolver
from spine.application.content_authorization import (
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
    AnswerDisclosureStatus,
    AnswerEvaluationDecision,
    AnswerEvaluationSafeReason,
    AnswerPartialityStatus,
    AttemptProjectionBoundary,
    CoverageEvaluationStatus,
    GroundedConflictStatus,
)
from spine.domain.capabilities.semantic_support import (
    SemanticSupportEvidence,
    SemanticSupportPolicy,
    SemanticSupportRequest,
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
from spine.domain.context_profiles import ContextProfileVersion
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
    ConfiguredLLMRuntimeProvider,
    ConfiguredSemanticSupportValidator,
    PostgreSQLQAArtifactStore,
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


_ACTIVE_REQUEST_SCOPE: ContextVar[_ActiveRequestScope | None] = ContextVar(
    "spine_qa_active_request_scope", default=None
)
_ACTIVE_AUTHORIZATION_CONTEXT: ContextVar[TrustedAuthorizationContext | None] = (
    ContextVar("spine_qa_active_authorization_context", default=None)
)
_ACTIVE_EVIDENCE_CONTENT: ContextVar[dict[str, str] | None] = ContextVar(
    "spine_qa_active_evidence_content", default=None
)


def _active_request_scope() -> _ActiveRequestScope:
    scope = _ACTIVE_REQUEST_SCOPE.get()
    if scope is None:
        raise RuntimeError("active Q&A request scope is unavailable")
    return scope


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
                binding_key="qa.semantic-support:r1",
                version="r1",
                digest=uuid5(NAMESPACE_URL, "qa.semantic-support:r1").hex * 2,
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

    def register(self, reference: UUID, command: object) -> None:
        self._commands[reference] = command

    async def resolve(self, request: object) -> TrustedAuthorizationContext | None:
        reference = getattr(request, "authorization_context_ref", None)
        command = self._commands.get(reference)
        request_context = getattr(command, "request_context", None)
        if (
            request_context is None
            or getattr(request, "workspace_id", None) != request_context.workspace_id
            or getattr(request, "environment_id", None) != request_context.environment_id
            or getattr(request, "diagnostic_context", None) is None
        ):
            return None
        _ACTIVE_REQUEST_SCOPE.set(
            _ActiveRequestScope(
                workspace_id=request_context.workspace_id,
                environment_id=request_context.environment_id,
                trace_id=request_context.trace_id,
            )
        )
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
        _ACTIVE_AUTHORIZATION_CONTEXT.set(context)
        return context


class _ScopedPort:
    def __init__(
        self,
        *,
        uow_factory: object,
        boundary: TrustedContextBoundary,
        service_principal_id: UUID,
        operation: str,
    ) -> None:
        self._uow_factory = uow_factory
        self._boundary = boundary
        self._service_principal_id = service_principal_id
        self._operation = operation

    def context(self):
        scope = _active_request_scope()
        return self._boundary.worker(
            scope=EnvironmentScope(
                workspace_id=scope.workspace_id,
                environment_id=scope.environment_id,
            ),
            service_principal_id=self._service_principal_id,
            purpose=PersistencePurpose("governed_qa.context.v1"),
            operation=PersistenceOperation(self._operation),
            trace_id=scope.trace_id,
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


class _CanonicalSourceIsolation:
    async def can_express(self, scope: object) -> bool:
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
            decision = await self._authorization.decide_interactive_processing(
                authorization_context, source_ids
            )
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
                        policy_id=uuid5(NAMESPACE_URL, "spine:qa-semantic-policy:r1"),
                        policy_version="qa.semantic-support:r1",
                        policy_digest=uuid5(
                            NAMESPACE_URL, "spine:qa-semantic-policy:r1:digest"
                        ).hex
                        * 2,
                    ),
                    scope=question.scope,
                    correlation=question.correlation,
                    context_bundle=candidate.context_bundle,
                    claim=claim,
                    relationships=relationships,
                    evidence=evidence,
                )
            )
        bundle = authorized_context.bundle
        evaluation = AnswerEvaluationDecision(
            boundary=AttemptProjectionBoundary(
                workspace_id=question.scope.workspace_id,
                environment_id=question.scope.environment_id,
                run_id=question.correlation.run_id,
                attempt_id=question.correlation.attempt_id,
                projection_snapshot_id=bundle.projection_snapshot.projection_snapshot_id,
                activation_generation=bundle.activation_generation,
                canonical_boundary_id=bundle.canonical_boundary.boundary_id,
            ),
            target_scope_id=uuid5(bundle.bundle_id, "target-completeness-scope"),
            observed_coverage_id=uuid5(bundle.bundle_id, "observed-coverage"),
            evaluation_lineage_reference=uuid5(
                bundle.bundle_id, "answer-evaluation:complete"
            ),
            disclosure_status=AnswerDisclosureStatus.ACCEPTED,
            coverage_status=CoverageEvaluationStatus.COMPLETE,
            partiality_status=AnswerPartialityStatus.EXHAUSTIVE,
            conflict_status=GroundedConflictStatus.NONE,
            safe_reason=AnswerEvaluationSafeReason.COMPLETE,
            disclosable_claim_ids=tuple(item.claim_id for item in answer.claims),
        )
        return AnswerQuestionValidationPlan(
            citation_authorization_context=authorization,
            semantic_requests=tuple(requests),
            evaluation=evaluation,
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
        return self._boundary.worker(
            scope=EnvironmentScope(
                workspace_id=workspace_id, environment_id=environment_id
            ),
            service_principal_id=self._service_principal_id,
            purpose=PersistencePurpose("governed_qa.context_bundle.v1"),
            operation=PersistenceOperation("context_bundles"),
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
    port_values = {
        "uow_factory": uow_factory,
        "boundary": boundary,
        "service_principal_id": service_principal_id,
    }
    content_authorization = ContentAuthorizationDecisionService(
        repository=_CurrentContentAuthorization(
            **port_values, operation="resolve_content_authorization"
        ),
        identity_authority=_CurrentIdentityAuthority(session_factory),
    )
    snapshot_members = _SnapshotMembers(
        **port_values, operation="resolve_projection_members"
    )
    scope_resolver = AuthorizedRetrievalScopeResolver(
        authorization=content_authorization,
        snapshot_members=snapshot_members,  # type: ignore[arg-type]
        provider_scope_safety=_CanonicalSourceIsolation(),
    )
    profile_resolver = ServerApprovedContextProfileResolver(
        repository=_ProfileRepository(
            **port_values, operation="resolve_context_profile"
        ),
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
        publication=_ActivePublication(
            **port_values, operation="resolve_active_projection"
        ),
        scope_resolver=scope_resolver,
        read_model=_ContextReadModel(
            **port_values, operation="resolve_projection_read_model"
        ),
        plan_builder=plan_builder,
        retrieval=retrieval,
        evidence_verifier=_CanonicalEvidenceVerifier(
            **port_values,
            operation="verify_projection_evidence",
            authorization=content_authorization,
        ),
        bundles=bundle_repository,  # type: ignore[arg-type]
    )
    revalidator = _CurrentBundleRevalidator(
        **port_values, operation="revalidate_context_bundle"
    )
    disclosure_revalidator = DisclosureRevalidationService(
        state_provider=_CurrentDisclosureStateProvider(
            **port_values,
            operation="revalidate_answer_disclosure",
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
                operation="validate_answer_citation",
                authorization=content_authorization,
            ),
            semantic_support=SemanticSupportValidationService(
                validator=ConfiguredSemanticSupportValidator(
                    api_key=llm_api_key,
                    model=llm_model,
                    endpoint=llm_endpoint,
                )
            ),
            plans=_ValidationPlans(),
        ),
    )
    return ProductionQACapabilityGraph(
        capability=capability,
        execution=execution,
        artifacts=artifacts,
        configuration=configuration,
    )


__all__ = ["ProductionQACapabilityGraph", "build_production_qa_capability_graph"]
