"""Typed observation seam for the R1 production-contract evaluation mode.

The evaluation plane may inspect the result of an application composition root,
but it must not manufacture an answer from the dataset's gold labels.  This
module keeps that boundary explicit: an application flow returns the exact
typed values it produced, and the runner only maps those values to the
existing evaluation contracts.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import time
from math import isfinite
from typing import Callable, Final, Literal, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import Field, ValidationError, field_validator, model_validator

from spine.domain.capabilities import (
    AnswerQuestionInput,
    AnswerQuestionOutput,
    AnswerQuestionOutcome,
)
from spine.domain.capabilities.answer_disclosure import AnswerDisclosureRecord
from spine.domain.common import DefinitionModel
from spine.domain.context_bundles import ContextBundle
from spine.domain.projections import ProjectionFreshness, ProjectionHealth
from spine.evals.qa_r1 import QACase
from spine.memory.contracts import (
    ContextOutcome,
    ContextResponse,
    ProjectionRetrievalResult,
    RetrievalResult,
)


PRODUCTION_CONTRACT_OBSERVATION_SEAM: Final[str] = (
    "r1-production-contract-observation:v1"
)
PRODUCTION_CONTRACT_INDEX_VERSION: Final[str] = (
    "r1-production-contract-observation:v1"
)
PRODUCTION_CONTRACT_CASE_REQUEST_SCHEMA: Final[str] = (
    "r1-production-contract-case-request:v1"
)
CASE_EXECUTION_RECORD_SCHEMA: Final[str] = "r1-case-execution-record:v1"


class ProductionContractObservationError(ValueError):
    """A production-contract observation is not safe to evaluate."""


class ProductionContractCaseRequest(DefinitionModel):
    """Gold-label-free input released to an application flow."""

    schema_version: Literal[PRODUCTION_CONTRACT_CASE_REQUEST_SCHEMA] = (
        PRODUCTION_CONTRACT_CASE_REQUEST_SCHEMA
    )
    case_id: str
    question: str
    language: str
    workspace_id: UUID
    environment_id: UUID
    principal_id: UUID
    available_revision_ids: tuple[UUID, ...] = Field(min_length=1)
    retrieval_strategy: str

    @field_validator("case_id", "question", "language", "retrieval_strategy")
    @classmethod
    def validate_non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("production case request fields must not be blank")
        return value

    @field_validator("workspace_id", "environment_id", "principal_id")
    @classmethod
    def validate_scope_identifier(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("production case request scope identifiers must be non-zero")
        return value

    @field_validator("available_revision_ids")
    @classmethod
    def validate_revision_identifiers(cls, value: tuple[UUID, ...]) -> tuple[UUID, ...]:
        if any(item.int == 0 for item in value) or len(set(value)) != len(value):
            raise ValueError("production case request revision identifiers are invalid")
        return value

    @classmethod
    def from_case(cls, case: QACase) -> "ProductionContractCaseRequest":
        return cls(
            case_id=case.case_id,
            question=case.question,
            language=case.language,
            workspace_id=case.workspace_id,
            environment_id=case.environment_id,
            principal_id=case.principal_id,
            available_revision_ids=case.available_revision_ids,
            retrieval_strategy=case.retrieval_strategy,
        )


class ProductionContractObservation(DefinitionModel):
    """The complete, disclosure-safe output of one executed app flow."""

    schema_version: Literal[PRODUCTION_CONTRACT_OBSERVATION_SEAM] = (
        PRODUCTION_CONTRACT_OBSERVATION_SEAM
    )
    case_id: str
    question: AnswerQuestionInput
    context_response: ContextResponse
    context_bundle: ContextBundle | None
    projection_retrieval: ProjectionRetrievalResult
    retrieval: RetrievalResult
    answer: AnswerQuestionOutput
    disclosure: AnswerDisclosureRecord

    def validate(self) -> "ProductionContractObservation":
        """Validate cross-contract identity before the runner sees the data."""

        if not self.case_id.strip():
            raise ProductionContractObservationError("observation case identity is missing")
        if type(self.question) is not AnswerQuestionInput:
            raise ProductionContractObservationError("observation question is invalid")
        if type(self.context_response) is not ContextResponse:
            raise ProductionContractObservationError("observation context response is invalid")
        if type(self.retrieval) is not RetrievalResult:
            raise ProductionContractObservationError("observation retrieval is invalid")
        if type(self.projection_retrieval) is not ProjectionRetrievalResult:
            raise ProductionContractObservationError("observation projection retrieval is invalid")
        if type(self.answer) is not AnswerQuestionOutput:
            raise ProductionContractObservationError("observation answer is invalid")
        if type(self.disclosure) is not AnswerDisclosureRecord:
            raise ProductionContractObservationError("observation disclosure is invalid")

        scope = self.question.scope
        correlation = self.question.correlation
        if self.answer.scope != scope or self.answer.correlation != correlation:
            raise ProductionContractObservationError("answer scope or correlation is inconsistent")
        if (
            self.disclosure.workspace_id != scope.workspace_id
            or self.disclosure.environment_id != scope.environment_id
            or self.disclosure.run_id != correlation.run_id
            or self.disclosure.attempt_id != correlation.attempt_id
            or self.disclosure.outcome is not self.answer.outcome
        ):
            raise ProductionContractObservationError("disclosure lineage is inconsistent")
        if self.disclosure.accepted_answer is not None:
            disclosed = self.disclosure.accepted_answer.answer
            observed = self.answer.answer
        else:
            disclosed = self.disclosure.safe_output
            observed = self.answer
        if disclosed != observed:
            raise ProductionContractObservationError("disclosure output is not the observed answer")

        if self.context_response.outcome in {ContextOutcome.ALLOWED, ContextOutcome.PARTIAL}:
            bundle = self.context_bundle
            if (
                bundle is None
                or self.context_response.context_bundle_ref != bundle.reference
                or bundle.publication_pin_id is None
                or self.context_response.publication_pin_id is None
            ):
                raise ProductionContractObservationError("allowed response has no exact context bundle")
            if (
                bundle.workspace_id != scope.workspace_id
                or bundle.environment_id != scope.environment_id
                or bundle.run_id != correlation.run_id
                or bundle.attempt_id != correlation.attempt_id
                or bundle.publication_pin_id != self.context_response.publication_pin_id
            ):
                raise ProductionContractObservationError("context bundle scope or pin is inconsistent")
            if bundle.freshness is not ProjectionFreshness.CURRENT:
                raise ProductionContractObservationError("context bundle is stale")
            if bundle.health is not ProjectionHealth.HEALTHY:
                raise ProductionContractObservationError("context bundle health is not eligible")
            for reference in bundle.evidence_references:
                if (
                    reference.schema_version != "context-evidence-reference:v2"
                    or reference.original_sha256 is None
                    or reference.revision_digest is None
                    or reference.revision_number is None
                    or reference.processing_receipt_id is None
                    or reference.projected_kind is None
                    or reference.locator_contract_version is None
                    or reference.locator_digest is None
                ):
                    raise ProductionContractObservationError(
                        "context bundle evidence provenance is incomplete"
                    )
            _validate_retrieval_lineage(self.retrieval, bundle)
            if self.answer.outcome is AnswerQuestionOutcome.ANSWERED:
                accepted = self.disclosure.accepted_answer
                if (
                    accepted is None
                    or self.disclosure.validation is None
                    or self.answer.answer is None
                    or not self.answer.answer.citations
                ):
                    raise ProductionContractObservationError(
                        "answered output has no accepted disclosure or citations"
                    )
                _validate_claim_text_coverage(self.answer.answer)
                validation = accepted.validation
                if (
                    validation.context_bundle_id != bundle.bundle_id
                    or validation.context_bundle_version != bundle.bundle_version
                    or validation.context_bundle_digest != bundle.bundle_digest
                ):
                    raise ProductionContractObservationError(
                        "disclosure validation is not pinned to the observed context bundle"
                    )
                evidence_by_id = {
                    reference.evidence_id: reference
                    for reference in bundle.evidence_references
                }
                for citation in self.answer.answer.citations:
                    expected = evidence_by_id.get(citation.evidence_reference.evidence_id)
                    if (
                        citation.context_bundle != bundle.lineage_reference
                        or expected is None
                        or citation.evidence_reference != expected
                    ):
                        raise ProductionContractObservationError(
                            "answer cites fabricated or unrelated evidence"
                        )
            _validate_projection_retrieval(self.projection_retrieval, bundle)
        else:
            if self.context_bundle is not None or self.context_response.context_bundle_ref is not None:
                raise ProductionContractObservationError("non-bundle response carries protected context")
            if self.retrieval.references or self.retrieval.context:
                raise ProductionContractObservationError("non-bundle response carries retrieval context")
            if (
                self.projection_retrieval.evidence
                or self.projection_retrieval.chunks
                or self.projection_retrieval.entities
                or self.projection_retrieval.relations
            ):
                raise ProductionContractObservationError(
                    "non-bundle response carries projection context"
                )
            if self.answer.outcome is AnswerQuestionOutcome.ANSWERED:
                raise ProductionContractObservationError(
                    "non-bundle response cannot disclose an answer"
                )

        return self


class ProductionContractApplicationFlow(Protocol):
    """Application composition root executed by the production-contract mode."""

    async def execute(
        self, request: ProductionContractCaseRequest
    ) -> ProductionContractObservation: ...


@dataclass(frozen=True, slots=True)
class ProductionContractObservationBridge:
    """Adapt a real application flow to the runner's versioned seam."""

    flow: ProductionContractApplicationFlow
    index_version: str = PRODUCTION_CONTRACT_INDEX_VERSION

    async def observe(
        self, *, request: ProductionContractCaseRequest
    ) -> ProductionContractObservation:
        observation = await self.flow.execute(request)
        if not isinstance(observation, ProductionContractObservation):
            raise ProductionContractObservationError("application flow returned an invalid observation")
        if observation.case_id != request.case_id:
            raise ProductionContractObservationError("application flow returned another case")
        if (
            observation.question.subject.text != request.question
            or observation.question.scope.workspace_id != request.workspace_id
            or observation.question.scope.environment_id != request.environment_id
        ):
            raise ProductionContractObservationError("application flow returned another request scope")
        if observation.context_bundle is not None and any(
            reference.source_revision_id not in request.available_revision_ids
            for reference in observation.context_bundle.evidence_references
        ):
            raise ProductionContractObservationError("observation contains an unavailable source revision")
        if observation.retrieval.index_version != self.index_version:
            raise ProductionContractObservationError(
                "observed retrieval index version is not pinned"
            )
        return observation.validate()


