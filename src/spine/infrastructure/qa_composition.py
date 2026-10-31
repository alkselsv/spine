"""Production persistence adapters for the governed Q&A HTTP surface.

The application session service intentionally depends on a repository rather
than a unit-of-work factory.  This adapter keeps that repository scoped to the
already authorized request context and commits every mutation at the same
boundary.  No workspace or environment selector is accepted separately from
the trusted context.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from spine.application.persistence.idempotency import IdempotencyKey
from spine.application.persistence.unit_of_work import UnitOfWorkFactory
from spine.application.feedback import (
    FeedbackRequester,
    FeedbackSubmissionResult,
    FeedbackTargetReference,
    PersistedFeedbackService,
)
from spine.application.qa_sessions import (
    PersistedQASessionService,
    QAMessagePage,
    QAMessagePosition,
    QAMessageRead,
    QAMutationResult,
    QASessionPage,
    QASessionPosition,
    QASessionRequester,
)
from spine.application.disclosure_revalidation import (
    DisclosureRevalidationService,
    RevalidatingQAMessageDisclosure,
)
from spine.application.citation_inspector import (
    CitationInspectorRequester,
    ProtectedCitationInspector,
)
from spine.application.persistence.context import (
    EnvironmentScope,
    TrustedPersistenceContext,
)
from spine.domain.capabilities import (
    AnswerDisclosureRecord,
    AnswerQuestionOutcome,
    Citation,
)
from spine.domain.disclosure import (
    CitationInspectionMode,
    CitationInspectionResult,
    DisclosureDependencyObservation,
    DisclosureDependencySet,
    DisclosurePublicationImpact,
    DisclosurePublicationObservation,
    DisclosureRevalidationObservation,
)
from spine.domain.feedback import FeedbackOutcome, FeedbackTargetKind
from spine.domain.projections import PublishedActivePublication
from spine.domain.qa_sessions import QAMessage, QASession
from spine.domain.sources import RevisionKind
from spine.infrastructure.disclosure_constraints import (
    resolve_final_disclosure_constraints,
)
from spine.infrastructure.persistence.parsed_artifacts import (
    ParsedArtifactCitationExcerptResolver,
    PostgreSQLParsedArtifactRepository,
)


@dataclass(frozen=True, slots=True)
class _CurrentSessionOwnerDisclosure:
    """Release question/safe state only to the session's current owner.

    Accepted answer content remains fail-closed until the production disclosure
    revalidator is composed.  This still permits session navigation, admission,
    abstention, denial and cancellation without turning an administrator role
    into content authority.
    """

    repository: object
    accepted: RevalidatingQAMessageDisclosure | None = None

    async def is_eligible(self, *, message: QAMessage, requester: QASessionRequester) -> bool:
        session = await self.repository.resolve_session(  # type: ignore[attr-defined]
            workspace_id=message.workspace_id,
            environment_id=message.environment_id,
            session_id=message.session_id,
        )
        return session is not None and session.subject_id == requester.subject_id

    async def is_accepted_eligible(
        self,
        *,
        message: QAMessage,
        requester: QASessionRequester,
        record: AnswerDisclosureRecord,
    ) -> bool:
        if not await self.is_eligible(message=message, requester=requester):
            return False
        if self.accepted is None:
            return False
        return await self.accepted.is_accepted_eligible(
            message=message,
            requester=requester,
            record=record,
        )


@dataclass(frozen=True, slots=True)
class _CurrentSessionDisclosureState:
    """Resolve current human authority and exact publication lineage in one UoW."""

    uow: object
    requester: QASessionRequester

    async def resolve(self, request: object) -> DisclosureRevalidationObservation:
        if request.requester_id != self.requester.subject_id:
            raise RuntimeError("Q&A disclosure requester changed")
        dependencies = tuple(request.dependencies.dependencies)
        artifact_id = request.message_id or request.attempt_id or request.run_id
        if artifact_id is None:
            raise RuntimeError("disclosure artifact identity is unavailable")
        observations = []
        requester_authorized = True
        for dependency in dependencies:
            head = await self.uow.canonical.resolve_current(dependency.source_object_id)  # type: ignore[attr-defined]
            current_policy = await self.uow.access_policies.resolve_current(  # type: ignore[attr-defined]
                dependency.source_object_id
            )
            policy = (
                None
                if current_policy is None
                else await self.uow.access_policies.resolve_policy(  # type: ignore[attr-defined]
                    current_policy.access_policy_version_id
                )
            )
            revision = (
                None
                if head is None
                else await self.uow.sources.resolve_revision(head.current_revision_id)  # type: ignore[attr-defined]
            )
            contributor_authorized = bool(
                policy is not None
                and policy.allows_human_read(
                    self.requester.subject_id,
                    frozenset(),
                )
            )
            lifecycle_eligible, active_hold = await resolve_final_disclosure_constraints(
                self.uow,
                dependency,
                artifact_id=artifact_id,
                evaluated_at=datetime.now(timezone.utc),
            )
            requester_authorized = requester_authorized and contributor_authorized
            observations.append(
                DisclosureDependencyObservation(
                    dependency_id=dependency.dependency_id,
                    source_object_id=dependency.source_object_id,
                    current_policy_version_id=(
                        None if current_policy is None else current_policy.access_policy_version_id
                    ),
                    current_policy_generation=(None if current_policy is None else current_policy.policy_generation),
                    membership_generation=self.requester.authorization_generation,
                    authority_generation=self.requester.authorization_generation,
                    service_authority_generation=(
                        dependency.service_authority_generation if contributor_authorized else None
                    ),
                    contributor_authorized=contributor_authorized,
                    current_revision_id=(None if revision is None else revision.revision_id),
                    current_revision_digest=(None if revision is None else revision.revision_digest),
                    revision_current=bool(
                        revision is not None and revision.revision_id == dependency.source_revision_id
                    ),
                    superseded=bool(revision is not None and revision.revision_id != dependency.source_revision_id),
                    tombstoned=bool(head is not None and head.current_revision_kind is not RevisionKind.CONTENT),
                    retention_eligible=bool(head is not None and head.current_revision_kind is RevisionKind.CONTENT),
                    lifecycle_disclosure_eligible=lifecycle_eligible,
                    active_disclosure_hold=active_hold,
                )
            )
        publication = await self.uow.projections.resolve_active_publication("qa")  # type: ignore[attr-defined]
        current = publication if isinstance(publication, PublishedActivePublication) else None
        first = dependencies[0]
        exact = current is not None and (
            current.projection_snapshot.projection_snapshot_id == first.projection_snapshot_id
            and current.activation_generation == first.activation_generation
            and current.canonical_boundary_id == first.canonical_boundary_id
        )
        return DisclosureRevalidationObservation(
            workspace_id=request.workspace_id,
            environment_id=request.environment_id,
            requester_authorized=requester_authorized,
            publication=DisclosurePublicationObservation(
                pinned_snapshot_id=first.projection_snapshot_id,
                pinned_activation_generation=first.activation_generation,
                pinned_boundary_id=first.canonical_boundary_id,
                pinned_as_of_boundary_id=first.canonical_as_of_boundary_id,
                pinned_as_of_generation=first.canonical_as_of_generation,
                current_snapshot_id=(None if current is None else current.projection_snapshot.projection_snapshot_id),
                current_activation_generation=(None if current is None else current.activation_generation),
                current_boundary_id=(None if current is None else current.canonical_boundary_id),
                current_as_of_boundary_id=(None if current is None else current.canonical_boundary_id),
                current_as_of_generation=(None if current is None else current.accepted_through_generation),
                impact=(DisclosurePublicationImpact.NONE if exact else DisclosurePublicationImpact.MATERIAL),
            ),
            dependencies=tuple(observations),
        )


def current_disclosure_revalidator(uow: object, requester: object) -> DisclosureRevalidationService:
    """Bind fresh disclosure checks to the active scoped unit of work."""

    return DisclosureRevalidationService(
        state_provider=_CurrentSessionDisclosureState(
            uow=uow,
            requester=requester,  # type: ignore[arg-type]
        ),
        result_repository=uow.disclosure_validations,  # type: ignore[attr-defined]
    )


@dataclass(frozen=True, slots=True)
class _CurrentTargetScopeAuthorizer:
    uow: object
    requester: object

    async def allows(self, target_scope: object) -> bool:
        members = getattr(target_scope, "members", None)
        requester_id = getattr(self.requester, "subject_id", None)
        if members is None or requester_id is None:
            return False
        for member in members:
            head = await self.uow.canonical.resolve_current(member.source_object_id)  # type: ignore[attr-defined]
            current_policy = await self.uow.access_policies.resolve_current(  # type: ignore[attr-defined]
                member.source_object_id
            )
            policy = (
                None
                if current_policy is None
                else await self.uow.access_policies.resolve_policy(  # type: ignore[attr-defined]
                    current_policy.access_policy_version_id
                )
            )
            if (
                head is None
                or head.current_revision_id != member.source_revision_id
                or head.current_revision_kind is not RevisionKind.CONTENT
                or policy is None
                or not policy.allows_human_read(requester_id, frozenset())
            ):
                return False
        return True


def current_target_scope_authorizer(uow: object, requester: object) -> _CurrentTargetScopeAuthorizer:
    """Bind target-scope authorization to the active scoped unit of work."""

    return _CurrentTargetScopeAuthorizer(uow=uow, requester=requester)


@dataclass(frozen=True, slots=True)
class PostgreSQLQASessionService:
    """Run the existing session application service in one scoped UoW."""

    uow_factory: UnitOfWorkFactory

    @staticmethod
    def _service(
        uow: object,
        requester: QASessionRequester | None = None,
    ) -> PersistedQASessionService:
        repository = uow.qa_sessions  # type: ignore[attr-defined]
        accepted = None
        if requester is not None:
            accepted = RevalidatingQAMessageDisclosure(current_disclosure_revalidator(uow, requester))
        return PersistedQASessionService(
            repository=repository,
            disclosure_eligibility=_CurrentSessionOwnerDisclosure(
                repository,
                accepted,
            ),
            answer_question_runs=uow.answer_question_runs,  # type: ignore[attr-defined]
            answer_disclosures=uow.answer_disclosures,  # type: ignore[attr-defined]
        )

    async def create_session(
        self,
        *,
        context: TrustedPersistenceContext,
        requester: QASessionRequester,
        idempotency_key: IdempotencyKey,
    ) -> QAMutationResult[QASession]:
        async with self.uow_factory(context) as uow:
            result = await self._service(uow).create_session(
                context=context,
                requester=requester,
                idempotency_key=idempotency_key,
            )
            await uow.commit()
            return result

    async def list_sessions(
        self,
        *,
        context: TrustedPersistenceContext,
        limit: int,
        after: QASessionPosition | None,
    ) -> QASessionPage[QASession]:
        async with self.uow_factory(context) as uow:
            result = await self._service(uow).list_sessions(context=context, limit=limit, after=after)
            await uow.commit()
            return result

    async def get_session(self, *, context: TrustedPersistenceContext, session_id: UUID) -> QASession | None:
        async with self.uow_factory(context) as uow:
            result = await self._service(uow).get_session(context=context, session_id=session_id)
            await uow.commit()
            return result

    async def restore_session(
        self,
        *,
        context: TrustedPersistenceContext,
        session_id: UUID,
        expected_version: int,
        idempotency_key: IdempotencyKey,
    ) -> QAMutationResult[QASession]:
        async with self.uow_factory(context) as uow:
            result = await self._service(uow).restore_session(
                context=context,
                session_id=session_id,
                expected_version=expected_version,
                idempotency_key=idempotency_key,
            )
            await uow.commit()
            return result

    async def create_pending_message(
        self,
        *,
        context: TrustedPersistenceContext,
        requester: QASessionRequester,
        session_id: UUID,
        question: str,
        run_id: UUID | None,
        idempotency_key: IdempotencyKey,
    ) -> QAMutationResult[QAMessage]:
        async with self.uow_factory(context) as uow:
            result = await self._service(uow).create_pending_message(
                context=context,
                requester=requester,
                session_id=session_id,
                question=question,
                run_id=run_id,
                idempotency_key=idempotency_key,
            )
            await uow.commit()
            return result

    async def list_messages(
        self,
        *,
        context: TrustedPersistenceContext,
        requester: QASessionRequester,
        session_id: UUID,
        limit: int,
        after: QAMessagePosition | None,
    ) -> QAMessagePage[QAMessageRead] | None:
        async with self.uow_factory(context) as uow:
            result = await self._service(uow, requester).list_messages(
                context=context,
                requester=requester,
                session_id=session_id,
                limit=limit,
                after=after,
            )
            await uow.commit()
            return result

    async def get_message(
        self,
        *,
        context: TrustedPersistenceContext,
        requester: QASessionRequester,
        session_id: UUID,
        message_id: UUID,
    ) -> QAMessageRead | None:
        async with self.uow_factory(context) as uow:
            result = await self._service(uow, requester).get_message(
                context=context,
                requester=requester,
                session_id=session_id,
                message_id=message_id,
            )
            await uow.commit()
            return result

    async def link_message_to_run(
        self,
        *,
        context: TrustedPersistenceContext,
        session_id: UUID,
        message_id: UUID,
        run_id: UUID,
        idempotency_key: IdempotencyKey,
    ) -> QAMutationResult[QAMessage]:
        async with self.uow_factory(context) as uow:
            result = await self._service(uow).link_message_to_run(
                context=context,
                session_id=session_id,
                message_id=message_id,
                run_id=run_id,
                idempotency_key=idempotency_key,
            )
            await uow.commit()
            return result

    async def complete_message(
        self,
        *,
        context: TrustedPersistenceContext,
        session_id: UUID,
        message_id: UUID,
        answer: str | None,
        outcome: AnswerQuestionOutcome,
        cancelled: bool,
        idempotency_key: IdempotencyKey,
    ) -> QAMutationResult[QAMessage]:
        async with self.uow_factory(context) as uow:
            result = await self._service(uow).complete_message(
                context=context,
                session_id=session_id,
                message_id=message_id,
                answer=answer,
                outcome=outcome,
                cancelled=cancelled,
                idempotency_key=idempotency_key,
            )
            await uow.commit()
            return result

    async def complete_message_in_transaction(
        self,
        *,
        repository: object,
        context: TrustedPersistenceContext,
        session_id: UUID,
        message_id: UUID,
        answer: str | None,
        outcome: AnswerQuestionOutcome,
        cancelled: bool,
        idempotency_key: IdempotencyKey,
        disclosure_dependencies: DisclosureDependencySet | None = None,
    ) -> QAMutationResult[QAMessage]:
        """Join canonical run and message terminalization in the caller's UoW."""

        service = PersistedQASessionService(
            repository=repository,  # type: ignore[arg-type]
            disclosure_eligibility=_CurrentSessionOwnerDisclosure(repository),
        )
        return await service.complete_message_in_transaction(
            repository=repository,  # type: ignore[arg-type]
            context=context,
            session_id=session_id,
            message_id=message_id,
            answer=answer,
            outcome=outcome,
            cancelled=cancelled,
            idempotency_key=idempotency_key,
            disclosure_dependencies=disclosure_dependencies,
        )


