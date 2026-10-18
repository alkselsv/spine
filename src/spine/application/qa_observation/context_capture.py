"""Request-scoped, in-memory capture of an authorized Context Broker call."""

from __future__ import annotations

from enum import Enum
from typing import Protocol

from pydantic import model_validator

from spine.application.qa_observation.contracts import (
    AdmittedExecutionIdentity,
    CaptureSubscription,
)
from spine.domain.capabilities import AnswerQuestionInput
from spine.domain.common import ValidatedDefinitionModel
from spine.domain.context_bundles import ContextBundle
from spine.memory.contracts import (
    ContextOutcome,
    ContextReference,
    ContextResponse,
    ProjectionRetrievalResult,
    PublicationPin,
    RetrievalReadContext,
    RetrievalResult,
)


class AuthorizedContextCaptureContext(ValidatedDefinitionModel):
    """Trusted C0 identity and capture routing bound to the broker request."""

    identity: AdmittedExecutionIdentity
    subscription: CaptureSubscription
    question: AnswerQuestionInput

    @model_validator(mode="after")
    def validate_identity(self) -> "AuthorizedContextCaptureContext":
        question = self.question
        identity = self.identity
        if (
            question.scope.workspace_id != identity.workspace_id
            or question.scope.environment_id != identity.environment_id
            or question.correlation.run_id != identity.run_id
            or question.correlation.attempt_id != identity.attempt_id
        ):
            raise ValueError("capture question does not match admitted execution")
        return self


class AuthorizedContextCapture(ValidatedDefinitionModel):
    """Exact typed retrieval lineage returned by one authorized broker call."""

    capture_context: AuthorizedContextCaptureContext
    response: ContextResponse
    projection_retrieval: ProjectionRetrievalResult
    retrieval: RetrievalResult
    context_bundle: ContextBundle
    publication_pin: PublicationPin

    @model_validator(mode="after")
    def validate_same_execution(self) -> "AuthorizedContextCapture":
        question = self.capture_context.question
        identity = self.capture_context.identity
        bundle = self.context_bundle
        pin = self.publication_pin
        response = self.response
        retrieval = self.projection_retrieval
        read_contexts = _read_contexts(retrieval)
        if response.outcome not in {ContextOutcome.ALLOWED, ContextOutcome.PARTIAL}:
            raise ValueError("capture requires an authorized evidence outcome")
        if (
            response.context_bundle_ref != bundle.reference
            or response.publication_pin_id != pin.pin_id
            or bundle.publication_pin_id != pin.pin_id
            or bundle.workspace_id != identity.workspace_id
            or bundle.environment_id != identity.environment_id
            or bundle.run_id != identity.run_id
            or bundle.attempt_id != identity.attempt_id
            or pin.workspace_id != identity.workspace_id
            or pin.environment_id != identity.environment_id
            or pin.projection_snapshot.projection_snapshot_id
            != bundle.projection_snapshot.projection_snapshot_id
            or pin.activation_generation != bundle.activation_generation
            or question.scope.workspace_id != bundle.workspace_id
            or question.scope.environment_id != bundle.environment_id
            or question.correlation.run_id != bundle.run_id
            or question.correlation.attempt_id != bundle.attempt_id
            or not read_contexts
            or any(
                context.workspace_id != identity.workspace_id
                or context.environment_id != identity.environment_id
                or context.publication_pin_id != pin.pin_id
                or context.publication_pin_digest != pin.pin_digest
                or context.projection_snapshot_id
                != pin.projection_snapshot.projection_snapshot_id
                or context.projection_activation_id
                != pin.projection_activation.projection_activation_id
                or context.activation_generation != pin.activation_generation
                or context.canonical_boundary_id != pin.canonical_boundary.boundary_id
                or context.canonical_boundary_digest
                != pin.canonical_boundary.boundary_digest
                for context in read_contexts
            )
        ):
            raise ValueError("capture lineage does not match authorized execution")
        if self.retrieval.outcome is not response.outcome:
            raise ValueError("capture retrieval outcome does not match response")
        if self.retrieval.references != _references(retrieval):
            raise ValueError("capture references do not match projection retrieval")
        if (
            self.retrieval.context != tuple(item.text for item in retrieval.chunks)
            or self.retrieval.entities
            != tuple(item.model_dump(mode="json") for item in retrieval.entities)
            or self.retrieval.relations
            != tuple(item.model_dump(mode="json") for item in retrieval.relations)
            or self.retrieval.retrieval_strategy != bundle.retrieval_strategy
            or self.retrieval.index_version != retrieval.physical_projection_version
        ):
            raise ValueError("capture retrieval view does not match physical retrieval")
        projected_evidence = {
            (
                item.evidence_id,
                item.workspace_id,
                item.environment_id,
                item.source_object_id,
                item.source_revision_id,
            )
            for item in retrieval.evidence
        }
        bundled_evidence = {
            (
                item.evidence_id,
                item.workspace_id,
                item.environment_id,
                item.source_object_id,
                item.source_revision_id,
            )
            for item in bundle.evidence_references
        }
        if not bundled_evidence or bundled_evidence != projected_evidence:
            raise ValueError("capture bundle evidence is outside this retrieval")
        if any(
            item.workspace_id != identity.workspace_id
            or item.environment_id != identity.environment_id
            or not any(
                (item.source_object_id, item.source_revision_id)
                in set(zip(context.source_object_ids, context.source_revision_ids))
                for context in read_contexts
            )
            for item in retrieval.evidence
        ):
            raise ValueError(
                "capture evidence is outside authorized revision membership"
            )
        return self


