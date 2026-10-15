"""Broker-side safety gates for one structured retrieval attempt.

The gate service consumes authorities that were resolved elsewhere.  It never
selects a publication, grants access, or interprets provider payloads.  Its
job is to make the final retrieval disposition explicit before a bundle can be
issued: a stale or degraded publication cannot become a healthy success, and
only complete, verified evidence can cross the allowed/partial boundary.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal
from uuid import UUID

from pydantic import model_validator

from spine.domain.common import DefinitionModel
from spine.domain.context_bundles import PolicyDecisionGeneration
from spine.domain.context_profiles import PartialAnswerFallback
from spine.domain.projections import (
    ProjectionFreshness,
    ProjectionHealth,
    ProjectionReconciliationOutcome,
    PublishedActivePublication,
    UnavailableActivePublication,
    UnpublishedActivePublication,
)
from spine.memory.contracts import (
    ContextOutcome,
    ContextReasonCode,
    EvidenceReference,
    PublicationPin,
    ProjectionChunk,
    ProjectionEntity,
    ProjectionOutcome,
    ProjectionRelation,
    ProjectionRetrievalResult,
)

from spine.application.context_profiles import EffectiveContextRetrievalPolicy
from spine.application.evidence_lineage_verification import (
    EvidenceItem,
    EvidenceVerificationOutcome,
    EvidenceVerificationReasonCode,
    RejectedEvidenceItem,
    VerifiedEvidenceItem,
)


PublicationResolution = (
    PublishedActivePublication
    | UnpublishedActivePublication
    | UnavailableActivePublication
)


class RetrievalGateReason(str, Enum):
    """Internal, provider-neutral classification used by the gate result."""

    ALLOWED = "allowed"
    DENIED = "denied"
    UNAVAILABLE = "unavailable"
    STALE = "stale"
    DEGRADED = "degraded"
    PARTIAL = "partial"
    INSUFFICIENT = "insufficient"
    ABSTAINED = "abstained"
    CANCELLED = "cancelled"


class RetrievalGateRequest(DefinitionModel):
    """All independently resolved inputs required for one gate evaluation."""

    schema_version: Literal["retrieval-gate-request:v1"] = (
        "retrieval-gate-request:v1"
    )
    workspace_id: UUID
    environment_id: UUID
    profile: EffectiveContextRetrievalPolicy
    publication: PublicationResolution
    retrieval: ProjectionRetrievalResult
    evidence_verifications: tuple[EvidenceVerificationOutcome, ...] = ()
    policy_decision_generations: tuple[PolicyDecisionGeneration, ...] = ()
    cancelled: bool = False

    @model_validator(mode="after")
    def validate_scope(self) -> "RetrievalGateRequest":
        if self.workspace_id.int == 0 or self.environment_id.int == 0:
            raise ValueError("retrieval gate scope IDs must be non-zero")
        if (
            self.profile.workspace_id != self.workspace_id
            or self.profile.environment_id != self.environment_id
        ):
            raise ValueError("retrieval gate profile scope does not match request")
        if (
            self.publication.workspace_id != self.workspace_id
            or self.publication.environment_id != self.environment_id
        ):
            raise ValueError("retrieval gate publication scope does not match request")
        return self


class RetrievalGateDecision(DefinitionModel):
    """Disclosure-safe disposition and lineage for one retrieval attempt."""

    schema_version: Literal["retrieval-gate-decision:v1"] = (
        "retrieval-gate-decision:v1"
    )
    outcome: ContextOutcome
    reason_code: ContextReasonCode
    gate_reason: RetrievalGateReason
    publication_pin: PublicationPin | None = None
    evidence_references: tuple[EvidenceReference, ...] = ()
    policy_decision_generations: tuple[PolicyDecisionGeneration, ...] = ()
    authorization_decision_references: tuple[UUID, ...] = ()
    bundle_eligible: bool = False

    @model_validator(mode="after")
    def validate_decision(self) -> "RetrievalGateDecision":
        allowed_reasons = {
            ContextOutcome.ALLOWED: ContextReasonCode.ALLOWED,
            ContextOutcome.DENIED: ContextReasonCode.AUTHORIZATION_DENIED,
            ContextOutcome.UNAVAILABLE: {
                ContextReasonCode.RETRIEVAL_UNAVAILABLE,
                ContextReasonCode.PUBLICATION_UNAVAILABLE,
            },
            ContextOutcome.STALE: ContextReasonCode.STALE_CONTEXT,
            ContextOutcome.DEGRADED: ContextReasonCode.DEGRADED_CONTEXT,
            ContextOutcome.PARTIAL: ContextReasonCode.PARTIAL_CONTEXT,
            ContextOutcome.INSUFFICIENT: ContextReasonCode.INSUFFICIENT_EVIDENCE,
            ContextOutcome.ABSTAINED: ContextReasonCode.INSUFFICIENT_EVIDENCE,
            ContextOutcome.CANCELLED: ContextReasonCode.CANCELLED,
        }
        expected = allowed_reasons[self.outcome]
        if self.reason_code not in (
            expected if isinstance(expected, set) else {expected}
        ):
            raise ValueError("gate reason code does not match outcome")
        if self.outcome in {ContextOutcome.ALLOWED, ContextOutcome.PARTIAL}:
            if (
                self.publication_pin is None
                or not self.evidence_references
                or not self.policy_decision_generations
            ):
                raise ValueError("successful gate decisions require pinned evidence")
            if not self.bundle_eligible:
                raise ValueError("bundle outcomes must be bundle eligible")
        elif self.bundle_eligible or self.evidence_references:
            raise ValueError("rejected gate decisions cannot carry evidence")
        for reference in self.authorization_decision_references:
            if reference.int == 0:
                raise ValueError("authorization decision references must be non-zero")
        return self


class RetrievalGateService:
    """Evaluate publication, profile, provider and evidence gates fail-closed."""

    def evaluate(self, request: RetrievalGateRequest) -> RetrievalGateDecision:
        """Return a safe typed result without exposing provider diagnostics."""

        try:
            return self._evaluate(request)
        except Exception:
            return self._failure(
                ContextOutcome.UNAVAILABLE,
                RetrievalGateReason.UNAVAILABLE,
                ContextReasonCode.RETRIEVAL_UNAVAILABLE,
            )

    def _evaluate(self, request: RetrievalGateRequest) -> RetrievalGateDecision:
        if request.cancelled or request.retrieval.outcome is ProjectionOutcome.CANCELLED:
            return self._failure(
                ContextOutcome.CANCELLED,
                RetrievalGateReason.CANCELLED,
                ContextReasonCode.CANCELLED,
            )

        if not isinstance(request.publication, PublishedActivePublication):
            return self._failure(
                ContextOutcome.UNAVAILABLE,
                RetrievalGateReason.UNAVAILABLE,
                ContextReasonCode.PUBLICATION_UNAVAILABLE,
            )

        try:
            publication_pin = PublicationPin.from_publication(request.publication)
        except Exception:
            return self._failure(
                ContextOutcome.UNAVAILABLE,
                RetrievalGateReason.UNAVAILABLE,
                ContextReasonCode.PUBLICATION_UNAVAILABLE,
            )

        publication_failure = self._publication_failure(request.publication)
        if publication_failure is not None:
            outcome, gate_reason, reason = publication_failure
            return self._failure(outcome, gate_reason, reason, publication_pin)

        retrieval_failure = self._retrieval_failure(request.retrieval)
        if retrieval_failure is not None:
            return self._failure(*retrieval_failure, publication_pin)

        items = _retrieval_items(request.retrieval)
        if request.retrieval.retrieval_strategy != request.profile.retrieval_strategy.value:
            return self._failure(
                ContextOutcome.ABSTAINED,
                RetrievalGateReason.ABSTAINED,
                ContextReasonCode.INSUFFICIENT_EVIDENCE,
                publication_pin,
            )
        if not items or len(items) > request.profile.max_results:
            return self._failure(
                ContextOutcome.INSUFFICIENT,
                RetrievalGateReason.INSUFFICIENT,
                ContextReasonCode.INSUFFICIENT_EVIDENCE,
                publication_pin,
            )

        evidence_failure = self._evidence_failure(
            request.retrieval,
            request.evidence_verifications,
            request.policy_decision_generations,
            publication_pin,
            request.workspace_id,
            request.environment_id,
        )
        if evidence_failure is not None:
            return self._failure(*evidence_failure, publication_pin)

        evidence_references = _safe_evidence_references(items)
        decision_references = tuple(
            verification.authorization_decision_reference
            for verification in request.evidence_verifications
            if isinstance(verification, VerifiedEvidenceItem)
        )
        is_partial = _is_partial(request.retrieval)
        if is_partial:
            if request.profile.partial_answer_fallback is not PartialAnswerFallback.ALLOWED:
                return self._failure(
                    ContextOutcome.INSUFFICIENT,
                    RetrievalGateReason.INSUFFICIENT,
                    ContextReasonCode.INSUFFICIENT_EVIDENCE,
                    publication_pin,
                )
            return RetrievalGateDecision(
                outcome=ContextOutcome.PARTIAL,
                reason_code=ContextReasonCode.PARTIAL_CONTEXT,
                gate_reason=RetrievalGateReason.PARTIAL,
                publication_pin=publication_pin,
                evidence_references=evidence_references,
                policy_decision_generations=request.policy_decision_generations,
                authorization_decision_references=decision_references,
                bundle_eligible=True,
            )
        return RetrievalGateDecision(
            outcome=ContextOutcome.ALLOWED,
            reason_code=ContextReasonCode.ALLOWED,
            gate_reason=RetrievalGateReason.ALLOWED,
            publication_pin=publication_pin,
            evidence_references=evidence_references,
            policy_decision_generations=request.policy_decision_generations,
            authorization_decision_references=decision_references,
            bundle_eligible=True,
        )

    @staticmethod
    def _publication_failure(
        publication: PublishedActivePublication,
    ) -> tuple[ContextOutcome, RetrievalGateReason, ContextReasonCode] | None:
        if publication.freshness is ProjectionFreshness.STALE:
            return (
                ContextOutcome.STALE,
                RetrievalGateReason.STALE,
                ContextReasonCode.STALE_CONTEXT,
            )
        if publication.health is ProjectionHealth.DEGRADED:
            return (
                ContextOutcome.DEGRADED,
                RetrievalGateReason.DEGRADED,
                ContextReasonCode.DEGRADED_CONTEXT,
            )
        reconciliation = publication.effective_reconciliation
        if reconciliation is None:
            return (
                ContextOutcome.UNAVAILABLE,
                RetrievalGateReason.UNAVAILABLE,
                ContextReasonCode.PUBLICATION_UNAVAILABLE,
            )
        if reconciliation.outcome is not ProjectionReconciliationOutcome.HEALTHY:
            return (
                ContextOutcome.DEGRADED,
                RetrievalGateReason.DEGRADED,
                ContextReasonCode.DEGRADED_CONTEXT,
            )
        return None

    @staticmethod
    def _retrieval_failure(
        retrieval: ProjectionRetrievalResult,
    ) -> tuple[ContextOutcome, RetrievalGateReason, ContextReasonCode] | None:
        if retrieval.outcome in {
            ProjectionOutcome.SUCCEEDED,
            ProjectionOutcome.REPLAYED,
            ProjectionOutcome.PARTIAL,
        }:
            return None
        if retrieval.outcome is ProjectionOutcome.CANCELLED:
            return (
                ContextOutcome.CANCELLED,
                RetrievalGateReason.CANCELLED,
                ContextReasonCode.CANCELLED,
            )
        if retrieval.outcome in {
            ProjectionOutcome.MISSING,
            ProjectionOutcome.STALE,
            ProjectionOutcome.TOMBSTONED,
            ProjectionOutcome.AMBIGUOUS,
            ProjectionOutcome.INCOMPLETE,
        }:
            return (
                ContextOutcome.INSUFFICIENT,
                RetrievalGateReason.INSUFFICIENT,
                ContextReasonCode.INSUFFICIENT_EVIDENCE,
            )
        return (
            ContextOutcome.UNAVAILABLE,
            RetrievalGateReason.UNAVAILABLE,
            ContextReasonCode.RETRIEVAL_UNAVAILABLE,
        )

    @staticmethod
    def _evidence_failure(
        retrieval: ProjectionRetrievalResult,
        verifications: tuple[EvidenceVerificationOutcome, ...],
        policy_decision_generations: tuple[PolicyDecisionGeneration, ...],
        publication_pin: PublicationPin,
        workspace_id: UUID,
        environment_id: UUID,
    ) -> tuple[ContextOutcome, RetrievalGateReason, ContextReasonCode] | None:
        rejected = tuple(
            result for result in verifications if isinstance(result, RejectedEvidenceItem)
        )
        if rejected:
            if any(
                result.reason_code is EvidenceVerificationReasonCode.AUTHORIZATION_DENIED
                for result in rejected
            ):
                return (
                    ContextOutcome.DENIED,
                    RetrievalGateReason.DENIED,
                    ContextReasonCode.AUTHORIZATION_DENIED,
                )
            if any(
                result.reason_code
                is EvidenceVerificationReasonCode.VERIFICATION_UNAVAILABLE
                for result in rejected
            ):
                return (
                    ContextOutcome.UNAVAILABLE,
                    RetrievalGateReason.UNAVAILABLE,
                    ContextReasonCode.RETRIEVAL_UNAVAILABLE,
                )
            return (
                ContextOutcome.ABSTAINED,
                RetrievalGateReason.ABSTAINED,
                ContextReasonCode.INSUFFICIENT_EVIDENCE,
            )

        verified = tuple(
            result for result in verifications if isinstance(result, VerifiedEvidenceItem)
        )
        items = _retrieval_items(retrieval)
        expected = {_item_key(item): item for item in items}
        actual = {_item_key(result.item): result for result in verified}
        if len(actual) != len(verified) or set(actual) != set(expected):
            return (
                ContextOutcome.ABSTAINED,
                RetrievalGateReason.ABSTAINED,
                ContextReasonCode.INSUFFICIENT_EVIDENCE,
            )
        if any(
            result.publication_pin_id != publication_pin.pin_id
            or result.item != expected[key]
            for key, result in actual.items()
        ):
            return (
                ContextOutcome.ABSTAINED,
                RetrievalGateReason.ABSTAINED,
                ContextReasonCode.INSUFFICIENT_EVIDENCE,
            )
        if not _policy_generations_match(items, verified, policy_decision_generations):
            return (
                ContextOutcome.ABSTAINED,
                RetrievalGateReason.ABSTAINED,
                ContextReasonCode.INSUFFICIENT_EVIDENCE,
            )
        references = tuple(
            reference for item in items for reference in item.evidence
        )
        if any(
            reference.workspace_id != workspace_id
            or reference.environment_id != environment_id
            or reference.status != "valid"
            or reference.revision_digest is None
            or reference.processing_receipt_id is None
            or reference.locator_contract_version is None
            for reference in references
        ):
            return (
                ContextOutcome.ABSTAINED,
                RetrievalGateReason.ABSTAINED,
                ContextReasonCode.INSUFFICIENT_EVIDENCE,
            )
        expected_refs = {reference.evidence_id: reference for reference in references}
        returned_refs = {reference.evidence_id: reference for reference in retrieval.evidence}
        if (
            len(expected_refs) != len(references)
            or len(returned_refs) != len(retrieval.evidence)
            or expected_refs != returned_refs
        ):
            return (
                ContextOutcome.ABSTAINED,
                RetrievalGateReason.ABSTAINED,
                ContextReasonCode.INSUFFICIENT_EVIDENCE,
            )
        return None

    @staticmethod
    def _failure(
        outcome: ContextOutcome,
        gate_reason: RetrievalGateReason,
        reason_code: ContextReasonCode,
        publication_pin: PublicationPin | None = None,
    ) -> RetrievalGateDecision:
        return RetrievalGateDecision(
            outcome=outcome,
            reason_code=reason_code,
            gate_reason=gate_reason,
            publication_pin=publication_pin,
        )


def _retrieval_items(
    retrieval: ProjectionRetrievalResult,
) -> tuple[EvidenceItem, ...]:
    return (*retrieval.chunks, *retrieval.entities, *retrieval.relations)


def _item_key(item: EvidenceItem) -> tuple[str, str]:
    if isinstance(item, ProjectionChunk):
        return "chunk", item.chunk_id
    if isinstance(item, ProjectionEntity):
        return "entity", item.entity_id
    if isinstance(item, ProjectionRelation):
        return "relation", item.relation_id
    raise TypeError("unsupported retrieval item")


def _safe_evidence_references(items: tuple[EvidenceItem, ...]) -> tuple[EvidenceReference, ...]:
    by_id = {
        reference.evidence_id: reference
        for item in items
        for reference in item.evidence
    }
    return tuple(by_id[key] for key in sorted(by_id))


def _policy_generations_match(
    items: tuple[EvidenceItem, ...],
    verified: tuple[VerifiedEvidenceItem, ...],
    generations: tuple[PolicyDecisionGeneration, ...],
) -> bool:
    source_ids = {
        reference.source_object_id
        for item in items
        for reference in item.evidence
    }
    generations_by_source = {
        generation.source_object_id: generation for generation in generations
    }
    if (
        len(generations_by_source) != len(generations)
        or set(generations_by_source) != source_ids
    ):
        return False
    for generation in generations:
        matching = tuple(
            result
            for result in verified
            if any(
                reference.source_object_id == generation.source_object_id
                for reference in result.item.evidence
            )
        )
        if not matching or any(
            result.authorization_decision_reference != generation.decision_reference
            for result in matching
        ):
            return False
    return True


def _is_partial(retrieval: ProjectionRetrievalResult) -> bool:
    return retrieval.outcome is ProjectionOutcome.PARTIAL or bool(
        retrieval.partial_failures
    )


__all__ = [
    "PublicationResolution",
    "RetrievalGateDecision",
    "RetrievalGateReason",
    "RetrievalGateRequest",
    "RetrievalGateService",
]
