"""Production Q&A handoff to the authorized Context Broker.

The answer capability receives a broker-issued, immutable ``ContextBundle``;
it never constructs a bundle and never knows how a physical projection is
read.  This module is intentionally limited to the composition seam between
the capability and the existing broker/application ports.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import ConfigDict, Field, model_validator

from spine.application.answer_disclosure import (
    AnswerDisclosureFacts,
    AnswerDisclosureResult,
    eligibility_from_revalidation,
)
from spine.application.answer_outcomes import AnswerOutcomeFacts, resolve_answer_outcome
from spine.application.authorized_context_broker import (
    AuthorizedContextRequest,
    ContextBundleRevalidator,
    SavedContextBundlePort,
)
from spine.application.diagnostics import DiagnosticContext
from spine.domain.capabilities import (
    AnswerAuthorizationOutcome,
    AnswerQuestionCandidateInput,
    AnswerQuestionInput,
    AnswerQuestionOutput,
    AnswerQuestionOutcome,
    AnswerQuestionSafeReason,
    AnswerQuestionSafeReasonCode,
    AnswerQuestionExecutionPin,
    RetrievalOutcome,
)
from spine.domain.context_bundles import (
    ContextBundle,
    RevalidationCheckpoint,
    RevalidationCheckpointResult,
    RevalidationDisposition,
    RevalidationFailureCode,
    RevalidationRequest,
)
from spine.domain.capabilities.answer_disclosure import (
    AnswerDisclosureRecord,
    AnswerExecutionCheckpoint,
    CurrentDisclosureEligibility,
)
from spine.domain.capabilities import AcceptedAnswerPresentation
from spine.domain.common import ValidatedDefinitionModel
from spine.domain.disclosure import (
    DisclosureDependencySet,
    DisclosureRevalidationCheckpoint,
    DisclosureRevalidationRequest,
    DisclosureRevalidationResult,
)
from spine.memory.contracts import ContextOutcome, ContextResponse


class AnswerQuestionContextBroker(Protocol):
    """The only retrieval port visible to the answer capability."""

    async def retrieve(self, request: AuthorizedContextRequest) -> ContextResponse: ...


class AnswerQuestionDisclosureRevalidator(Protocol):
    """Optional current-authority check for disclosure dependencies."""

    async def revalidate(
        self, request: DisclosureRevalidationRequest
    ) -> DisclosureRevalidationResult: ...


class AnswerQuestionDisclosureGate(Protocol):
    """The existing AQ-11 gate used after all candidate validators finish."""

    async def disclose_facts(self, facts: AnswerDisclosureFacts) -> AnswerDisclosureResult: ...

    async def prepare_facts(
        self,
        facts: AnswerDisclosureFacts,
        *,
        execution_checkpoint: AnswerExecutionCheckpoint | None = None,
    ) -> AnswerDisclosureResult: ...

    async def deliver_committed(
        self,
        record: AnswerDisclosureRecord,
        presentation: AcceptedAnswerPresentation,
    ) -> UUID | None: ...


class ContextRevalidationFailure(str, Enum):
    INVALID_RESPONSE = "invalid_response"
    BUNDLE_UNAVAILABLE = "bundle_unavailable"
    BUNDLE_LINEAGE_INVALID = "bundle_lineage_invalid"
    DEGRADED_CONTEXT = "degraded_context"
    REVALIDATION_UNAVAILABLE = "revalidation_unavailable"


class AnswerQuestionContextRequest(ValidatedDefinitionModel):
    """Server-routed input for one Q&A broker handoff.

    The execution pin is an already resolved server identity.  It is used to
    derive the broker profile and projection expectations; no projection,
    namespace, activation, boundary, candidate or policy selector is accepted
    from this request.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")

    question: AnswerQuestionInput
    execution_pin: AnswerQuestionExecutionPin
    request_id: UUID
    trusted_request_context_ref: UUID
    authorization_context_ref: UUID
    idempotency_key: str = Field(min_length=1, max_length=256)
    cancelled: bool = False

    @model_validator(mode="after")
    def validate_scope_and_lineage(self) -> "AnswerQuestionContextRequest":
        scope = self.question.scope
        if (
            self.execution_pin.workspace_id != scope.workspace_id
            or self.execution_pin.environment_id != scope.environment_id
        ):
            raise ValueError("answer question context request has a different scope")
        for name in (
            "request_id",
            "trusted_request_context_ref",
            "authorization_context_ref",
        ):
            if getattr(self, name).int == 0:
                raise ValueError(f"{name} must be non-zero")
        if self.question.correlation.run_id.int == 0 or self.question.correlation.attempt_id.int == 0:
            raise ValueError("answer question correlation is invalid")
        return self