class CaptureDisposition(str, Enum):
    CAPTURED = "captured"
    NOT_ELIGIBLE = "not_eligible"
    INVALID_CONTEXT = "invalid_context"
    INVALID_LINEAGE = "invalid_lineage"
    EXPIRED = "expired"
    SINK_FAILED = "sink_failed"
    SINK_TIMEOUT = "sink_timeout"


class AuthorizedContextCaptureSink(Protocol):
    """Consume one authorized capture in memory; implementations must not persist it."""

    async def capture(self, value: AuthorizedContextCapture) -> None: ...


class AuthorizedContextCaptureResult(ValidatedDefinitionModel):
    """Canonical broker response and independent observer capture disposition."""

    response: ContextResponse
    disposition: CaptureDisposition


def retrieval_result_from_projection(
    projection: ProjectionRetrievalResult,
    *,
    retrieval_strategy: str,
    outcome: ContextOutcome,
) -> RetrievalResult:
    """Build the legacy typed view from this exact physical retrieval result.

    This is an in-memory shape conversion, not another retrieval or a new
    evidence record. ``AuthorizedContextCapture`` validates the view against
    the source result before it can reach the observer sink.
    """

    return RetrievalResult(
        context=tuple(item.text for item in projection.chunks),
        entities=tuple(item.model_dump(mode="json") for item in projection.entities),
        relations=tuple(item.model_dump(mode="json") for item in projection.relations),
        references=_references(projection),
        retrieval_strategy=retrieval_strategy,
        index_version=projection.physical_projection_version,
        outcome=outcome,
    )


def _references(
    projection: ProjectionRetrievalResult,
) -> tuple[ContextReference, ...]:
    return tuple(
        ContextReference(
            source_id=str(item.source_object_id),
            source_revision_id=item.source_revision_id,
            workspace_id=item.workspace_id,
            environment_id=item.environment_id,
            evidence_id=item.evidence_id,
            locator={"kind": item.locator.kind, "value": item.locator.value},
        )
        for item in projection.evidence
    )


def _read_contexts(
    projection: ProjectionRetrievalResult,
) -> tuple[RetrievalReadContext, ...]:
    contexts = list(projection.read_contexts)
    if projection.read_context is not None:
        contexts.append(projection.read_context)
    for item in (*projection.chunks, *projection.entities, *projection.relations):
        contexts.extend(item.read_contexts)
        if item.read_context is not None:
            contexts.append(item.read_context)
    return tuple(dict.fromkeys(contexts))
