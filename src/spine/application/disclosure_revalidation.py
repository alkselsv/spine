"""Current-authority guard for stored answers and Q&A history reads."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from spine.application.persistence.disclosure import DisclosureValidationRepository
from spine.domain.disclosure import (
    DisclosureDependency,
    DisclosurePublicationImpact,
    DisclosureRevalidationCheckpoint,
    DisclosureRevalidationDisposition,
    DisclosureRevalidationFailureCode,
    DisclosureRevalidationObservation,
    DisclosureRevalidationRequest,
    DisclosureRevalidationResult,
    DisclosureValidationFact,
)

if TYPE_CHECKING:
    from spine.application.qa_sessions import QASessionRequester
    from spine.domain.capabilities.answer_disclosure import AnswerDisclosureRecord
    from spine.domain.qa_sessions import QAMessage


class DisclosureStateProvider(Protocol):
    async def resolve(
        self, request: DisclosureRevalidationRequest
    ) -> DisclosureRevalidationObservation: ...


DisclosureValidationResultRepository = DisclosureValidationRepository


class DisclosureIdempotencyConflict(RuntimeError):
    """The same disclosure request identity was submitted with new data."""


class DisclosureRevalidationUnavailable(RuntimeError):
    """The canonical authority could not provide a safe current snapshot."""


class InMemoryDisclosureValidationResultRepository:
    """Deterministic repository for application and contract tests."""

    def __init__(self) -> None:
        self._results: dict[tuple[UUID, UUID, UUID], DisclosureRevalidationResult] = {}
        self._digests: dict[tuple[UUID, UUID, UUID], str] = {}

    async def resolve(
        self, *, workspace_id: UUID, environment_id: UUID, request_id: UUID
    ) -> DisclosureRevalidationResult | None:
        return self._results.get((workspace_id, environment_id, request_id))

    async def save(
        self, result: DisclosureRevalidationResult
    ) -> DisclosureRevalidationResult:
        key = (result.workspace_id, result.environment_id, result.request_id)
        digest = _result_digest(result)
        current = self._results.get(key)
        if current is not None:
            if self._digests[key] != digest:
                raise DisclosureIdempotencyConflict(
                    "Disclosure validation request conflicts with an immutable result."
                )
            return current
        self._results[key] = result
        self._digests[key] = digest
        return result


@dataclass(frozen=True, slots=True)
class DisclosureRevalidationService:
    state_provider: DisclosureStateProvider
    result_repository: DisclosureValidationResultRepository
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)

    async def revalidate(
        self, request: DisclosureRevalidationRequest
    ) -> DisclosureRevalidationResult:
        existing = None
        if not request.force_fresh:
            existing = await self.result_repository.resolve(
                workspace_id=request.workspace_id,
                environment_id=request.environment_id,
                request_id=request.request_id,
            )
        if existing is not None:
            if existing.request_digest != request.command_digest:
                return _result(
                    request,
                    DisclosureRevalidationDisposition.INELIGIBLE,
                    (DisclosureRevalidationFailureCode.IDEMPOTENCY_CONFLICT,),
                    (),
                    "stored_answer_disclosure_unavailable",
                    self.clock(),
                )
            return existing

        if request.cancelled:
            return await self._store(
                _result(
                    request,
                    DisclosureRevalidationDisposition.CANCELLED,
                    (),
                    (),
                    "stored_answer_disclosure_cancelled",
                    self.clock(),
                ),
                persist=not request.force_fresh,
            )

        try:
            observation = await self.state_provider.resolve(request)
        except Exception:
            return await self._store(
                _result(
                    request,
                    DisclosureRevalidationDisposition.RETRYABLE_FAILURE,
                    (DisclosureRevalidationFailureCode.REVALIDATION_UNAVAILABLE,),
                    (),
                    "stored_answer_disclosure_unavailable",
                    self.clock(),
                ),
                persist=not request.force_fresh,
            )

        if not observation.available or (
            observation.workspace_id != request.workspace_id
            or observation.environment_id != request.environment_id
        ):
            return await self._store(
                _result(
                    request,
                    DisclosureRevalidationDisposition.RETRYABLE_FAILURE,
                    (DisclosureRevalidationFailureCode.REVALIDATION_UNAVAILABLE,),
                    (),
                    "stored_answer_disclosure_unavailable",
                    self.clock(),
                ),
                persist=not request.force_fresh,
            )

        evaluated_at = self.clock()
        by_id = {item.dependency_id: item for item in observation.dependencies}
        missing = tuple(
            dependency
            for dependency in request.dependencies.dependencies
            if dependency.dependency_id not in by_id
        )
        failures: list[DisclosureRevalidationFailureCode] = []
        facts: list[DisclosureValidationFact] = []
        if not observation.requester_authorized:
            failures.append(DisclosureRevalidationFailureCode.REQUESTER_UNAUTHORIZED)

        for dependency in request.dependencies.dependencies:
            dependency_observation = by_id.get(dependency.dependency_id)
            dependency_failures = (
                [DisclosureRevalidationFailureCode.DEPENDENCY_MISSING]
                if dependency_observation is None
                else _dependency_failures(
                    dependency,
                    dependency_observation,
                    allow_historical_revision=request.historical_inspection,
                )
            )
            failures.extend(dependency_failures)
            facts.append(
                _fact(
                    request,
                    dependency_id=dependency.dependency_id,
                    disposition=(
                        DisclosureRevalidationDisposition.INELIGIBLE
                        if dependency_failures
                        else DisclosureRevalidationDisposition.SAFE_REUSE
                    ),
                    failure_codes=dependency_failures,
                    evaluated_at=evaluated_at,
                )
            )

        publication_failures = (
            []
            if request.historical_inspection
            else _publication_failures(observation, request.dependencies.dependencies)
        )
        failures.extend(publication_failures)
        if observation.requester_authorized is False:
            disposition = DisclosureRevalidationDisposition.INELIGIBLE
        elif missing or any(
            code
            in {
                DisclosureRevalidationFailureCode.CONTRIBUTOR_UNAUTHORIZED,
                DisclosureRevalidationFailureCode.SOURCE_TOMBSTONED,
                DisclosureRevalidationFailureCode.RETENTION_INELIGIBLE,
                DisclosureRevalidationFailureCode.REQUESTER_UNAUTHORIZED,
                DisclosureRevalidationFailureCode.DEPENDENCY_MISSING,
                DisclosureRevalidationFailureCode.SOURCE_REVISION_UNAVAILABLE,
            }
            for code in failures
        ):
            disposition = DisclosureRevalidationDisposition.INELIGIBLE
        elif failures:
            disposition = DisclosureRevalidationDisposition.REVALIDATION_REQUIRED
        else:
            disposition = DisclosureRevalidationDisposition.SAFE_REUSE

        if publication_failures or observation.requester_authorized is False:
            facts.append(
                _fact(
                    request,
                    dependency_id=None,
                    disposition=disposition,
                    failure_codes=tuple(_unique(failures)),
                    evaluated_at=evaluated_at,
                )
            )
        return await self._store(
            _result(
                request,
                disposition,
                tuple(_unique(failures)),
                tuple(facts),
                _safe_reason(disposition),
                evaluated_at,
            ),
            persist=not request.force_fresh,
        )

    async def _store(
        self, result: DisclosureRevalidationResult, *, persist: bool = True
    ) -> DisclosureRevalidationResult:
        if not persist:
            return result
        return await self.result_repository.save(result)


class RevalidatingQAMessageDisclosure:
    """Adapter used by Q&A history reads to release the whole message."""

    def __init__(self, service: DisclosureRevalidationService) -> None:
        self._service = service

    async def is_eligible(self, *, message: object, requester: object) -> bool:
        from spine.application.qa_sessions import QASessionRequester
        from spine.domain.qa_sessions import QAMessage

        if not isinstance(message, QAMessage) or not isinstance(requester, QASessionRequester):
            return False
        if message.answer is not None and not message.disclosure_dependencies:
            return False
        checkpoint = (
            DisclosureRevalidationCheckpoint.BEFORE_STORED_ANSWER_READ
            if message.answer is not None
            else DisclosureRevalidationCheckpoint.BEFORE_HISTORY_READ
        )
        request = DisclosureRevalidationRequest(
            request_id=uuid5(
                NAMESPACE_URL,
                f"spine:history:{message.message_id}:{message.version}:{checkpoint.value}:"
                f"{requester.subject_id}:{requester.authorization_generation}",
            ),
            workspace_id=message.workspace_id,
            environment_id=message.environment_id,
            requester_id=requester.subject_id,
            checkpoint=checkpoint,
            dependencies=message.disclosure_dependencies,
            run_id=message.run_id,
            message_id=message.message_id,
            force_fresh=True,
        )
        result = await self._service.revalidate(request)
        return result.can_disclose_content

    async def is_accepted_eligible(
        self,
        *,
        message: QAMessage,
        requester: QASessionRequester,
        record: AnswerDisclosureRecord,
    ) -> bool:
        """Freshly revalidate the exact dependencies bound to an accepted record."""

        if (
            record.accepted_answer is None
            or record.validation is None
            or message.run_id is None
            or record.run_id != message.run_id
            or tuple(record.validation.dependency_ids)
            != tuple(message.disclosure_dependency_ids)
        ):
            return False
        request = DisclosureRevalidationRequest(
            request_id=record.request_id,
            workspace_id=message.workspace_id,
            environment_id=message.environment_id,
            requester_id=requester.subject_id,
            checkpoint=DisclosureRevalidationCheckpoint.BEFORE_STORED_ANSWER_READ,
            dependencies=message.disclosure_dependencies,
            run_id=message.run_id,
            attempt_id=record.attempt_id,
            message_id=message.message_id,
            force_fresh=True,
        )
        result = await self._service.revalidate(request)
        return result.can_disclose_content


def _dependency_failures(
    dependency: DisclosureDependency,
    observation: object,
    *,
    allow_historical_revision: bool = False,
) -> list[DisclosureRevalidationFailureCode]:
    from spine.domain.disclosure import DisclosureDependencyObservation

    if not isinstance(observation, DisclosureDependencyObservation):
        return [DisclosureRevalidationFailureCode.DEPENDENCY_MISSING]
    failures: list[DisclosureRevalidationFailureCode] = []
    if observation.source_object_id != dependency.source_object_id:
        failures.append(DisclosureRevalidationFailureCode.DEPENDENCY_SCOPE_MISMATCH)
    if dependency.policy_version_id is None or observation.current_policy_version_id is None:
        failures.append(DisclosureRevalidationFailureCode.POLICY_CHANGED)
    elif observation.current_policy_version_id != dependency.policy_version_id:
        failures.append(DisclosureRevalidationFailureCode.POLICY_CHANGED)
    if (
        dependency.policy_generation is None
        or observation.current_policy_generation is None
        or observation.current_policy_generation != dependency.policy_generation
    ):
        failures.append(DisclosureRevalidationFailureCode.POLICY_GENERATION_CHANGED)
    if (
        dependency.membership_generation is None
        or observation.membership_generation is None
        or observation.membership_generation != dependency.membership_generation
    ):
        failures.append(DisclosureRevalidationFailureCode.MEMBERSHIP_GENERATION_CHANGED)
    if (
        dependency.authority_generation is None
        or observation.authority_generation is None
        or observation.authority_generation != dependency.authority_generation
    ):
        failures.append(DisclosureRevalidationFailureCode.AUTHORITY_GENERATION_CHANGED)
    if dependency.service_authority_generation is not None and (
        observation.service_authority_generation is None
        or observation.service_authority_generation != dependency.service_authority_generation
    ):
        failures.append(DisclosureRevalidationFailureCode.AUTHORITY_GENERATION_CHANGED)
    if not observation.contributor_authorized:
        failures.append(DisclosureRevalidationFailureCode.CONTRIBUTOR_UNAUTHORIZED)
    if observation.tombstoned:
        failures.append(DisclosureRevalidationFailureCode.SOURCE_TOMBSTONED)
    if observation.superseded and not allow_historical_revision:
        failures.append(DisclosureRevalidationFailureCode.SOURCE_SUPERSEDED)
    if not observation.retention_eligible:
        failures.append(DisclosureRevalidationFailureCode.RETENTION_INELIGIBLE)
    if allow_historical_revision:
        if (
            not observation.historical_revision_available
            or observation.historical_revision_id != dependency.source_revision_id
            or observation.historical_revision_digest != dependency.source_revision_digest
        ):
            failures.append(DisclosureRevalidationFailureCode.SOURCE_REVISION_UNAVAILABLE)
    else:
        if not observation.revision_current:
            failures.append(DisclosureRevalidationFailureCode.SOURCE_REVISION_REPLACED)
        if observation.current_revision_id is None:
            failures.append(DisclosureRevalidationFailureCode.SOURCE_REVISION_UNAVAILABLE)
        elif observation.current_revision_id != dependency.source_revision_id:
            failures.append(DisclosureRevalidationFailureCode.SOURCE_REVISION_REPLACED)
        if observation.current_revision_digest != dependency.source_revision_digest:
            failures.append(DisclosureRevalidationFailureCode.SOURCE_REVISION_REPLACED)
    return _unique(failures)


def _publication_failures(
    observation: DisclosureRevalidationObservation,
    dependencies: tuple[DisclosureDependency, ...],
) -> list[DisclosureRevalidationFailureCode]:
    publication = observation.publication
    failures: list[DisclosureRevalidationFailureCode] = []
    if any(
        dependency.projection_snapshot_id != publication.pinned_snapshot_id
        for dependency in dependencies
    ):
        failures.append(DisclosureRevalidationFailureCode.SNAPSHOT_CHANGED)
    if any(
        dependency.canonical_boundary_id != publication.pinned_boundary_id
        for dependency in dependencies
    ):
        failures.append(DisclosureRevalidationFailureCode.BOUNDARY_CHANGED)
    if any(
        dependency.canonical_as_of_boundary_id != publication.pinned_as_of_boundary_id
        or dependency.canonical_as_of_generation != publication.pinned_as_of_generation
        for dependency in dependencies
    ):
        failures.append(DisclosureRevalidationFailureCode.AS_OF_CHANGED)
    if publication.snapshot_changed:
        failures.append(DisclosureRevalidationFailureCode.SNAPSHOT_CHANGED)
    if publication.boundary_changed:
        failures.append(DisclosureRevalidationFailureCode.BOUNDARY_CHANGED)
    if publication.as_of_changed:
        failures.append(DisclosureRevalidationFailureCode.AS_OF_CHANGED)
    if publication.impact is DisclosurePublicationImpact.MATERIAL:
        failures.append(DisclosureRevalidationFailureCode.LATER_PUBLICATION_IMPACT)
    elif publication.impact is DisclosurePublicationImpact.UNKNOWN:
        failures.append(DisclosureRevalidationFailureCode.UNKNOWN_PUBLICATION_IMPACT)
    return _unique(failures)


def _fact(
    request: DisclosureRevalidationRequest,
    *,
    dependency_id: UUID | None,
    disposition: DisclosureRevalidationDisposition,
    failure_codes: list[DisclosureRevalidationFailureCode] | tuple[DisclosureRevalidationFailureCode, ...],
    evaluated_at: datetime,
) -> DisclosureValidationFact:
    return DisclosureValidationFact.create(
        validation_id=uuid5(
            NAMESPACE_URL,
            f"spine:disclosure-fact:{request.request_id}:{dependency_id or 'aggregate'}",
        ),
        request_id=request.request_id,
        workspace_id=request.workspace_id,
        environment_id=request.environment_id,
        run_id=request.run_id,
        attempt_id=request.attempt_id,
        message_id=request.message_id,
        checkpoint=request.checkpoint,
        dependency_id=dependency_id,
        disposition=disposition,
        failure_codes=tuple(_unique(list(failure_codes))),
        evaluated_at=evaluated_at,
    )


def _result(
    request: DisclosureRevalidationRequest,
    disposition: DisclosureRevalidationDisposition,
    failure_codes: tuple[DisclosureRevalidationFailureCode, ...],
    facts: tuple[DisclosureValidationFact, ...],
    safe_reason: str,
    evaluated_at: datetime,
) -> DisclosureRevalidationResult:
    del evaluated_at
    return DisclosureRevalidationResult(
        request_id=request.request_id,
        request_digest=request.command_digest,
        workspace_id=request.workspace_id,
        environment_id=request.environment_id,
        checkpoint=request.checkpoint,
        disposition=disposition,
        failure_codes=failure_codes,
        safe_reason=safe_reason,
        facts=facts,
    )


def _safe_reason(disposition: DisclosureRevalidationDisposition) -> str:
    return {
        DisclosureRevalidationDisposition.SAFE_REUSE: "stored_answer_disclosure_allowed",
        DisclosureRevalidationDisposition.REVALIDATION_REQUIRED: "stored_answer_revalidation_required",
        DisclosureRevalidationDisposition.INELIGIBLE: "stored_answer_disclosure_ineligible",
        DisclosureRevalidationDisposition.RETRYABLE_FAILURE: "stored_answer_disclosure_unavailable",
        DisclosureRevalidationDisposition.CANCELLED: "stored_answer_disclosure_cancelled",
    }[disposition]


def _unique(values: list[DisclosureRevalidationFailureCode]) -> list[DisclosureRevalidationFailureCode]:
    return list(dict.fromkeys(values))


def _result_digest(result: DisclosureRevalidationResult) -> str:
    import hashlib
    import json

    payload = result.model_dump(mode="json", exclude={"safe_reason"})
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


__all__ = [
    "DisclosureIdempotencyConflict",
    "DisclosureRevalidationService",
    "DisclosureRevalidationUnavailable",
    "DisclosureStateProvider",
    "DisclosureValidationResultRepository",
    "InMemoryDisclosureValidationResultRepository",
    "RevalidatingQAMessageDisclosure",
]