@dataclass(frozen=True, slots=True)
class PostgreSQLCitationInspectorService:
    """Inspect accepted citations inside one authorized PostgreSQL UoW."""

    uow_factory: UnitOfWorkFactory
    session_factory: object
    context_verifier: object

    async def inspect(
        self,
        *,
        context: TrustedPersistenceContext,
        requester: CitationInspectorRequester,
        citation_id: UUID,
        mode: CitationInspectionMode = CitationInspectionMode.CURRENT,
        expected_citation: Citation | None = None,
    ) -> CitationInspectionResult:
        session_requester = QASessionRequester(
            subject_id=requester.subject_id,
            authorization_generation=requester.authorization_generation,
        )
        async with self.uow_factory(context) as uow:
            inspector = ProtectedCitationInspector(
                citation_repository=uow.answer_disclosures,  # type: ignore[attr-defined]
                context_bundle_repository=uow.context_bundles,  # type: ignore[attr-defined]
                disclosure_revalidator=DisclosureRevalidationService(
                    state_provider=_CurrentSessionDisclosureState(
                        uow=uow,
                        requester=session_requester,
                    ),
                    result_repository=uow.disclosure_validations,  # type: ignore[attr-defined]
                ),
                excerpt_resolver=ParsedArtifactCitationExcerptResolver(
                    PostgreSQLParsedArtifactRepository(
                        self.session_factory,  # type: ignore[arg-type]
                        context_factory=lambda workspace_id, environment_id: (
                            _citation_context(
                                context,
                                workspace_id=workspace_id,
                                environment_id=environment_id,
                            )
                        ),
                        context_verifier=self.context_verifier,  # type: ignore[arg-type]
                    )
                ),
            )
            result = await inspector.inspect(
                context=context,
                requester=requester,
                citation_id=citation_id,
                mode=mode,
                expected_citation=expected_citation,
            )
            await uow.commit()
            return result


