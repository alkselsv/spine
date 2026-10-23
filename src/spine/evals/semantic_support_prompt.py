"""Versioned, human-labelled evaluation for the semantic-support policy prompt."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import ConfigDict, Field

from spine.application.semantic_support import SemanticSupportValidator
from spine.domain.capabilities import (
    AnswerClaim,
    AnswerClaimLocation,
    AnswerQuestionScope,
    Citation,
    ClaimEvidenceRelationship,
    ClaimEvidenceRelationshipKind,
    RunAttemptCorrelation,
    SemanticSupportEvidence,
    SemanticSupportOutcome,
    SemanticSupportPolicy,
    SemanticSupportRequest,
)
from spine.domain.common import DefinitionModel
from spine.domain.context_bundles import (
    ContextBundleEvidenceReference,
    ContextBundleLineageRef,
    ContextBundleRef,
)


SEMANTIC_SUPPORT_EVAL_SCHEMA_VERSION = "semantic-support-prompt-eval:v1"
SEMANTIC_SUPPORT_EVAL_DATASET_VERSION = "2.0.0"


class SemanticSupportPromptCase(DefinitionModel):
    """One synthetic claim/evidence pair with a human-labelled outcome."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str = Field(pattern=r"^[a-z][a-z0-9-]+$")
    scenario: Literal[
        "normalized_direct_match",
        "faithful_paraphrase",
        "partial_support",
        "unsupported_absence_claim",
    ]
    claim: str = Field(min_length=1)
    evidence: tuple[str, ...] = Field(min_length=1)
    expected_outcome: SemanticSupportOutcome


class SemanticSupportPromptDataset(DefinitionModel):
    """Immutable prompt-evaluation dataset and policy pin."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[SEMANTIC_SUPPORT_EVAL_SCHEMA_VERSION]
    dataset_version: Literal[SEMANTIC_SUPPORT_EVAL_DATASET_VERSION]
    policy_binding: Literal["qa.semantic-support:r2"]
    prompt_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    assumptions: tuple[str, ...] = Field(min_length=1)
    cases: tuple[SemanticSupportPromptCase, ...] = Field(min_length=1)


class SemanticSupportEvaluationConfiguration(DefinitionModel):
    """Immutable identities for one reproducible evaluator execution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    evaluator_id: str = Field(min_length=1)
    evaluator_version: str = Field(min_length=1)
    validator_id: str = Field(min_length=1)
    validator_version: str = Field(min_length=1)
    model_profile_id: str = Field(min_length=1)
    model_profile_version: str = Field(min_length=1)
    model_profile_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class SemanticSupportCaseOutcome(DefinitionModel):
    """Content-free labelled outcome for one pinned dataset case."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    expected_outcome: SemanticSupportOutcome
    observed_outcome: SemanticSupportOutcome
    passed: bool


class SemanticSupportPromptEvaluationReport(DefinitionModel):
    """Content-free result of executing the labelled prompt cases."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["semantic-support-prompt-eval-report:v2"] = (
        "semantic-support-prompt-eval-report:v2"
    )
    dataset_version: str
    policy_binding: str
    prompt_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    configuration: SemanticSupportEvaluationConfiguration
    total_cases: int = Field(ge=1)
    passed_cases: int = Field(ge=0)
    failed_case_ids: tuple[str, ...]
    case_outcomes: tuple[SemanticSupportCaseOutcome, ...]


@lru_cache(maxsize=1)
def load_semantic_support_prompt_dataset() -> SemanticSupportPromptDataset:
    path = Path(__file__).with_name("data") / "semantic_support_prompt_v2.json"
    return SemanticSupportPromptDataset.model_validate_json(
        path.read_text(encoding="utf-8")
    )


async def run_semantic_support_prompt_evaluation(
    *,
    validator: SemanticSupportValidator,
    configuration: SemanticSupportEvaluationConfiguration,
) -> SemanticSupportPromptEvaluationReport:
    """Execute every labelled case through the configured validator seam."""

    dataset = load_semantic_support_prompt_dataset()
    failed: list[str] = []
    outcomes: list[SemanticSupportCaseOutcome] = []
    for case in dataset.cases:
        assessment = await validator.assess(_request(case, dataset))
        passed = assessment.outcome is case.expected_outcome
        if not passed:
            failed.append(case.case_id)
        outcomes.append(
            SemanticSupportCaseOutcome(
                case_id=case.case_id,
                expected_outcome=case.expected_outcome,
                observed_outcome=assessment.outcome,
                passed=passed,
            )
        )
    return SemanticSupportPromptEvaluationReport(
        dataset_version=dataset.dataset_version,
        policy_binding=dataset.policy_binding,
        prompt_digest=dataset.prompt_digest,
        configuration=configuration,
        total_cases=len(dataset.cases),
        passed_cases=len(dataset.cases) - len(failed),
        failed_case_ids=tuple(failed),
        case_outcomes=tuple(outcomes),
    )