class CaseExecutionDisposition(str, Enum):
    """Safe outer disposition for one gold-blind application invocation."""

    OBSERVED = "observed"
    DENIED = "denied"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


class CaseExecutionFailureCategory(str, Enum):
    """Closed, content-free categories exposed by the observer boundary."""

    INVALID_CASE = "invalid_case"
    INVALID_SCOPE = "invalid_scope"
    INVALID_REVISION = "invalid_revision"
    INVALID_OBSERVATION = "invalid_observation"
    STALE_LINEAGE = "stale_lineage"
    FABRICATED_CITATION = "fabricated_citation"
    INCONSISTENT_DISCLOSURE = "inconsistent_disclosure"
    AUTHORIZATION_DENIED = "authorization_denied"
    RUNTIME_UNAVAILABLE = "runtime_unavailable"
    PARTIAL_FAILURE = "partial_failure"
    APPLICATION_FAILURE = "application_failure"
    INVALID_TIMING = "invalid_timing"


class CaseExecutionRecord(DefinitionModel):
    """Validated outer result; protected observation data is all-or-nothing."""

    schema_version: Literal[CASE_EXECUTION_RECORD_SCHEMA] = CASE_EXECUTION_RECORD_SCHEMA
    case_id: str = Field(min_length=1, max_length=128)
    workspace_id: UUID
    environment_id: UUID
    disposition: CaseExecutionDisposition
    observation: ProductionContractObservation | None = None
    failure_category: CaseExecutionFailureCategory | None = None
    correlation_id: UUID | None = None
    failure_type: str | None = Field(
        default=None,
        max_length=128,
        pattern=r"^[A-Za-z_][A-Za-z0-9_.:-]{0,127}$",
    )
    completed_attempt_id: UUID | None = None
    completed_attempt_number: int | None = Field(default=None, ge=1)
    observer_elapsed_ms: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_disposition(self) -> "CaseExecutionRecord":
        if self.disposition is CaseExecutionDisposition.OBSERVED:
            if (
                self.observation is None
                or self.failure_category is not None
                or self.failure_type is not None
            ):
                raise ValueError("observed record requires only a validated observation")
            if self.observer_elapsed_ms is None:
                raise ValueError("observed record requires observer elapsed time")
            try:
                self.observation.validate()
            except ProductionContractObservationError as error:
                raise ValueError("observed record contains an invalid observation") from error
            if self.observation.case_id != self.case_id:
                raise ValueError("observed record case identity is inconsistent")
            if (
                self.observation.question.scope.workspace_id != self.workspace_id
                or self.observation.question.scope.environment_id != self.environment_id
            ):
                raise ValueError("observed record scope is inconsistent")
            expected_correlation_id = _observation_correlation_id(self.observation)
            if self.correlation_id != expected_correlation_id:
                raise ValueError("observed record correlation is inconsistent")
        elif self.observation is not None or self.failure_category is None:
            raise ValueError("safe disposition cannot carry an observation")
        if self.correlation_id is not None and self.correlation_id.int == 0:
            raise ValueError("correlation_id must be non-zero")
        if self.completed_attempt_id is not None and self.completed_attempt_id.int == 0:
            raise ValueError("completed_attempt_id must be non-zero")
        if (self.completed_attempt_id is None) != (
            self.completed_attempt_number is None
        ):
            raise ValueError("completed attempt metadata must be complete")
        return self


