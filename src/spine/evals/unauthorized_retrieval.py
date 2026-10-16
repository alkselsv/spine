"""Fail-closed R1 safety checks for unauthorized retrieval and disclosure.

This module is evaluation-plane code.  It observes the typed application-flow
contracts produced by G01 and does not reimplement authorization or retrieval.
Its only job is to prove that an observation cannot cross the disclosure
boundary with evidence that is denied, stale, out of scope, or untraceable.
"""

from __future__ import annotations

from enum import Enum
from typing import Final, Literal

from spine.domain.capabilities import AnswerQuestionOutcome
from spine.domain.common import DefinitionModel
from spine.domain.context_bundles import ContextBundleState
from spine.domain.context_bundles import ContextBundleEvidenceStatus
from spine.domain.projections import ProjectionActivationKind, ProjectionSnapshotState
from spine.domain.projections import ProjectionFreshness, ProjectionHealth
from spine.evals.production_contract import (
    ProductionContractCaseRequest,
    ProductionContractObservation,
    ProductionContractObservationError,
)
from spine.memory.contracts import ContextOutcome, ProjectionOutcome


UNAUTHORIZED_RETRIEVAL_SAFETY_SEAM: Final[str] = (
    "r1-unauthorized-retrieval-disclosure:v1"
)
UNAUTHORIZED_RETRIEVAL_SAFETY_VERSION: Final[str] = "v1"
UNAUTHORIZED_RETRIEVAL_SAFETY_CASE_IDS: Final[tuple[str, ...]] = (
    "authorized-current-evidence",
    "revoked-access",
    "stale-access-generation",
    "mixed-authorized-unauthorized-evidence",
    "cross-tenant-physical-lookup",
    "provider-output-before-authorization",
    "invalid-evidence-lineage",
    "unauthorized-source-existence-disclosure",
)


class ProductionContractSafetyViolation(str, Enum):
    CONTRACT_VIOLATION = "contract_violation"
    OBSERVATION_UNAVAILABLE = "observation_unavailable"


class ProductionContractSafetyResult(DefinitionModel):
    """Disclosure-safe result for one executed production-contract case."""

    schema_version: Literal[UNAUTHORIZED_RETRIEVAL_SAFETY_SEAM] = (
        UNAUTHORIZED_RETRIEVAL_SAFETY_SEAM
    )
    seam: Literal[UNAUTHORIZED_RETRIEVAL_SAFETY_SEAM] = (
        UNAUTHORIZED_RETRIEVAL_SAFETY_SEAM
    )
    case_id: str
    status: Literal["passed", "failed"]
    violation_codes: tuple[ProductionContractSafetyViolation, ...] = ()
    protected_payload_disclosed: bool = False


class ProductionContractSafetyGate:
    """Evaluate G01 observations without becoming a second application gate."""

    def evaluate(
        self,
        *,
        request: ProductionContractCaseRequest,
        observation: ProductionContractObservation,
    ) -> ProductionContractSafetyResult:
        violations: list[ProductionContractSafetyViolation] = []
        protected_payload_disclosed = False
        try:
            if not isinstance(observation, ProductionContractObservation):
                raise ProductionContractObservationError("observation is not typed")
            protected_payload_disclosed = _has_unauthorized_payload(observation)
            observation.validate()
            _validate_request_identity(request, observation)
            _validate_safety_disposition(observation)
        except Exception:
            # Error details intentionally do not cross this report boundary.
            violations.append(ProductionContractSafetyViolation.CONTRACT_VIOLATION)

        return ProductionContractSafetyResult(
            case_id=request.case_id,
            status="failed" if violations else "passed",
            violation_codes=tuple(dict.fromkeys(violations)),
            protected_payload_disclosed=protected_payload_disclosed,
        )

    @staticmethod
    def failed_without_observation(case_id: str) -> ProductionContractSafetyResult:
        """Record a safe failure when the application flow produced no observation."""

        return ProductionContractSafetyResult(
            case_id=case_id,
            status="failed",
            violation_codes=(ProductionContractSafetyViolation.OBSERVATION_UNAVAILABLE,),
        )


def evaluate_unauthorized_retrieval_disclosure(
    *,
    request: ProductionContractCaseRequest,
    observation: ProductionContractObservation,
) -> ProductionContractSafetyResult:
    """Functional entry point used by qualification tests and callers."""

    return ProductionContractSafetyGate().evaluate(
        request=request, observation=observation
    )


