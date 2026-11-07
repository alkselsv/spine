"""#674-backed runner for the unsupported-requirements release gate."""

from __future__ import annotations

import argparse
from collections.abc import Callable
import subprocess
from pathlib import Path

from spine.application.extract_requirements import (
    EXTRACT_REQUIREMENTS_IMPLEMENTATION_KEY,
    extract_requirements_agent_version,
    extract_requirements_capability,
)
from spine.domain.capabilities import RequirementsExecutionConfiguration
from spine.evals.transcription_russian_v1 import (
    RussianTranscriptionCase,
    RussianTranscriptionDataset,
    load_russian_transcription_v1_dataset,
    run_russian_transcription_v1,
)
from spine.evaluations.unsupported_requirements import (
    UnsupportedRequirementsClass,
    UnsupportedRequirementsEvalReport,
    UnsupportedRequirementsEvaluationIdentity,
    UnsupportedRequirementsObservation,
    UnsupportedRequirementsStratum,
    evaluate_unsupported_requirements,
)

_QUALIFIED_PATHS = (
    "src/spine/evals/data/russian_transcription_v1.json",
    "src/spine/evals/transcription_russian_v1.py",
    "src/spine/evals/unsupported_requirements_runner.py",
    "src/spine/evaluations/unsupported_requirements.py",
)
ISSUE_674_DATASET_SHA256 = (
    "0f2c70d2663a6bfb69afc13e34d65683e04d7ddfb1914520b8a1174646a290e7"
)


def run_unsupported_requirements_qualification(
    *,
    tested_commit_sha: str,
    commit_verifier: Callable[[str], None] | None = None,
) -> UnsupportedRequirementsEvalReport:
    """Run the accepted #674 fake baseline through the approved #799 policy."""

    dataset = load_russian_transcription_v1_dataset()
    identity = unsupported_requirements_evaluation_identity(
        tested_commit_sha=tested_commit_sha,
        dataset=dataset,
    )
    execution_failures: list[str] = []
    observations: tuple[UnsupportedRequirementsObservation, ...] = ()
    try:
        (commit_verifier or verify_qualified_commit)(tested_commit_sha)
        source_report = run_russian_transcription_v1(dataset)
        if source_report.dataset_sha256 != ISSUE_674_DATASET_SHA256:
            execution_failures.append("source_dataset_digest_mismatch")
        observations = _observe_dataset(dataset)
    except (OSError, subprocess.SubprocessError, ValueError):
        execution_failures.append("qualification_execution_failed")
    return evaluate_unsupported_requirements(
        observations=observations,
        identity=identity,
        execution_failures=tuple(execution_failures),
    )


def unsupported_requirements_evaluation_identity(
    *,
    tested_commit_sha: str,
    dataset: RussianTranscriptionDataset | None = None,
) -> UnsupportedRequirementsEvaluationIdentity:
    """Build the canonical identity shared by the runner and contract tests."""

    loaded = dataset or load_russian_transcription_v1_dataset()
    capability = extract_requirements_capability()
    version = extract_requirements_agent_version()
    return UnsupportedRequirementsEvaluationIdentity(
        tested_commit_sha=tested_commit_sha,
        source_dataset_id=loaded.dataset_id,
        source_dataset_version=loaded.version,
        source_dataset_sha256=ISSUE_674_DATASET_SHA256,
        source_schema_version=loaded.schema_version,
        source_requirements_metric_version=loaded.requirements_metric_version,
        source_fake_model_version=loaded.fake_model_version,
        source_fake_config_version=loaded.fake_config_version,
        capability_id=capability.id,
        input_schema=f"{capability.input_schema.name}:{capability.input_schema.version}",
        output_schema=f"{capability.output_schema.name}:{capability.output_schema.version}",
        agent_version_id=version.id,
        implementation_key=EXTRACT_REQUIREMENTS_IMPLEMENTATION_KEY,
        execution_configuration=RequirementsExecutionConfiguration(),
    )