class ProductionContractObserverFailure(RuntimeError):
    """Base class for safe application-flow dispositions."""

    category: CaseExecutionFailureCategory = (
        CaseExecutionFailureCategory.APPLICATION_FAILURE
    )
    disposition: CaseExecutionDisposition = CaseExecutionDisposition.FAILED

    def __init__(
        self,
        *,
        correlation_id: UUID | None = None,
        completed_attempt_id: UUID | None = None,
        completed_attempt_number: int | None = None,
    ) -> None:
        super().__init__()
        self.correlation_id = correlation_id
        self.completed_attempt_id = completed_attempt_id
        self.completed_attempt_number = completed_attempt_number


class ProductionContractAuthorizationDenied(ProductionContractObserverFailure):
    category = CaseExecutionFailureCategory.AUTHORIZATION_DENIED
    disposition = CaseExecutionDisposition.DENIED


class ProductionContractUnavailable(ProductionContractObserverFailure):
    category = CaseExecutionFailureCategory.RUNTIME_UNAVAILABLE
    disposition = CaseExecutionDisposition.UNAVAILABLE


class ProductionContractPartialFailure(ProductionContractObserverFailure):
    category = CaseExecutionFailureCategory.PARTIAL_FAILURE


@dataclass(frozen=True, slots=True)
class ProductionContractObserver:
    """Run and freeze one bounded, gold-blind production observation."""

    flow: ProductionContractApplicationFlow
    clock: Callable[[], float] = time.monotonic
    index_version: str = PRODUCTION_CONTRACT_INDEX_VERSION

    async def observe(
        self, *, request: ProductionContractCaseRequest
    ) -> CaseExecutionRecord:
        preflight_failure = _validate_observer_request(request)
        if preflight_failure is not None:
            return CaseExecutionRecord(
                case_id=_safe_case_id(request),
                workspace_id=_safe_scope_id(request, "workspace_id"),
                environment_id=_safe_scope_id(request, "environment_id"),
                disposition=CaseExecutionDisposition.FAILED,
                failure_category=preflight_failure,
                correlation_id=_safe_correlation_id(request),
                failure_type="preflight_validation",
            )

        try:
            started_at = self.clock()
        except Exception:
            return self._failed(
                request,
                CaseExecutionFailureCategory.INVALID_TIMING,
                failure_type="clock_error",
                elapsed_ms=None,
            )
        if not isfinite(started_at):
            return self._failed(
                request,
                CaseExecutionFailureCategory.INVALID_TIMING,
                failure_type="clock_error",
                elapsed_ms=None,
            )

        try:
            observation = await ProductionContractObservationBridge(
                self.flow,
                index_version=self.index_version,
            ).observe(request=request)
            elapsed_ms = self._elapsed_ms(started_at)
            if elapsed_ms is None:
                return self._failed(
                    request,
                    CaseExecutionFailureCategory.INVALID_TIMING,
                    elapsed_ms=None,
                )
            return CaseExecutionRecord(
                case_id=request.case_id,
                workspace_id=request.workspace_id,
                environment_id=request.environment_id,
                disposition=CaseExecutionDisposition.OBSERVED,
                observation=observation,
                correlation_id=_observation_correlation_id(observation),
                observer_elapsed_ms=elapsed_ms,
            )
        except ProductionContractObserverFailure as error:
            return self._failed(
                request,
                error.category,
                disposition=error.disposition,
                correlation_id=error.correlation_id,
                failure_type=_safe_failure_type(error),
                completed_attempt_id=error.completed_attempt_id,
                completed_attempt_number=error.completed_attempt_number,
                elapsed_ms=self._elapsed_ms(started_at),
            )
        except ProductionContractObservationError as error:
            return self._failed(
                request,
                _observation_failure_category(error),
                failure_type=_safe_failure_type(error),
                elapsed_ms=self._elapsed_ms(started_at),
            )
        except Exception as error:
            return self._failed(
                request,
                CaseExecutionFailureCategory.APPLICATION_FAILURE,
                failure_type=_safe_failure_type(error),
                elapsed_ms=self._elapsed_ms(started_at),
            )

    def _elapsed_ms(self, started_at: float) -> int | None:
        try:
            finished_at = self.clock()
        except Exception:
            return None
        if not isfinite(finished_at) or finished_at < started_at:
            return None
        return int(round((finished_at - started_at) * 1000))

    def _failed(
        self,
        request: ProductionContractCaseRequest,
        category: CaseExecutionFailureCategory,
        *,
        disposition: CaseExecutionDisposition = CaseExecutionDisposition.FAILED,
        correlation_id: UUID | None = None,
        failure_type: str | None = None,
        completed_attempt_id: UUID | None = None,
        completed_attempt_number: int | None = None,
        elapsed_ms: int | None,
    ) -> CaseExecutionRecord:
        return CaseExecutionRecord(
            case_id=_safe_case_id(request),
            workspace_id=_safe_scope_id(request, "workspace_id"),
            environment_id=_safe_scope_id(request, "environment_id"),
            disposition=disposition,
            failure_category=category,
            correlation_id=correlation_id or _safe_correlation_id(request),
            failure_type=failure_type or category.value,
            completed_attempt_id=completed_attempt_id,
            completed_attempt_number=completed_attempt_number,
            observer_elapsed_ms=elapsed_ms,
        )


