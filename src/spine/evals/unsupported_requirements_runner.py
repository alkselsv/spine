"""#674-backed runner for the unsupported-requirements release gate."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable
from hashlib import sha256
import subprocess
from pathlib import Path
from uuid import UUID, uuid5

from spine.agents.runtime import (
    AgentExecutionContext,
    AgentRequest,
    CapabilitySchemaIdentity,
    TrustedExecutionIdentitySnapshot,
)
from spine.application.diagnostics import DiagnosticContext
from spine.application.extract_requirements import (
    EXTRACT_REQUIREMENTS_IMPLEMENTATION_KEY,
    deterministic_extract_requirements,
    extract_requirements_agent_version,
    extract_requirements_capability,
)
from spine.domain.artifacts import TimeRangeLocator, TranscriptRecognitionQuality
from spine.domain.capabilities import (
    ExtractRequirementsInput,
    RequirementsEvidenceAvailability,
    RequirementsExecutionConfiguration,
    RequirementsExtractionOutcome,
    RequirementsExtractionOutput,
    TranscriptRequirementsEvidence,
)
from spine.domain.common import EnvironmentKind
from spine.domain.workflows import (
    AcceptedSourceRevisionRef,
    ClientRequestV1,
    RequirementState,
    VersionedRequirement,
)
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
    "pyproject.toml",
    "uv.lock",
    "src/spine",
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
        observations = asyncio.run(_observe_dataset(dataset))
    except Exception:
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


async def _observe_dataset(
    dataset: RussianTranscriptionDataset,
) -> tuple[UnsupportedRequirementsObservation, ...]:
    observations: list[UnsupportedRequirementsObservation] = []
    for case in dataset.cases:
        output, segment_ids = await _execute_case(case)
        observations.extend(_observe_case(case, output, segment_ids))
    return tuple(observations)


def _observe_case(
    case: RussianTranscriptionCase,
    output: RequirementsExtractionOutput,
    segment_ids: dict[UUID, str],
) -> tuple[UnsupportedRequirementsObservation, ...]:
    observations: list[UnsupportedRequirementsObservation] = []
    unmatched = list(output.requirements)
    normalized_labels = tuple(_normalize(item.label) for item in case.requirement_labels)
    if len(set(normalized_labels)) != len(normalized_labels):
        raise ValueError("ambiguous duplicate requirement label")

    for label in case.requirement_labels:
        matches = tuple(
            item
            for item in unmatched
            if _normalize(item.statement) == _normalize(label.label)
        )
        if len(matches) > 1:
            raise ValueError("ambiguous requirement output mapping")
        candidate = matches[0] if matches else None
        if candidate is None:
            predicted_class = _output_class(output)
        else:
            unmatched.remove(candidate)
            predicted_class = _candidate_class(
                candidate=candidate,
                output=output,
                expected_segment_ids=label.evidence_segment_ids,
                segment_ids=segment_ids,
            )
        observations.append(
            UnsupportedRequirementsObservation(
                item_id=f"{case.case_id}:requirement:{label.requirement_id}",
                source_case_id=case.case_id,
                actual_class=UnsupportedRequirementsClass.SUPPORTED,
                predicted_class=predicted_class,
            )
        )

    for candidate in unmatched:
        cited = _candidate_segment_ids(candidate, segment_ids)
        malicious = bool(set(cited) & set(case.malicious_spans))
        observations.append(
            UnsupportedRequirementsObservation(
                item_id=f"{case.case_id}:unexpected:{candidate.requirement_id}",
                source_case_id=case.case_id,
                actual_class=UnsupportedRequirementsClass.UNSUPPORTED,
                predicted_class=_candidate_class(
                    candidate=candidate,
                    output=output,
                    expected_segment_ids=cited,
                    segment_ids=segment_ids,
                ),
                strata=tuple(
                    stratum
                    for stratum, include in (
                        (UnsupportedRequirementsStratum.MISSING_OR_FABRICATED_EVIDENCE, True),
                        (UnsupportedRequirementsStratum.MALICIOUS_INSTRUCTION, malicious),
                    )
                    if include
                ),
            )
        )

    cited_segments = {
        segment_id for candidate in output.requirements
        for segment_id in _candidate_segment_ids(candidate, segment_ids)
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
                predicted_class=_output_class(output),
                strata=strata,
            )
        )
    elif case.expected_requirements_outcome == "abstain":
        observations.append(
            UnsupportedRequirementsObservation(
                item_id=f"{case.case_id}:outcome",
                source_case_id=case.case_id,
                actual_class=UnsupportedRequirementsClass.INCONCLUSIVE_ABSTENTION,
                predicted_class=_output_class(output),
                strata=(UnsupportedRequirementsStratum.UNAVAILABLE_AUTHORITY,),
            )
        )
    return tuple(observations)


def _candidate_class(
    *,
    candidate: VersionedRequirement,
    output: RequirementsExtractionOutput,
    expected_segment_ids: tuple[str, ...],
    segment_ids: dict[UUID, str],
) -> UnsupportedRequirementsClass:
    cited = _candidate_segment_ids(candidate, segment_ids)
    exact_evidence = cited == expected_segment_ids
    safely_supported = (
        output.outcome is RequirementsExtractionOutcome.SUCCESS
        and candidate.state is RequirementState.CANDIDATE
        and candidate.uncertainty is None
        and not candidate.ambiguous_markers
        and not candidate.missing_markers
        and exact_evidence
    )
    return (
        UnsupportedRequirementsClass.SUPPORTED
        if safely_supported
        else UnsupportedRequirementsClass.CLARIFICATION_NEEDED
    )


def _candidate_segment_ids(
    candidate: VersionedRequirement,
    segment_ids: dict[UUID, str],
) -> tuple[str, ...]:
    try:
        return tuple(
            segment_ids[reference.segment_id]
            for reference in candidate.evidence_refs
        )
    except KeyError as error:
        raise ValueError("candidate cites evidence outside the pinned dataset") from error


def _output_class(output: RequirementsExtractionOutput) -> UnsupportedRequirementsClass:
    if output.outcome is RequirementsExtractionOutcome.PARTIAL:
        return UnsupportedRequirementsClass.CLARIFICATION_NEEDED
    return UnsupportedRequirementsClass.INCONCLUSIVE_ABSTENTION


def _normalize(value: str) -> str:
    return " ".join(value.casefold().split())


class _Cancellation:
    is_cancelled = False

    def raise_if_cancelled(self) -> None:
        return None


class _Events:
    async def emit(self, event: object) -> None:
        del event


_EVAL_NAMESPACE = UUID("a51b3c0b-d840-4eaf-b9ca-6b4d12e38875")


def _id(case_id: str, role: str) -> UUID:
    return uuid5(_EVAL_NAMESPACE, f"{case_id}:{role}")


async def _execute_case(
    case: RussianTranscriptionCase,
) -> tuple[RequirementsExtractionOutput, dict[UUID, str]]:
    source = AcceptedSourceRevisionRef(
        source_object_id=_id(case.case_id, "source"),
        source_revision_id=_id(case.case_id, "revision"),
        provenance_id=_id(case.case_id, "provenance"),
    )
    transcript_id = _id(case.case_id, "transcript")
    reverse_segment_ids: dict[UUID, str] = {}
    evidence: list[TranscriptRequirementsEvidence] = []
    for segment in case.reference_segments:
        segment_id = _id(case.case_id, segment.segment_id)
        reverse_segment_ids[segment_id] = segment.segment_id
        locator = TimeRangeLocator(
            source_object_id=source.source_object_id,
            source_revision_id=source.source_revision_id,
            source_revision_digest=ISSUE_674_DATASET_SHA256,
            transcript_artifact_id=transcript_id,
            transcript_artifact_digest=sha256(
                case.expected_transcript.encode("utf-8")
            ).hexdigest(),
            transcript_artifact_schema_version="transcription-artifact:v1",
            start_ms=segment.start_ms,
            end_ms=segment.end_ms,
            segment_ids=(segment_id,),
        )
        evidence.append(
            TranscriptRequirementsEvidence(
                reference={
                    "source_ref": source,
                    "transcript_locator": locator,
                    "segment_id": segment_id,
                    "segment_digest": sha256(
                        segment.reference_text.encode("utf-8")
                    ).hexdigest(),
                    "locator_contract_version": "time-range-locator:v1",
                },
                text=segment.reference_text,
                language="ru",
                anonymous_speaker_label=segment.anonymous_speaker,
                recognition_quality=TranscriptRecognitionQuality.RELIABLE,
                recognition_policy_reference="russian-transcription-eval:v1",
            )
        )
    if not evidence:
        segment_id = _id(case.case_id, "unavailable-segment")
        reverse_segment_ids[segment_id] = "unavailable-segment"
        locator = TimeRangeLocator(
            source_object_id=source.source_object_id,
            source_revision_id=source.source_revision_id,
            source_revision_digest=ISSUE_674_DATASET_SHA256,
            transcript_artifact_id=transcript_id,
            transcript_artifact_digest=sha256(case.case_id.encode("utf-8")).hexdigest(),
            transcript_artifact_schema_version="transcription-artifact:v1",
            start_ms=0,
            end_ms=case.duration_ms,
            segment_ids=(segment_id,),
        )
        evidence.append(
            TranscriptRequirementsEvidence(
                availability=RequirementsEvidenceAvailability.MISSING,
                reference={
                    "source_ref": source,
                    "transcript_locator": locator,
                    "segment_id": segment_id,
                    "segment_digest": sha256(case.case_id.encode("utf-8")).hexdigest(),
                    "locator_contract_version": "time-range-locator:v1",
                },
                text=None,
            )
        )

    workflow_run_id = _id(case.case_id, "workflow-run")
    work_item_id = _id(case.case_id, "work-item")
    capability = extract_requirements_capability()
    version = extract_requirements_agent_version()
    value = ExtractRequirementsInput(
        workspace_id=_id(case.case_id, "workspace"),
        environment_id=_id(case.case_id, "environment"),
        environment=EnvironmentKind.DEVELOPMENT,
        workflow_run_id=workflow_run_id,
        work_item_id=work_item_id,
        client_request=ClientRequestV1(
            message="Evaluate transcript requirements",
            accepted_sources=(source,),
        ),
        authorized_source_refs=((source,) if case.reference_segments else ()),
        evidence=tuple(evidence),
    )
    context = AgentExecutionContext(
        workspace_id=value.workspace_id,
        environment_id=value.environment_id,
        environment=value.environment,
        trusted_identity=TrustedExecutionIdentitySnapshot(
            workspace_id=value.workspace_id,
            environment_id=value.environment_id,
            acting_subject_id=_id(case.case_id, "subject"),
            authorization_generation=1,
        ),
        work_item_id=work_item_id,
        workflow_run_id=workflow_run_id,
        step_run_id=_id(case.case_id, "step-run"),
        agent_run_id=_id(case.case_id, "agent-run"),
        capability=capability,
        schema_identity=CapabilitySchemaIdentity.from_capability(capability),
        agent_version=version,
        context_profile=version.context_profile,
        idempotency_key=f"unsupported-requirements:{case.case_id}",
        diagnostic_context=DiagnosticContext(trace_id=_id(case.case_id, "trace")),
        input_artifact_ids=(_id(case.case_id, "input-artifact"),),
        cancellation=_Cancellation(),
        event_sink=_Events(),
    )
    output = await deterministic_extract_requirements(
        AgentRequest(capability=capability, input=value),
        context,
    )
    return output, reverse_segment_ids


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
