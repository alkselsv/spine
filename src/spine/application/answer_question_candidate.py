"""Capability-owned mapping for one internal Q&A candidate artifact.

This module is the Q&A seam over the generic local runtime.  It does not
resolve context, choose a source, disclose content, or implement retries.  The
runtime remains responsible for handler selection, bounded retry, cancellation
and idempotent artifact persistence.
"""

from __future__ import annotations

import math
from typing import Any

from pydantic import BaseModel

from spine.agents.runtime import (
    AgentResponse,
    CapabilityArtifactSchema,
    CapabilitySchemaIdentity,
)
from spine.domain.agents import AgentInvocation, AgentRetrySafety
from spine.domain.capabilities import (
    AnswerQuestionCandidateInput,
    AnswerQuestionCandidateOutput,
    CapabilityDefinition,
)
from spine.domain.artifacts import Artifact
from spine.domain.common import SchemaRef

from spine.application.answer_question_execution import (
    AnswerQuestionExecutionBoundary,
    AnswerQuestionExecutionRequest,
    AnswerQuestionExecutionResult,
)


_SAFE_METRIC_KEYS = frozenset(
    {"input_tokens", "output_tokens", "cached_input_tokens"}
)
_EXPECTED_INPUT_SCHEMA = SchemaRef(
    name="answer-question.candidate-input", version="v1"
)
_EXPECTED_OUTPUT_SCHEMA = SchemaRef(
    name="answer-question.candidate-output", version="v1"
)


def answer_question_candidate_artifact_schema(
    capability: CapabilityDefinition,
) -> CapabilityArtifactSchema:
    """Build the typed handoff mapping for the resolved answer capability.

    The capability's schema identity remains authoritative and is attached to
    the generic runtime request.  The concrete Pydantic models below define
    what that identity means for this Q&A implementation.
    """

    if type(capability) is not CapabilityDefinition or capability.key != "answer_question":
        raise ValueError("answer_question capability is required")
    if (
        capability.input_schema != _EXPECTED_INPUT_SCHEMA
        or capability.output_schema != _EXPECTED_OUTPUT_SCHEMA
    ):
        raise ValueError(
            "answer_question capability candidate schemas are incompatible"
        )

    return CapabilityArtifactSchema(
        schema_identity=CapabilitySchemaIdentity.from_capability(capability),
        input_model=AnswerQuestionCandidateInput,
        output_model=AnswerQuestionCandidateOutput,
        assemble_input=_assemble_one_input,
        validate_metadata=_validate_metadata,
        validate_handoff=_validate_handoff,
    )


def build_answer_question_candidate_artifact_schema(
    capability: CapabilityDefinition,
) -> CapabilityArtifactSchema:
    """Compatibility spelling for composition roots registering the mapper."""

    return answer_question_candidate_artifact_schema(capability)


answer_question_candidate_schema = answer_question_candidate_artifact_schema


async def generate_answer_question_candidate(
    execution: AnswerQuestionExecutionBoundary,
    request: AnswerQuestionExecutionRequest,
    invocation: AgentInvocation,
) -> AnswerQuestionExecutionResult:
    """Run one candidate attempt through the existing local runtime seam.

    The input artifact must contain the exact
    :class:`AnswerQuestionCandidateInput` accepted by the mapper above.  The
    artifact is intentionally not reconstructed here: the runtime's canonical
    artifact read and lineage checks are the source of truth.  This function
    only constrains the invocation shape and delegates binding resolution,
    handler registry admission, pin verification, retries and cancellation to
    the existing execution/runtime contracts.
    """

    if type(request) is not AnswerQuestionExecutionRequest:
        raise ValueError("answer question execution request is invalid")
    if type(invocation) is not AgentInvocation:
        raise ValueError("answer question invocation is invalid")
    if (
        invocation.capability != "answer_question"
        or len(invocation.input_artifact_ids) != 1
        or invocation.context_request
        or invocation.execution_configuration_digest is not None
        or invocation.constraints != request.intent.runtime_constraints()
    ):
        raise ValueError("answer candidate invocation is invalid")

    repeatable = invocation.model_copy(
        update={"retry_safety": AgentRetrySafety.SAFE_TO_REPEAT}
    )
    return await execution.execute(request, repeatable)


def _assemble_one_input(artifacts: tuple[Artifact, ...]) -> Any:
    if len(artifacts) != 1:
        raise ValueError("answer candidate requires exactly one input artifact")
    return artifacts[0].content


def _validate_metadata(response: AgentResponse[Any]) -> None:
    if response.proposed_action_ids:
        raise ValueError("answer candidate cannot propose external actions")
    if set(response.structured_metrics) - _SAFE_METRIC_KEYS:
        raise ValueError("answer candidate metadata contains an unsupported metric")
    if any(not math.isfinite(value) for value in response.structured_metrics.values()):
        raise ValueError("answer candidate metadata contains an invalid metric")
    if response.error is not None:
        if (
            response.evidence_refs
            or response.confidence is not None
            or response.structured_metrics
        ):
            raise ValueError("failed answer candidate contains provider metadata")
        return
    if not response.evidence_refs:
        raise ValueError("answer candidate must expose evidence references")
    if any(not reference.strip() for reference in response.evidence_refs):
        raise ValueError("answer candidate evidence reference is invalid")
    if len(set(response.evidence_refs)) != len(response.evidence_refs):
        raise ValueError("answer candidate evidence references must be unique")


def _validate_handoff(
    input_model: BaseModel,
    output_model: BaseModel,
    response: AgentResponse[Any],
) -> None:
    if not isinstance(input_model, AnswerQuestionCandidateInput):
        raise ValueError("answer candidate input mapping is invalid")
    if not isinstance(output_model, AnswerQuestionCandidateOutput):
        raise ValueError("answer candidate output mapping is invalid")
    if response.error is not None:
        if response.output is not None:
            raise ValueError("failed answer candidate contains output")
        return

    expected_bundle = input_model.context_bundle.lineage_reference
    if output_model.question != input_model.question:
        raise ValueError("answer candidate question does not match input")
    if output_model.context_bundle != expected_bundle:
        raise ValueError("answer candidate changed the authorized context bundle")

    allowed_evidence = {
        reference.evidence_id: reference
        for reference in input_model.context_bundle.evidence_references
    }
    citations = output_model.answer.citations
    if not citations:
        raise ValueError("answer candidate requires citations")
    for citation in citations:
        expected = allowed_evidence.get(citation.evidence_id)
        if expected is None or citation.evidence_reference != expected:
            raise ValueError("answer candidate cites evidence outside the context bundle")
    citation_evidence_ids = tuple(citation.evidence_id for citation in citations)
    if set(response.evidence_refs) != set(citation_evidence_ids):
        raise ValueError("answer candidate evidence metadata does not match citations")


__all__ = [
    "answer_question_candidate_artifact_schema",
    "answer_question_candidate_schema",
    "build_answer_question_candidate_artifact_schema",
    "generate_answer_question_candidate",
]