@dataclass(frozen=True, slots=True)
class AnswerQuestionAuthorizedContext:
    """Exact broker handoff retained for candidate and disclosure stages."""

    question: AnswerQuestionInput
    bundle: ContextBundle
    response: ContextResponse
    revalidation: RevalidationCheckpointResult
    disclosure_dependencies: DisclosureDependencySet

    @property
    def candidate_input(self) -> AnswerQuestionCandidateInput:
        """Build the only typed input accepted by the candidate handler."""

        return AnswerQuestionCandidateInput(
            question=self.question,
            context_bundle=self.bundle,
        )


@dataclass(frozen=True, slots=True)
class AnswerQuestionContextResolution:
    """Safe result of the broker handoff; terminal branches contain no bundle."""

    retrieval_outcome: RetrievalOutcome
    authorization: AnswerAuthorizationOutcome
    context: AnswerQuestionAuthorizedContext | None = None
    output: AnswerQuestionOutput | None = None
    failure: ContextRevalidationFailure | None = None


@dataclass(frozen=True, slots=True)
class AnswerQuestionDisclosureCheckpoint:
    """Typed pre-disclosure facts consumed by the final disclosure gate."""

    revalidation: RevalidationCheckpointResult
    eligibility: CurrentDisclosureEligibility
    disclosure_dependencies: DisclosureDependencySet


