"""Untrusted, evidence-bound output of one Q&A generation attempt.

The candidate contract is deliberately separate from ``AnswerQuestionOutput``:
it is an internal model artifact and is not a disclosure decision. It carries
the exact Context Bundle identity so later validation cannot silently combine
evidence from another attempt.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, TypeVar

from pydantic import ConfigDict, model_validator

from spine.domain.capabilities.answer_question import (
    AnswerContent,
    AnswerQuestionContractError,
    AnswerQuestionInput,
)
from spine.domain.common import ValidatedDefinitionModel
from spine.domain.context_bundles import (
    ContextBundle,
    ContextBundleEvidenceStatus,
    ContextBundleLineageRef,
    ContextBundleOutcome,
    ContextBundleState,
)


ANSWER_QUESTION_CANDIDATE_INPUT_SCHEMA = "answer-question.candidate-input:v1"
ANSWER_QUESTION_CANDIDATE_OUTPUT_SCHEMA = "answer-question.candidate-output:v1"


class _CandidateModel(ValidatedDefinitionModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        revalidate_instances="always",
    )


class AnswerQuestionCandidateInput(_CandidateModel):
    """The only input a Q&A handler may use for candidate generation."""

    schema_version: Literal[ANSWER_QUESTION_CANDIDATE_INPUT_SCHEMA] = (
        ANSWER_QUESTION_CANDIDATE_INPUT_SCHEMA
    )
    capability: Literal["answer_question"] = "answer_question"
    question: AnswerQuestionInput
    context_bundle: ContextBundle

    @model_validator(mode="after")
    def validate_authorized_context(self) -> AnswerQuestionCandidateInput:
        bundle = self.context_bundle
        if (
            bundle.workspace_id != self.question.scope.workspace_id
            or bundle.environment_id != self.question.scope.environment_id
        ):
            raise ValueError("context bundle is outside the question scope")
        if (
            bundle.run_id != self.question.correlation.run_id
            or bundle.attempt_id != self.question.correlation.attempt_id
        ):
            raise ValueError("context bundle is outside the question attempt")
        if bundle.outcome not in {
            ContextBundleOutcome.ALLOWED,
            ContextBundleOutcome.DEGRADED,
            ContextBundleOutcome.PARTIAL,
        }:
            raise ValueError("context bundle is not authorized for generation")
        if bundle.state not in {ContextBundleState.COMPLETE, ContextBundleState.PARTIAL}:
            raise ValueError("context bundle is not authorized for generation")
        if not bundle.evidence_references:
            raise ValueError("context bundle has no usable evidence")
        if any(
            evidence.status is not ContextBundleEvidenceStatus.VALID
            for evidence in bundle.evidence_references
        ):
            raise ValueError("context bundle contains unauthorized evidence")
        return self


class AnswerQuestionCandidateOutput(_CandidateModel):
    """Structured model material awaiting deterministic Q&A validation."""

    schema_version: Literal[ANSWER_QUESTION_CANDIDATE_OUTPUT_SCHEMA] = (
        ANSWER_QUESTION_CANDIDATE_OUTPUT_SCHEMA
    )
    capability: Literal["answer_question"] = "answer_question"
    question: AnswerQuestionInput
    context_bundle: ContextBundleLineageRef
    answer: AnswerContent

    @model_validator(mode="after")
    def validate_answer_lineage(self) -> AnswerQuestionCandidateOutput:
        if (
            self.context_bundle.workspace_id != self.question.scope.workspace_id
            or self.context_bundle.environment_id != self.question.scope.environment_id
            or self.context_bundle.run_id != self.question.correlation.run_id
            or self.context_bundle.attempt_id != self.question.correlation.attempt_id
        ):
            raise ValueError("candidate context bundle has incompatible lineage")
        if (
            self.answer.workspace_id != self.question.scope.workspace_id
            or self.answer.environment_id != self.question.scope.environment_id
            or self.answer.run_id != self.question.correlation.run_id
            or self.answer.attempt_id != self.question.correlation.attempt_id
        ):
            raise ValueError("candidate answer has incompatible lineage")
        if any(
            citation.context_bundle != self.context_bundle
            for citation in self.answer.citations
        ):
            raise ValueError("candidate citation has incompatible context bundle")
        return self


_CandidateT = TypeVar("_CandidateT", bound=_CandidateModel)


def _parse_candidate(
    model_type: type[_CandidateT],
    value: str | Mapping[str, Any] | _CandidateT,
) -> _CandidateT:
    try:
        if isinstance(value, str):
            return model_type.model_validate_json(value)
        return model_type.model_validate(value)
    except (TypeError, ValueError) as exc:
        raise AnswerQuestionContractError(
            f"{model_type.__name__} is not a compatible answer_question candidate"
        ) from exc


def parse_answer_question_candidate_input(
    value: str | Mapping[str, Any] | AnswerQuestionCandidateInput,
) -> AnswerQuestionCandidateInput:
    return _parse_candidate(AnswerQuestionCandidateInput, value)


def parse_answer_question_candidate_output(
    value: str | Mapping[str, Any] | AnswerQuestionCandidateOutput,
) -> AnswerQuestionCandidateOutput:
    return _parse_candidate(AnswerQuestionCandidateOutput, value)


AnswerCandidateInput = AnswerQuestionCandidateInput
AnswerCandidateOutput = AnswerQuestionCandidateOutput


__all__ = [
    "ANSWER_QUESTION_CANDIDATE_INPUT_SCHEMA",
    "ANSWER_QUESTION_CANDIDATE_OUTPUT_SCHEMA",
    "AnswerCandidateInput",
    "AnswerCandidateOutput",
    "AnswerQuestionCandidateInput",
    "AnswerQuestionCandidateOutput",
    "parse_answer_question_candidate_input",
    "parse_answer_question_candidate_output",
]