def _citation_context(
    context: TrustedPersistenceContext,
    *,
    workspace_id: UUID,
    environment_id: UUID,
) -> TrustedPersistenceContext:
    scope = context.scope
    if (
        not isinstance(scope, EnvironmentScope)
        or scope.workspace_id != workspace_id
        or scope.environment_id != environment_id
    ):
        raise RuntimeError("citation excerpt scope is unavailable")
    return context


@dataclass(frozen=True, slots=True)
class _PostgreSQLFeedbackTargetResolver:
    uow: object

    async def resolve(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        requester: FeedbackRequester,
        target_kind: FeedbackTargetKind,
        target_id: UUID,
        run_id: UUID,
        message_id: UUID | None,
        citation_id: UUID | None,
    ) -> FeedbackTargetReference | None:
        try:
            run = await self.uow.answer_question_runs.resolve_run_in_scope(  # type: ignore[attr-defined]
                workspace_id=workspace_id,
                environment_id=environment_id,
                run_id=run_id,
            )
            if run.subject.id != requester.subject_id or run.final_attempt_id is None:
                return None
            link = await self.uow.answer_question_runs.resolve_message_link(run_id)  # type: ignore[attr-defined]
            if link is None or message_id is None or (link.message_id != message_id):
                return None
            record = await self.uow.answer_disclosures.resolve_for_attempt(  # type: ignore[attr-defined]
                workspace_id=workspace_id,
                environment_id=environment_id,
                run_id=run_id,
                attempt_id=run.final_attempt_id,
            )
            accepted = None if record is None else record.accepted_answer
            if accepted is None:
                return None
            if target_kind is FeedbackTargetKind.ANSWER:
                if target_id != accepted.answer.answer_id or citation_id is not None:
                    return None
            else:
                if citation_id != target_id or not any(
                    item.citation_id == target_id for item in accepted.answer.citations
                ):
                    return None
            return FeedbackTargetReference(
                workspace_id=workspace_id,
                environment_id=environment_id,
                target_kind=target_kind,
                target_id=target_id,
                run_id=run_id,
                message_id=message_id,
                citation_id=citation_id,
            )
        except Exception:
            return None