def _validate_request_identity(
    request: ProductionContractCaseRequest,
    observation: ProductionContractObservation,
) -> None:
    question = observation.question
    if question.subject.text != request.question:
        raise ProductionContractObservationError("question identity mismatch")
    if question.scope.workspace_id != request.workspace_id:
        raise ProductionContractObservationError("workspace identity mismatch")
    if question.scope.environment_id != request.environment_id:
        raise ProductionContractObservationError("environment identity mismatch")
    if observation.context_bundle is not None and any(
        reference.source_revision_id not in request.available_revision_ids
        for reference in observation.context_bundle.evidence_references
    ):
        raise ProductionContractObservationError("source revision is not available")


def _validate_safety_disposition(observation: ProductionContractObservation) -> None:
    response = observation.context_response
    bundle = observation.context_bundle
    if response.outcome in {ContextOutcome.ALLOWED, ContextOutcome.PARTIAL}:
        if bundle is None:
            raise ProductionContractObservationError("allowed response has no bundle")
        if bundle.outcome.value != response.outcome.value:
            raise ProductionContractObservationError("response and bundle outcomes differ")
        if bundle.freshness is not ProjectionFreshness.CURRENT:
            raise ProductionContractObservationError("bundle is stale")
        if bundle.health is not ProjectionHealth.HEALTHY:
            raise ProductionContractObservationError("bundle is degraded")
        if bundle.state not in {ContextBundleState.COMPLETE, ContextBundleState.PARTIAL}:
            raise ProductionContractObservationError("bundle state is not disclosable")
        if bundle.projection_snapshot.snapshot_state is not ProjectionSnapshotState.FINALIZED:
            raise ProductionContractObservationError("snapshot is not finalized")
        if bundle.projection_activation.activation_kind not in {
            ProjectionActivationKind.PUBLISH,
            ProjectionActivationKind.ROLLBACK,
        }:
            raise ProductionContractObservationError("snapshot is not published")
        if any(
            reference.status is not ContextBundleEvidenceStatus.VALID
            for reference in bundle.evidence_references
        ):
            raise ProductionContractObservationError("bundle contains invalid evidence")
        if any(
            generation.service_authority_generation is None
            for generation in bundle.policy_decision_generations
        ):
            raise ProductionContractObservationError("service authorization is not pinned")
        read_context = observation.projection_retrieval.read_context
        if read_context is None or not read_context.source_isolation_pre_retrieval_enforced:
            raise ProductionContractObservationError("source isolation is not proven")
        if observation.projection_retrieval.outcome not in {
            ProjectionOutcome.SUCCEEDED,
            ProjectionOutcome.REPLAYED,
            ProjectionOutcome.PARTIAL,
        }:
            raise ProductionContractObservationError("projection outcome is not disclosable")
        if observation.retrieval.outcome not in {
            ContextOutcome.ALLOWED,
            ContextOutcome.PARTIAL,
        }:
            raise ProductionContractObservationError("retrieval outcome is not disclosable")
        return

    # Every rejected outcome is a no-content terminal branch.  The typed
    # observation validator checks the detailed shapes; these checks make the
    # safety rule explicit at the qualification seam.
    if bundle is not None:
        raise ProductionContractObservationError("rejected response carries a bundle")
    if observation.answer.outcome is AnswerQuestionOutcome.ANSWERED:
        raise ProductionContractObservationError("rejected response carries an answer")
    if observation.disclosure.accepted_answer is not None:
        raise ProductionContractObservationError("rejected response carries accepted content")
    if observation.retrieval.references or observation.retrieval.context:
        raise ProductionContractObservationError("rejected response carries retrieval content")
    if observation.projection_retrieval.chunks or observation.projection_retrieval.evidence:
        raise ProductionContractObservationError("rejected response carries projection content")


def _has_unauthorized_payload(observation: ProductionContractObservation) -> bool:
    """Return only a boolean; never copy payload text into a failure report."""

    rejected = observation.context_response.outcome not in {
        ContextOutcome.ALLOWED,
        ContextOutcome.PARTIAL,
    }
    return rejected and bool(
        observation.answer.outcome is AnswerQuestionOutcome.ANSWERED
        or observation.context_bundle is not None
        or observation.retrieval.context
        or observation.retrieval.references
        or observation.projection_retrieval.chunks
        or observation.projection_retrieval.evidence
    )


__all__ = [
    "ProductionContractSafetyGate",
    "ProductionContractSafetyResult",
    "ProductionContractSafetyViolation",
    "UNAUTHORIZED_RETRIEVAL_SAFETY_CASE_IDS",
    "UNAUTHORIZED_RETRIEVAL_SAFETY_SEAM",
    "UNAUTHORIZED_RETRIEVAL_SAFETY_VERSION",
    "evaluate_unauthorized_retrieval_disclosure",
]