def _safe_case_id(request: object) -> str:
    if isinstance(request, ProductionContractCaseRequest) and request.case_id.strip():
        return request.case_id
    return "unknown"


def _safe_scope_id(request: object, field_name: str) -> UUID:
    value = getattr(request, field_name, None)
    return value if isinstance(value, UUID) else UUID(int=0)


def _safe_correlation_id(request: object) -> UUID | None:
    if not isinstance(request, ProductionContractCaseRequest):
        return None
    return uuid5(
        NAMESPACE_URL,
        "spine:r1:observer:"
        f"{request.case_id}:{request.workspace_id}:{request.environment_id}",
    )


def _safe_failure_type(error: BaseException) -> str:
    name = type(error).__name__
    return name if name and name[0].isalpha() else "unknown_failure"


def _validate_observer_request(
    request: object,
) -> CaseExecutionFailureCategory | None:
    if not isinstance(request, ProductionContractCaseRequest):
        return CaseExecutionFailureCategory.INVALID_CASE
    try:
        ProductionContractCaseRequest.model_validate(
            request.model_dump(mode="python")
        )
    except ValidationError as error:
        fields = {
            str(location[0])
            for item in error.errors()
            for location in (item.get("loc", ()),)
            if location
        }
        if fields & {"workspace_id", "environment_id", "principal_id"}:
            return CaseExecutionFailureCategory.INVALID_SCOPE
        if "available_revision_ids" in fields:
            return CaseExecutionFailureCategory.INVALID_REVISION
        return CaseExecutionFailureCategory.INVALID_CASE
    return None


