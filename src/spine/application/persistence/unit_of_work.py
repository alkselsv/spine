"""Framework-independent asynchronous Unit of Work protocols."""

from __future__ import annotations

from types import TracebackType
from typing import TYPE_CHECKING, Protocol

from spine.application.persistence.context import TrustedPersistenceContext
from spine.application.persistence.repositories import (
    EnvironmentRepository,
    IdempotencyRepository,
    WorkspaceRepository,
    SourceObservationRepository,
    AdmissionRepository,
)
from spine.application.persistence.outbox import OutboxWriter
from spine.application.persistence.ingestion import IngestionRepository
from spine.application.persistence.canonical import CanonicalAcceptanceRepository
from spine.application.persistence.boundary import CanonicalBoundaryRepository
from spine.application.persistence.projection import ProjectionRepository
from spine.application.persistence.context_profiles import ContextProfileRepository
from spine.application.persistence.context_bundles import ContextBundleRepository
from spine.application.persistence.answer_question_runs import AnswerQuestionRunRepository
from spine.application.persistence.disclosure import DisclosureValidationRepository
from spine.application.qa_sessions import QASessionRepository

if TYPE_CHECKING:
    from spine.application.diagnostics.audit import AuditWriter
    from spine.application.access_policies import AccessPolicyRepository


class TenantUnitOfWork(Protocol):
    @property
    def workspaces(self) -> WorkspaceRepository: ...

    @property
    def environments(self) -> EnvironmentRepository: ...

    @property
    def context_profiles(self) -> ContextProfileRepository: ...

    @property
    def context_bundles(self) -> ContextBundleRepository: ...

    @property
    def answer_question_runs(self) -> AnswerQuestionRunRepository: ...

    @property
    def disclosure_validations(self) -> DisclosureValidationRepository: ...

    @property
    def qa_sessions(self) -> QASessionRepository: ...

    async def __aenter__(self) -> "TenantUnitOfWork": ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...


class TenantUnitOfWorkFactory(Protocol):
    def __call__(self, context: TrustedPersistenceContext) -> TenantUnitOfWork: ...


class UnitOfWork(TenantUnitOfWork, Protocol):
    @property
    def sources(self) -> SourceObservationRepository: ...
    @property
    def ingestion(self) -> IngestionRepository: ...

    @property
    def admissions(self) -> AdmissionRepository: ...
    @property
    def canonical(self) -> CanonicalAcceptanceRepository: ...
    @property
    def boundaries(self) -> CanonicalBoundaryRepository: ...

    @property
    def canonical_boundaries(self) -> CanonicalBoundaryRepository: ...

    @property
    def projections(self) -> ProjectionRepository: ...

    @property
    def projection(self) -> ProjectionRepository: ...
    @property
    def access_policies(self) -> "AccessPolicyRepository": ...
    @property
    def idempotency(self) -> IdempotencyRepository: ...

    @property
    def outbox(self) -> OutboxWriter: ...

    @property
    def audit(self) -> "AuditWriter": ...

    async def __aenter__(self) -> "UnitOfWork": ...


class UnitOfWorkFactory(Protocol):
    def __call__(self, context: TrustedPersistenceContext) -> UnitOfWork: ...