def _request(
    case: SemanticSupportPromptCase,
    dataset: SemanticSupportPromptDataset,
) -> SemanticSupportRequest:
    stable = lambda name: uuid5(  # noqa: E731 - keeps fixture lineage visible
        NAMESPACE_URL, f"spine:semantic-support-eval:{case.case_id}:{name}"
    )
    workspace_id = stable("workspace")
    environment_id = stable("environment")
    run_id = stable("run")
    attempt_id = stable("attempt")
    answer_id = stable("answer")
    bundle = ContextBundleLineageRef(
        bundle=ContextBundleRef(
            bundle_id=stable("bundle"),
            bundle_version=1,
            bundle_digest="a" * 64,
        ),
        workspace_id=workspace_id,
        environment_id=environment_id,
        run_id=run_id,
        attempt_id=attempt_id,
    )
    claim = AnswerClaim(
        claim_id=stable("claim"),
        workspace_id=workspace_id,
        environment_id=environment_id,
        run_id=run_id,
        attempt_id=attempt_id,
        text=case.claim,
        location=AnswerClaimLocation(
            answer_id=answer_id,
            start_offset=0,
            end_offset=len(case.claim),
        ),
    )
    evidence: list[SemanticSupportEvidence] = []
    relationships: list[ClaimEvidenceRelationship] = []
    for index, content in enumerate(case.evidence, start=1):
        citation_id = stable(f"citation:{index}")
        evidence_id = f"semantic-{case.case_id}-{index}"
        citation = Citation(
            citation_id=citation_id,
            answer_id=answer_id,
            context_bundle=bundle,
            evidence_reference=ContextBundleEvidenceReference(
                schema_version="context-evidence-reference:v2",
                evidence_id=evidence_id,
                workspace_id=workspace_id,
                environment_id=environment_id,
                projection_snapshot_id=stable("snapshot"),
                activation_generation=1,
                source_object_id=stable(f"source:{index}"),
                source_revision_id=stable(f"revision:{index}"),
                original_sha256="b" * 64,
                revision_digest="c" * 64,
                revision_number=1,
                processing_receipt_id=stable(f"receipt:{index}"),
                projected_kind="chunk",
                locator_contract_version="locator:v1",
                locator={"kind": "character_range", "value": f"0:{len(content)}"},
                locator_digest="d" * 64,
                evidence_digest="e" * 64,
            ),
        )
        evidence.append(SemanticSupportEvidence(citation=citation, content=content))
        relationships.append(
            ClaimEvidenceRelationship(
                relationship_id=stable(f"relationship:{index}"),
                answer_id=answer_id,
                workspace_id=workspace_id,
                environment_id=environment_id,
                run_id=run_id,
                attempt_id=attempt_id,
                claim_id=claim.claim_id,
                kind=ClaimEvidenceRelationshipKind.INDEPENDENT,
                citation_ids=(citation_id,),
            )
        )
    return SemanticSupportRequest(
        policy=SemanticSupportPolicy(
            policy_id=uuid5(NAMESPACE_URL, f"spine:{dataset.policy_binding}"),
            policy_version=dataset.policy_binding,
            policy_digest=dataset.prompt_digest,
        ),
        scope=AnswerQuestionScope(
            workspace_id=workspace_id,
            environment_id=environment_id,
        ),
        correlation=RunAttemptCorrelation(
            run_id=run_id,
            attempt_id=attempt_id,
            trace_id=stable("trace"),
        ),
        context_bundle=bundle,
        claim=claim,
        relationships=tuple(relationships),
        evidence=tuple(evidence),
    )


__all__ = [
    "SEMANTIC_SUPPORT_EVAL_DATASET_VERSION",
    "SemanticSupportCaseOutcome",
    "SemanticSupportEvaluationConfiguration",
    "SemanticSupportPromptDataset",
    "SemanticSupportPromptEvaluationReport",
    "load_semantic_support_prompt_dataset",
    "run_semantic_support_prompt_evaluation",
]