def _observation_correlation_id(observation: ProductionContractObservation) -> UUID | None:
    correlation = observation.question.correlation
    return correlation.correlation_id or correlation.trace_id


def _observation_failure_category(
    error: ProductionContractObservationError,
) -> CaseExecutionFailureCategory:
    message = str(error)
    if "another case" in message or "case identity" in message:
        return CaseExecutionFailureCategory.INVALID_CASE
    if "request scope" in message or "scope" in message:
        return CaseExecutionFailureCategory.INVALID_SCOPE
    if "unavailable source revision" in message:
        return CaseExecutionFailureCategory.INVALID_REVISION
    if "stale" in message:
        return CaseExecutionFailureCategory.STALE_LINEAGE
    if "fabricated" in message or "unrelated evidence" in message:
        return CaseExecutionFailureCategory.FABRICATED_CITATION
    if "disclosure" in message:
        return CaseExecutionFailureCategory.INCONSISTENT_DISCLOSURE
    return CaseExecutionFailureCategory.INVALID_OBSERVATION


ProductionContractExecutionRecord = CaseExecutionRecord


def _validate_retrieval_lineage(retrieval: RetrievalResult, bundle: ContextBundle) -> None:
    expected = {reference.evidence_id: reference for reference in bundle.evidence_references}
    actual = {reference.evidence_id: reference for reference in retrieval.references}
    if set(expected) != set(actual):
        raise ProductionContractObservationError("retrieval and bundle evidence sets differ")
    for evidence_id, reference in expected.items():
        observed = actual[evidence_id]
        if (
            observed.workspace_id != reference.workspace_id
            or observed.environment_id != reference.environment_id
            or observed.source_id != str(reference.source_object_id)
            or observed.source_revision_id != reference.source_revision_id
            or observed.locator != reference.locator.model_dump(mode="json")
        ):
            raise ProductionContractObservationError(
                f"retrieval lineage is inconsistent for evidence {evidence_id}"
            )