def verify_qualified_commit(tested_commit_sha: str) -> None:
    """Reject caller-supplied SHAs whose qualified files differ from this tree."""

    if len(tested_commit_sha) != 40:
        raise ValueError("tested commit must be a full SHA")
    root = Path(__file__).resolve().parents[3]
    resolved = subprocess.run(
        ["git", "rev-parse", f"{tested_commit_sha}^{{commit}}"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.strip()
    if resolved != tested_commit_sha:
        raise ValueError("tested commit identity is not exact")
    subprocess.run(
        ["git", "merge-base", "--is-ancestor", tested_commit_sha, "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        timeout=10,
    )
    subprocess.run(
        ["git", "diff", "--quiet", tested_commit_sha, "HEAD", "--", *_QUALIFIED_PATHS],
        cwd=root,
        check=True,
        capture_output=True,
        timeout=10,
    )


def _observe_dataset(
    dataset: RussianTranscriptionDataset,
) -> tuple[UnsupportedRequirementsObservation, ...]:
    observations: list[UnsupportedRequirementsObservation] = []
    for case in dataset.cases:
        observations.extend(_observe_case(case))
    return tuple(observations)


def _observe_case(
    case: RussianTranscriptionCase,
) -> tuple[UnsupportedRequirementsObservation, ...]:
    observations: list[UnsupportedRequirementsObservation] = []
    gold = {item.requirement_id: item for item in case.requirement_labels}
    predicted = {item.requirement_id: item for item in case.fake_requirements}

    for requirement_id, label in gold.items():
        candidate = predicted.get(requirement_id)
        if candidate is None:
            predicted_class = _outcome_class(case.fake_requirements_outcome)
        elif set(candidate.evidence_segment_ids) == set(label.evidence_segment_ids):
            predicted_class = UnsupportedRequirementsClass.SUPPORTED
        else:
            predicted_class = UnsupportedRequirementsClass.CLARIFICATION_NEEDED
        observations.append(
            UnsupportedRequirementsObservation(
                item_id=f"{case.case_id}:requirement:{requirement_id}",
                source_case_id=case.case_id,
                actual_class=UnsupportedRequirementsClass.SUPPORTED,
                predicted_class=predicted_class,
            )
        )

    for requirement_id, candidate in predicted.items():
        if requirement_id in gold:
            continue
        malicious = bool(
            set(candidate.evidence_segment_ids) & set(case.malicious_spans)
        )
        observations.append(
            UnsupportedRequirementsObservation(
                item_id=f"{case.case_id}:unexpected:{requirement_id}",
                source_case_id=case.case_id,
                actual_class=UnsupportedRequirementsClass.UNSUPPORTED,
                predicted_class=UnsupportedRequirementsClass.SUPPORTED,
                strata=(
                    (UnsupportedRequirementsStratum.MALICIOUS_INSTRUCTION,)
                    if malicious
                    else ()
                ),
            )
        )

    cited_segments = {
        segment_id
        for candidate in predicted.values()
        for segment_id in candidate.evidence_segment_ids
    }
    for segment_id in case.malicious_spans:
        observations.append(
            UnsupportedRequirementsObservation(
                item_id=f"{case.case_id}:malicious:{segment_id}",
                source_case_id=case.case_id,
                actual_class=UnsupportedRequirementsClass.UNSUPPORTED,
                predicted_class=(
                    UnsupportedRequirementsClass.SUPPORTED
                    if segment_id in cited_segments
                    else UnsupportedRequirementsClass.UNSUPPORTED
                ),
                strata=(UnsupportedRequirementsStratum.MALICIOUS_INSTRUCTION,),
            )
        )

    if case.expected_requirements_outcome == "clarify":
        strata = (
            (UnsupportedRequirementsStratum.WEAK_OR_UNKNOWN_RECOGNITION,)
            if case.kind == "weak_speech"
            else (UnsupportedRequirementsStratum.MISSING_OR_FABRICATED_EVIDENCE,)
        )
        observations.append(
            UnsupportedRequirementsObservation(
                item_id=f"{case.case_id}:outcome",
                source_case_id=case.case_id,
                actual_class=UnsupportedRequirementsClass.CLARIFICATION_NEEDED,
                predicted_class=_outcome_class(case.fake_requirements_outcome),
                strata=strata,
            )
        )
    elif case.expected_requirements_outcome == "abstain":
        observations.append(
            UnsupportedRequirementsObservation(
                item_id=f"{case.case_id}:outcome",
                source_case_id=case.case_id,
                actual_class=UnsupportedRequirementsClass.INCONCLUSIVE_ABSTENTION,
                predicted_class=_outcome_class(case.fake_requirements_outcome),
            )
        )
    return tuple(observations)


def _outcome_class(
    outcome: str,
) -> UnsupportedRequirementsClass:
    return {
        "extract": UnsupportedRequirementsClass.SUPPORTED,
        "clarify": UnsupportedRequirementsClass.CLARIFICATION_NEEDED,
        "abstain": UnsupportedRequirementsClass.INCONCLUSIVE_ABSTENTION,
    }[outcome]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the #674-backed unsupported-requirements qualification gate."
    )
    parser.add_argument("--tested-commit", required=True)
    args = parser.parse_args()
    report = run_unsupported_requirements_qualification(
        tested_commit_sha=args.tested_commit
    )
    print(report.model_dump_json(indent=2))
    return 0 if report.result.value == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ISSUE_674_DATASET_SHA256",
    "run_unsupported_requirements_qualification",
    "unsupported_requirements_evaluation_identity",
    "verify_qualified_commit",
]
