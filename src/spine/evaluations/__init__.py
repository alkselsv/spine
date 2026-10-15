"""Offline, handoff, regression, and deployment evaluations."""

from spine.evaluations.evidence_lineage import (
    EVIDENCE_LINEAGE_EVALUATOR_VERSION,
    EvidenceLineageEvaluationReport,
    evaluate_evidence_lineage_outcomes,
)

from spine.evaluations.retrieval_metrics import (
    MRR_EVALUATOR_KEY,
    RECALL_AT_K_EVALUATOR_KEY,
    RETRIEVAL_METRICS_EVALUATOR_VERSION,
    evaluate_mrr,
    evaluate_recall_at_k,
)
from spine.evaluations.citation_metrics import (
    AnswerClaim,
    AnswerOutcome,
    AnswerSubmission,
    CITATION_EVALUATOR_KEY,
    CITATION_METRICS_EVALUATOR_VERSION,
    CITATION_PRECISION_EVALUATOR_KEY,
    CITATION_RECALL_EVALUATOR_KEY,
    Citation,
    ContextBundle,
    GROUNDEDNESS_EVALUATOR_KEY,
    LOCATOR_VALIDITY_EVALUATOR_KEY,
    evaluate_answer_outcome,
    evaluate_citation_precision,
    evaluate_citation_recall,
    evaluate_citations,
    evaluate_groundedness,
    evaluate_locator_validity,
)

__all__ = [
    "EVIDENCE_LINEAGE_EVALUATOR_VERSION",
    "EvidenceLineageEvaluationReport",
    "evaluate_evidence_lineage_outcomes",
    "MRR_EVALUATOR_KEY",
    "RECALL_AT_K_EVALUATOR_KEY",
    "RETRIEVAL_METRICS_EVALUATOR_VERSION",
    "evaluate_mrr",
    "evaluate_recall_at_k",
    "AnswerClaim",
    "AnswerOutcome",
    "AnswerSubmission",
    "CITATION_EVALUATOR_KEY",
    "CITATION_METRICS_EVALUATOR_VERSION",
    "CITATION_PRECISION_EVALUATOR_KEY",
    "CITATION_RECALL_EVALUATOR_KEY",
    "Citation",
    "ContextBundle",
    "GROUNDEDNESS_EVALUATOR_KEY",
    "LOCATOR_VALIDITY_EVALUATOR_KEY",
    "evaluate_answer_outcome",
    "evaluate_citation_precision",
    "evaluate_citation_recall",
    "evaluate_citations",
    "evaluate_groundedness",
    "evaluate_locator_validity",
]
