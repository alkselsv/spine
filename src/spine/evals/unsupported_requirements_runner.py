"""Deterministic runner for the approved unsupported-requirements gate."""

from __future__ import annotations

import argparse
import asyncio
from hashlib import sha256
from uuid import UUID

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
from spine.domain.artifacts import TimeRangeLocator
from spine.domain.capabilities import (
    ExtractRequirementsInput,
    RequirementsEvidenceAvailability,
    RequirementsExecutionConfiguration,
    TranscriptRecognitionQuality,
    TranscriptRequirementsEvidence,
)
from spine.domain.common import EnvironmentKind
from spine.domain.workflows import AcceptedSourceRevisionRef, ClientRequestV1
from spine.evals.unsupported_requirements_gate import (
    UnsupportedRequirementsEvalReport,
    UnsupportedRequirementsEvaluationIdentity,
    UnsupportedRequirementsObservation,
    classify_requirements_output,
    evaluate_unsupported_requirements,
    load_unsupported_requirements_dataset,
)


def _uid(value: int) -> UUID:
    return UUID(int=value)


class _Cancellation:
    is_cancelled = False

    def raise_if_cancelled(self) -> None:
        return None


class _Events:
    async def emit(self, event: object) -> None:
        return None


def _execution_context(case_ordinal: int) -> AgentExecutionContext:
    capability = extract_requirements_capability()
    version = extract_requirements_agent_version()
    return AgentExecutionContext(
        workspace_id=_uid(1),
        environment_id=_uid(2),
        work_item_id=_uid(3),
        environment=EnvironmentKind.DEVELOPMENT,
        trusted_identity=TrustedExecutionIdentitySnapshot(
            workspace_id=_uid(1),
            environment_id=_uid(2),
            acting_subject_id=_uid(9),
            authorization_generation=1,
        ),
        workflow_run_id=_uid(100 + case_ordinal),
        step_run_id=_uid(5),
        agent_run_id=_uid(6),
        capability=capability,
        schema_identity=CapabilitySchemaIdentity.from_capability(capability),
        agent_version=version,
        context_profile=version.context_profile,
        idempotency_key=f"eval:unsupported-requirements:v1:{case_ordinal}",
        diagnostic_context=DiagnosticContext(trace_id=_uid(7)),
        input_artifact_ids=(_uid(8),),
        cancellation=_Cancellation(),
        event_sink=_Events(),
    )


async def run_deterministic_unsupported_requirements_qualification(
    *, tested_commit_sha: str
) -> UnsupportedRequirementsEvalReport:
    """Execute all frozen cases through the existing deterministic capability."""

    dataset = load_unsupported_requirements_dataset()
    capability = extract_requirements_capability()
    version = extract_requirements_agent_version()
    execution = RequirementsExecutionConfiguration()
    observations: list[UnsupportedRequirementsObservation] = []

    for ordinal, case in enumerate(dataset.cases, start=1):
        source = AcceptedSourceRevisionRef(
            source_object_id=_uid(20),
            source_revision_id=_uid(21),
            provenance_id=_uid(22),
        )
        segment_id = _uid(1_000 + ordinal)
        locator = TimeRangeLocator(
            source_object_id=source.source_object_id,
            source_revision_id=source.source_revision_id,
            source_revision_digest="a" * 64,
            transcript_artifact_id=_uid(23),
            transcript_artifact_digest="b" * 64,
            transcript_artifact_schema_version="transcription-artifact:v1",
            start_ms=ordinal * 1_000,
            end_ms=ordinal * 1_000 + 900,
            segment_ids=(segment_id,),
        )
        availability = RequirementsEvidenceAvailability(case.availability)
        evidence = TranscriptRequirementsEvidence(
            availability=availability,
            reference={
                "source_ref": source,
                "transcript_locator": locator,
                "segment_id": segment_id,
                "segment_digest": sha256(case.transcript.encode()).hexdigest(),
                "locator_contract_version": "time-range-locator:v1",
            },
            text=(
                case.transcript
                if availability is RequirementsEvidenceAvailability.CURRENT
                else None
            ),
            anonymous_speaker_label=("speaker-1" if case.speaker_known else None),
            recognition_quality=TranscriptRecognitionQuality(case.recognition_quality),
            recognition_policy_reference="transcript-recognition-quality:v1",
        )
        request = ExtractRequirementsInput(
            workspace_id=_uid(1),
            environment_id=_uid(2),
            environment=EnvironmentKind.DEVELOPMENT,
            workflow_run_id=_uid(100 + ordinal),
            work_item_id=_uid(3),
            client_request=ClientRequestV1(
                message="Prepare proposal requirements",
                accepted_sources=(source,),
            ),
            authorized_source_refs=(
                (source,)
                if availability is RequirementsEvidenceAvailability.CURRENT
                else ()
            ),
            evidence=(evidence,),
            execution=execution,
        )
        output = await deterministic_extract_requirements(
            AgentRequest(capability=capability, input=request),
            _execution_context(ordinal),
        )
        observations.append(
            UnsupportedRequirementsObservation(
                case_id=case.case_id,
                predicted_class=classify_requirements_output(
                    evidence_availability=availability,
                    output=output,
                ),
            )
        )

    return evaluate_unsupported_requirements(
        dataset=dataset,
        observations=tuple(observations),
        identity=UnsupportedRequirementsEvaluationIdentity(
            tested_commit_sha=tested_commit_sha,
            capability_id=capability.id,
            input_schema=(
                f"{capability.input_schema.name}:{capability.input_schema.version}"
            ),
            output_schema=(
                f"{capability.output_schema.name}:{capability.output_schema.version}"
            ),
            agent_version_id=version.id,
            implementation_key=EXTRACT_REQUIREMENTS_IMPLEMENTATION_KEY,
            execution_configuration=execution,
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the deterministic unsupported-requirements qualification gate."
    )
    parser.add_argument("--tested-commit", required=True)
    args = parser.parse_args()
    report = asyncio.run(
        run_deterministic_unsupported_requirements_qualification(
            tested_commit_sha=args.tested_commit
        )
    )
    print(report.model_dump_json(indent=2))
    return 0 if report.result.value == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["run_deterministic_unsupported_requirements_qualification"]
