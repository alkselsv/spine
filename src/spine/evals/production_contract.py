"""Typed observation seam for the R1 production-contract evaluation mode.

The evaluation plane may inspect the result of an application composition root,
but it must not manufacture an answer from the dataset's gold labels.  This
module keeps that boundary explicit: an application flow returns the exact
typed values it produced, and the runner only maps those values to the
existing evaluation contracts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal, Protocol
from uuid import UUID

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
    available_revision_ids: tuple[UUID, ...]
    retrieval_strategy: str

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
        disclosed = (
            self.disclosure.accepted_answer.answer
            if self.disclosure.accepted_answer is not None
            else self.disclosure.safe_output
        )
        if disclosed != self.answer:
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
                if self.answer.answer is None or not self.answer.answer.citations:
                    raise ProductionContractObservationError("answered output has no citations")
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
        return observation.validate()


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
    "PRODUCTION_CONTRACT_INDEX_VERSION",
    "PRODUCTION_CONTRACT_CASE_REQUEST_SCHEMA",
    "PRODUCTION_CONTRACT_OBSERVATION_SEAM",
    "ProductionContractApplicationFlow",
    "ProductionContractCaseRequest",
    "ProductionContractObservation",
    "ProductionContractObservationBridge",
    "ProductionContractObservationError",
]