def _validate_claim_text_coverage(answer: object) -> None:
    """Reject answer prose outside the text ranges attributed to checked claims."""

    from spine.domain.capabilities import AnswerContent

    if type(answer) is not AnswerContent:
        raise ProductionContractObservationError("answer content is not a typed contract")
    cursor = 0
    for claim in sorted(answer.claims, key=lambda item: item.location.start_offset):
        gap = answer.text[cursor : claim.location.start_offset]
        if any(character.isalnum() for character in gap):
            raise ProductionContractObservationError(
                "answer contains text outside attributed claims"
            )
        cursor = max(cursor, claim.location.end_offset)
    if any(character.isalnum() for character in answer.text[cursor:]):
        raise ProductionContractObservationError(
            "answer contains text outside attributed claims"
        )


def _validate_projection_retrieval(
    retrieval: ProjectionRetrievalResult, bundle: ContextBundle
) -> None:
    expected = {reference.evidence_id: reference for reference in bundle.evidence_references}
    actual = {reference.evidence_id: reference for reference in retrieval.evidence}
    if set(expected) != set(actual):
        raise ProductionContractObservationError("projection retrieval evidence sets differ")
    if retrieval.read_context is None:
        raise ProductionContractObservationError("projection retrieval has no pinned read context")
    read_context = retrieval.read_context
    if (
        read_context.workspace_id != bundle.workspace_id
        or read_context.environment_id != bundle.environment_id
        or read_context.publication_pin_id != bundle.publication_pin_id
        or read_context.projection_snapshot_id
        != bundle.projection_snapshot.projection_snapshot_id
        or read_context.projection_activation_id
        != bundle.projection_activation.projection_activation_id
        or read_context.activation_generation != bundle.activation_generation
        or read_context.canonical_boundary_id != bundle.canonical_boundary.boundary_id
        or read_context.canonical_boundary_digest != bundle.canonical_boundary.manifest_digest
    ):
        raise ProductionContractObservationError("projection retrieval pin is inconsistent")
    for evidence_id, reference in expected.items():
        observed = actual[evidence_id]
        if (
            observed.workspace_id != reference.workspace_id
            or observed.environment_id != reference.environment_id
            or observed.source_object_id != reference.source_object_id
            or observed.source_revision_id != reference.source_revision_id
            or observed.revision_digest != reference.revision_digest
            or observed.revision_number != reference.revision_number
            or observed.processing_receipt_id != reference.processing_receipt_id
            or observed.locator_contract_version != reference.locator_contract_version
            or observed.locator.model_dump(mode="json")
            != reference.locator.model_dump(mode="json")
            or observed.evidence_digest != reference.evidence_digest
            or observed.status != "valid"
        ):
            raise ProductionContractObservationError(
                f"projection retrieval provenance is inconsistent for evidence {evidence_id}"
            )


__all__ = [
    "CASE_EXECUTION_RECORD_SCHEMA",
    "CaseExecutionDisposition",
    "CaseExecutionFailureCategory",
    "CaseExecutionRecord",
    "ProductionContractExecutionRecord",
    "PRODUCTION_CONTRACT_INDEX_VERSION",
    "PRODUCTION_CONTRACT_CASE_REQUEST_SCHEMA",
    "PRODUCTION_CONTRACT_OBSERVATION_SEAM",
    "ProductionContractApplicationFlow",
    "ProductionContractCaseRequest",
    "ProductionContractObservation",
    "ProductionContractObservationBridge",
    "ProductionContractObservationError",
    "ProductionContractObserver",
    "ProductionContractObserverFailure",
    "ProductionContractAuthorizationDenied",
    "ProductionContractUnavailable",
    "ProductionContractPartialFailure",
]