class AnswerQuestionContextService:
    """Connect ``answer_question`` to one production Context Broker contract."""

    def __init__(
        self,
        *,
        broker: AnswerQuestionContextBroker,
        bundles: SavedContextBundlePort,
        revalidator: ContextBundleRevalidator,
        disclosure_revalidator: AnswerQuestionDisclosureRevalidator | None = None,
    ) -> None:
        self._broker = broker
        self._bundles = bundles
        self._revalidator = revalidator
        self._disclosure_revalidator = disclosure_revalidator

    async def prepare(
        self, request: AnswerQuestionContextRequest
    ) -> AnswerQuestionContextResolution:
        """Retrieve, load and revalidate one exact bundle before generation."""

        if type(request) is not AnswerQuestionContextRequest:
            return self._failure(
                request.question,
                RetrievalOutcome.UNAVAILABLE,
                ContextRevalidationFailure.INVALID_RESPONSE,
            )

        broker_request = AuthorizedContextRequest(
            request_id=request.request_id,
            workspace_id=request.question.scope.workspace_id,
            environment_id=request.question.scope.environment_id,
            query=request.question.subject.text,
            profile_ref=request.execution_pin.context_profile.profile,
            trusted_request_context_ref=request.trusted_request_context_ref,
            authorization_context_ref=request.authorization_context_ref,
            run_id=request.question.correlation.run_id,
            attempt_id=request.question.correlation.attempt_id,
            idempotency_key=request.idempotency_key,
            diagnostic_context=DiagnosticContext(
                trace_id=request.question.correlation.trace_id,
                correlation_id=request.question.correlation.correlation_id,
                causation_id=request.question.correlation.causation_id,
            ),
            cancelled=request.cancelled,
        )
        try:
            response = await self._broker.retrieve(broker_request)
            if type(response) is not ContextResponse:
                raise ValueError("context response is invalid")
        except Exception:
            return self._failure(
                request.question,
                RetrievalOutcome.UNAVAILABLE,
                ContextRevalidationFailure.INVALID_RESPONSE,
            )

        retrieval_outcome = map_context_outcome_to_retrieval_outcome(response.outcome)
        if response.outcome is ContextOutcome.DENIED:
            return self._terminal(
                request.question,
                retrieval_outcome,
                AnswerAuthorizationOutcome.DENIED,
                AnswerQuestionOutcome.DENIED,
                AnswerQuestionSafeReasonCode.AUTHORIZATION_DENIED,
            )
        if response.outcome is ContextOutcome.INSUFFICIENT or response.outcome is ContextOutcome.ABSTAINED:
            return self._terminal(
                request.question,
                retrieval_outcome,
                AnswerAuthorizationOutcome.ALLOWED,
                AnswerQuestionOutcome.ABSTAINED,
                AnswerQuestionSafeReasonCode.NO_USABLE_EVIDENCE,
            )
        if response.outcome is ContextOutcome.STALE:
            return self._terminal(
                request.question,
                retrieval_outcome,
                AnswerAuthorizationOutcome.ALLOWED,
                AnswerQuestionOutcome.ABSTAINED,
                AnswerQuestionSafeReasonCode.UNMET_FRESHNESS,
            )
        if response.outcome is ContextOutcome.DEGRADED:
            return self._failure(
                request.question,
                retrieval_outcome,
                ContextRevalidationFailure.DEGRADED_CONTEXT,
            )
        if response.outcome is ContextOutcome.CANCELLED or request.cancelled:
            return self._terminal(
                request.question,
                retrieval_outcome,
                AnswerAuthorizationOutcome.ALLOWED,
                AnswerQuestionOutcome.CANCELLED,
                AnswerQuestionSafeReasonCode.CANCELLED,
            )
        if response.context_bundle_ref is None:
            return self._failure(
                request.question,
                retrieval_outcome,
                ContextRevalidationFailure.INVALID_RESPONSE,
            )

        try:
            bundle = await self._bundles.load(
                workspace_id=request.question.scope.workspace_id,
                environment_id=request.question.scope.environment_id,
                reference=response.context_bundle_ref,
            )
        except Exception:
            return self._failure(
                request.question,
                retrieval_outcome,
                ContextRevalidationFailure.BUNDLE_UNAVAILABLE,
            )
        try:
            self._validate_bundle(request, response, bundle)
        except Exception:
            return self._failure(
                request.question,
                retrieval_outcome,
                ContextRevalidationFailure.BUNDLE_LINEAGE_INVALID,
            )

        try:
            checkpoint = await self._revalidate(
                bundle=bundle,
                request=request,
                checkpoint=RevalidationCheckpoint.BEFORE_EVIDENCE_REUSE,
            )
        except Exception:
            return self._failure(
                request.question,
                retrieval_outcome,
                ContextRevalidationFailure.REVALIDATION_UNAVAILABLE,
            )
        if checkpoint.disposition is RevalidationDisposition.RETRYABLE_FAILURE:
            return self._failure(
                request.question,
                retrieval_outcome,
                ContextRevalidationFailure.REVALIDATION_UNAVAILABLE,
            )
        if checkpoint.disposition is not RevalidationDisposition.SAFE_REPLAY:
            return self._revalidation_terminal(request.question, retrieval_outcome, checkpoint)
        try:
            dependencies = DisclosureDependencySet.from_context_bundle(bundle)
            context = AnswerQuestionAuthorizedContext(
                question=request.question,
                bundle=bundle,
                response=response,
                revalidation=checkpoint,
                disclosure_dependencies=dependencies,
            )
            context.candidate_input
        except Exception:
            return self._terminal(
                request.question,
                retrieval_outcome,
                AnswerAuthorizationOutcome.ALLOWED,
                AnswerQuestionOutcome.ABSTAINED,
                AnswerQuestionSafeReasonCode.NO_USABLE_EVIDENCE,
            )
        return AnswerQuestionContextResolution(
            retrieval_outcome=retrieval_outcome,
            authorization=AnswerAuthorizationOutcome.ALLOWED,
            context=context,
        )

    async def before_final_disclosure(
        self,
        context: AnswerQuestionAuthorizedContext,
        *,
        requester_id: UUID,
        cancelled: bool = False,
    ) -> AnswerQuestionDisclosureCheckpoint:
        """Revalidate the same bundle and all disclosure dependencies."""

        request = RevalidationRequest(
            bundle=context.bundle,
            workspace_id=context.bundle.workspace_id,
            environment_id=context.bundle.environment_id,
            checkpoint=RevalidationCheckpoint.BEFORE_FINAL_DISCLOSURE,
            idempotency_key=self._checkpoint_key(context.bundle, RevalidationCheckpoint.BEFORE_FINAL_DISCLOSURE),
            cancelled=cancelled,
        )
        try:
            result = await self._revalidator.revalidate(request)
        except Exception:
            result = RevalidationCheckpointResult(
                disposition=RevalidationDisposition.RETRYABLE_FAILURE,
                checkpoint=request.checkpoint,
                workspace_id=request.workspace_id,
                environment_id=request.environment_id,
                idempotency_key=request.idempotency_key,
                command_digest=request.command_digest,
                failure_codes=(RevalidationFailureCode.REVALIDATION_UNAVAILABLE,),
                retryable=True,
                retry_attempt=request.retry_attempt,
            )
        eligibility = self._eligibility_from_attempt_result(
            context,
            result,
            requester_id=requester_id,
            cancelled=cancelled,
        )
        if self._disclosure_revalidator is not None and result.safe_to_reuse_bundle:
            disclosure_request = DisclosureRevalidationRequest(
                request_id=uuid5(
                    NAMESPACE_URL,
                    f"spine:answer-question:disclosure:{context.bundle.bundle_id}:"
                    f"{context.bundle.bundle_version}:{requester_id}",
                ),
                workspace_id=context.bundle.workspace_id,
                environment_id=context.bundle.environment_id,
                requester_id=requester_id,
                checkpoint=DisclosureRevalidationCheckpoint.BEFORE_STORED_ANSWER_READ,
                dependencies=context.disclosure_dependencies,
                run_id=context.bundle.run_id,
                attempt_id=context.bundle.attempt_id,
                cancelled=cancelled,
            )
            try:
                disclosure_result = await self._disclosure_revalidator.revalidate(
                    disclosure_request
                )
                disclosure_eligibility = eligibility_from_revalidation(disclosure_result)
                eligibility = disclosure_eligibility
            except Exception:
                eligibility = self._unavailable_eligibility(context, requester_id)
        return AnswerQuestionDisclosureCheckpoint(
            revalidation=result,
            eligibility=eligibility,
            disclosure_dependencies=context.disclosure_dependencies,
        )

    async def disclose(
        self,
        *,
        context: AnswerQuestionAuthorizedContext,
        outcome_facts: AnswerOutcomeFacts,
        request_id: UUID,
        requester_id: UUID,
        disclosure_gate: AnswerQuestionDisclosureGate,
        cancelled: bool = False,
        defer_delivery: bool = False,
        execution_checkpoint: AnswerExecutionCheckpoint | None = None,
        safe_terminal_output: AnswerQuestionOutput | None = None,
    ) -> AnswerDisclosureResult:
        """Run AQ-11 disclosure only after the same bundle is revalidated.

        Candidate generation and its citation/semantic/evaluation validators
        remain owned by their existing application stages.  This method only
        binds their typed facts to this exact Context Bundle, performs the
        current-authority checkpoint, and delegates the final decision to the
        existing fail-closed ``AnswerDisclosureGate``.
        """

        if outcome_facts.question != context.question:
            raise ValueError("answer outcome facts belong to another question")
        if outcome_facts.candidate is not None:
            if outcome_facts.candidate.context_bundle != context.bundle.lineage_reference:
                raise ValueError("answer candidate belongs to another context bundle")
        checkpoint = await self.before_final_disclosure(
            context,
            requester_id=requester_id,
            cancelled=cancelled,
        )
        if checkpoint.revalidation.disposition is RevalidationDisposition.RETRYABLE_FAILURE:
            raise RuntimeError("answer disclosure revalidation is temporarily unavailable")
        final_facts = replace(
            outcome_facts,
            disclosure_eligible=checkpoint.eligibility.can_disclose,
            cancellation_requested=outcome_facts.cancellation_requested or cancelled,
        )
        facts = AnswerDisclosureFacts(
            request_id=request_id,
            outcome_facts=final_facts,
            current_eligibility=checkpoint.eligibility,
            safe_terminal_output=safe_terminal_output,
        )
        if defer_delivery:
            return await disclosure_gate.prepare_facts(
                facts, execution_checkpoint=execution_checkpoint
            )
        return await disclosure_gate.disclose_facts(facts)

    async def prepare_terminal_disclosure(
        self,
        *,
        question: AnswerQuestionInput,
        retrieval_outcome: RetrievalOutcome,
        authorization: AnswerAuthorizationOutcome,
        request_id: UUID,
        disclosure_gate: AnswerQuestionDisclosureGate,
        cancelled: bool = False,
        execution_checkpoint: AnswerExecutionCheckpoint | None = None,
    ) -> AnswerDisclosureResult:
        """Persist a safe early terminal output without evidence fabrication."""

        facts = AnswerDisclosureFacts(
            request_id=request_id,
            outcome_facts=AnswerOutcomeFacts(
                question=question,
                retrieval_outcome=retrieval_outcome,
                authorization=authorization,
                cancellation_requested=cancelled,
            ),
        )
        return await disclosure_gate.prepare_facts(
            facts, execution_checkpoint=execution_checkpoint
        )

    async def _revalidate(
        self,
        *,
        bundle: ContextBundle,
        request: AnswerQuestionContextRequest,
        checkpoint: RevalidationCheckpoint,
    ) -> RevalidationCheckpointResult:
        return await self._revalidator.revalidate(
            RevalidationRequest(
                bundle=bundle,
                workspace_id=bundle.workspace_id,
                environment_id=bundle.environment_id,
                checkpoint=checkpoint,
                idempotency_key=self._checkpoint_key(bundle, checkpoint),
                cancelled=request.cancelled,
            )
        )

    @staticmethod
    def _checkpoint_key(bundle: ContextBundle, checkpoint: RevalidationCheckpoint) -> str:
        return f"answer-question:{bundle.run_id}:{bundle.attempt_id}:{checkpoint.value}"

    @staticmethod
    def _validate_bundle(
        request: AnswerQuestionContextRequest,
        response: ContextResponse,
        bundle: ContextBundle,
    ) -> None:
        question = request.question
        pin = request.execution_pin
        projection = pin.projection
        if (
            bundle.reference != response.context_bundle_ref
            or bundle.workspace_id != question.scope.workspace_id
            or bundle.environment_id != question.scope.environment_id
            or bundle.request_id != request.request_id
            or bundle.run_id != question.correlation.run_id
            or bundle.attempt_id != question.correlation.attempt_id
            or bundle.idempotency_key != request.idempotency_key
            or bundle.profile_ref != pin.context_profile.profile
            or bundle.trusted_request_context_ref != request.trusted_request_context_ref
            or bundle.authorization_context_ref != request.authorization_context_ref
            or bundle.publication_pin_id != response.publication_pin_id
            or bundle.outcome.value != response.outcome.value
            or bundle.projection_snapshot.projection_snapshot_id != projection.projection_snapshot_id
            or bundle.projection_snapshot.manifest_digest != projection.snapshot_manifest_digest
            or bundle.projection_activation.projection_activation_id != projection.projection_activation_id
            or bundle.activation_generation != projection.activation_generation
            or bundle.projection_config_version.projection_config_version_id != projection.projection_config_version_id
            or bundle.projection_config_version.config_digest != projection.projection_config_digest
            or bundle.canonical_boundary.boundary_id != projection.canonical_boundary_id
            or bundle.canonical_boundary.manifest_digest != projection.canonical_boundary_digest
            or bundle.canonical_as_of.canonical_boundary_id != projection.canonical_boundary_id
        ):
            raise ValueError("context bundle lineage is not the trusted execution pin")

    @staticmethod
    def _terminal(
        question: AnswerQuestionInput,
        retrieval_outcome: RetrievalOutcome,
        authorization: AnswerAuthorizationOutcome,
        outcome: AnswerQuestionOutcome,
        reason: AnswerQuestionSafeReasonCode,
    ) -> AnswerQuestionContextResolution:
        resolved = resolve_answer_outcome(
            AnswerOutcomeFacts(
                question=question,
                retrieval_outcome=retrieval_outcome,
                authorization=authorization,
                cancellation_requested=outcome is AnswerQuestionOutcome.CANCELLED,
            )
        )
        if resolved.output is None:
            raise ValueError("safe terminal outcome did not produce an output")
        output = resolved.output.model_copy(
            update={"outcome": outcome, "reason": AnswerQuestionSafeReason(code=reason)}
        )
        return AnswerQuestionContextResolution(
            retrieval_outcome=retrieval_outcome,
            authorization=authorization,
            output=output,
        )

    @classmethod
    def _failure(
        cls,
        question: AnswerQuestionInput,
        retrieval_outcome: RetrievalOutcome,
        failure: ContextRevalidationFailure,
    ) -> AnswerQuestionContextResolution:
        return AnswerQuestionContextResolution(
            retrieval_outcome=retrieval_outcome,
            authorization=AnswerAuthorizationOutcome.ALLOWED,
            output=cls._terminal(
                question,
                retrieval_outcome,
                AnswerAuthorizationOutcome.ALLOWED,
                AnswerQuestionOutcome.FAILED,
                AnswerQuestionSafeReasonCode.MANDATORY_PROCESSING_FAILED,
            ).output,
            failure=failure,
        )

    @classmethod
    def _revalidation_terminal(
        cls,
        question: AnswerQuestionInput,
        retrieval_outcome: RetrievalOutcome,
        result: RevalidationCheckpointResult,
    ) -> AnswerQuestionContextResolution:
        if result.disposition is RevalidationDisposition.CANCELLED:
            return cls._terminal(
                question,
                retrieval_outcome,
                AnswerAuthorizationOutcome.ALLOWED,
                AnswerQuestionOutcome.CANCELLED,
                AnswerQuestionSafeReasonCode.CANCELLED,
            )
        if any(
            code
            in {
                RevalidationFailureCode.CONTRIBUTOR_UNAUTHORIZED,
                RevalidationFailureCode.AUTHORITY_GENERATION_CHANGED,
            }
            for code in result.failure_codes
        ):
            return cls._terminal(
                question,
                retrieval_outcome,
                AnswerAuthorizationOutcome.DENIED,
                AnswerQuestionOutcome.DENIED,
                AnswerQuestionSafeReasonCode.AUTHORIZATION_DENIED,
            )
        if result.disposition is RevalidationDisposition.RETRYABLE_FAILURE:
            return cls._failure(
                question,
                retrieval_outcome,
                ContextRevalidationFailure.REVALIDATION_UNAVAILABLE,
            )
        return cls._terminal(
            question,
            retrieval_outcome,
            AnswerAuthorizationOutcome.ALLOWED,
            AnswerQuestionOutcome.ABSTAINED,
            AnswerQuestionSafeReasonCode.EVIDENCE_INVALIDATED,
        )

    @staticmethod
    def _eligibility_from_attempt_result(
        context: AnswerQuestionAuthorizedContext,
        result: RevalidationCheckpointResult,
        *,
        requester_id: UUID,
        cancelled: bool,
    ) -> CurrentDisclosureEligibility:
        validation_reference = uuid5(
            NAMESPACE_URL,
            f"spine:answer-question:attempt-disclosure:{context.bundle.bundle_id}:"
            f"{result.command_digest}:{requester_id}",
        )
        if result.safe_to_reuse_bundle:
            # Attempt revalidation proves that the immutable bundle can be
            # reused.  It is not a disclosure validation fact.  The optional
            # dependency revalidator below must still issue the eligible
            # result before the final disclosure gate can accept an answer.
            return AnswerQuestionContextService._unavailable_eligibility(
                context,
                requester_id,
                validation_reference=validation_reference,
            )
        if cancelled or result.disposition is RevalidationDisposition.CANCELLED:
            from spine.domain.capabilities.answer_disclosure import (
                DisclosureEligibilityReason,
                DisclosureEligibilityStatus,
            )

            return CurrentDisclosureEligibility(
                status=DisclosureEligibilityStatus.INELIGIBLE,
                validation_reference=validation_reference,
                reason=DisclosureEligibilityReason.CURRENT_AUTHORITY_REJECTED,
            )
        return AnswerQuestionContextService._unavailable_eligibility(
            context, requester_id, validation_reference=validation_reference
        )

    @staticmethod
    def _unavailable_eligibility(
        context: AnswerQuestionAuthorizedContext,
        requester_id: UUID,
        *,
        validation_reference: UUID | None = None,
    ) -> CurrentDisclosureEligibility:
        from spine.domain.capabilities.answer_disclosure import (
            DisclosureEligibilityReason,
            DisclosureEligibilityStatus,
        )

        return CurrentDisclosureEligibility(
            status=DisclosureEligibilityStatus.UNAVAILABLE,
            validation_reference=validation_reference
            or uuid5(NAMESPACE_URL, f"spine:answer-question:unavailable:{context.bundle.bundle_id}:{requester_id}"),
            reason=DisclosureEligibilityReason.CURRENT_AUTHORITY_UNAVAILABLE,
            workspace_id=context.bundle.workspace_id,
            environment_id=context.bundle.environment_id,
            run_id=context.bundle.run_id,
            attempt_id=context.bundle.attempt_id,
        )


def map_context_outcome_to_retrieval_outcome(
    outcome: ContextOutcome,
) -> RetrievalOutcome:
    """Map broker disposition to AQ retrieval facts, never to a final answer."""

    if outcome in {ContextOutcome.ALLOWED, ContextOutcome.PARTIAL}:
        return RetrievalOutcome.EVIDENCE_AVAILABLE
    if outcome in {ContextOutcome.INSUFFICIENT, ContextOutcome.ABSTAINED}:
        return RetrievalOutcome.INSUFFICIENT_EVIDENCE
    if outcome is ContextOutcome.DENIED:
        return RetrievalOutcome.FAILED
    return RetrievalOutcome.UNAVAILABLE


__all__ = [
    "AnswerQuestionAuthorizedContext",
    "AnswerQuestionContextBroker",
    "AnswerQuestionContextRequest",
    "AnswerQuestionContextResolution",
    "AnswerQuestionContextService",
    "AnswerQuestionDisclosureCheckpoint",
    "AnswerQuestionDisclosureGate",
    "AnswerQuestionDisclosureRevalidator",
    "ContextRevalidationFailure",
    "map_context_outcome_to_retrieval_outcome",
]