@dataclass(frozen=True, slots=True)
class PostgreSQLFeedbackService:
    """Persist feedback only after resolving its canonical accepted target."""

    uow_factory: UnitOfWorkFactory

    async def submit(
        self,
        *,
        context: TrustedPersistenceContext,
        requester: FeedbackRequester,
        run_id: UUID,
        message_id: UUID | None,
        target_kind: FeedbackTargetKind,
        target_id: UUID,
        citation_id: UUID | None,
        outcome: FeedbackOutcome,
        comment: str | None,
        reason: str | None,
        idempotency_key: IdempotencyKey,
    ) -> FeedbackSubmissionResult:
        async with self.uow_factory(context) as uow:
            service = PersistedFeedbackService(
                repository=uow.feedback,
                target_resolver=_PostgreSQLFeedbackTargetResolver(uow),
                audit_writer=uow.audit,
            )
            result = await service.submit(
                context=context,
                requester=requester,
                run_id=run_id,
                message_id=message_id,
                target_kind=target_kind,
                target_id=target_id,
                citation_id=citation_id,
                outcome=outcome,
                comment=comment,
                reason=reason,
                idempotency_key=idempotency_key,
            )
            await uow.commit()
            return result


__all__ = [
    "PostgreSQLCitationInspectorService",
    "PostgreSQLFeedbackService",
    "PostgreSQLQASessionService",
]
