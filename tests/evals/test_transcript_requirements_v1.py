from hashlib import sha256
from uuid import UUID

import pytest

from spine.agents.runtime import (
    AgentExecutionContext,
    AgentRequest,
    CapabilitySchemaIdentity,
    TrustedExecutionIdentitySnapshot,
)
from spine.application.diagnostics import DiagnosticContext
from spine.application.extract_requirements import (
    deterministic_extract_requirements,
    extract_requirements_agent_version,
    extract_requirements_capability,
)
from spine.domain.capabilities import (
    ExtractRequirementsInput,
    RequirementsEvidenceAvailability,
    TranscriptRecognitionQuality,
    TranscriptRequirementsEvidence,
)
from spine.domain.common import EnvironmentKind
from spine.domain.artifacts import TimeRangeLocator
from spine.domain.workflows import AcceptedSourceRevisionRef, ClientRequestV1
from spine.evals.transcript_requirements_v1 import (
    evaluate_transcript_requirements,
    load_transcript_requirements_dataset,
)


def uid(value: int) -> UUID:
    return UUID(int=value)


class Cancellation:
    is_cancelled = False

    def raise_if_cancelled(self) -> None:
        return None


class Events:
    async def emit(self, event) -> None:
        return None


def execution_context() -> AgentExecutionContext:
    capability = extract_requirements_capability()
    version = extract_requirements_agent_version()
    return AgentExecutionContext(
        workspace_id=uid(1), environment_id=uid(2), work_item_id=uid(3),
        environment=EnvironmentKind.DEVELOPMENT,
        trusted_identity=TrustedExecutionIdentitySnapshot(
            workspace_id=uid(1), environment_id=uid(2),
            acting_subject_id=uid(9), authorization_generation=1,
        ),
        workflow_run_id=uid(4), step_run_id=uid(5), agent_run_id=uid(6),
        capability=capability,
        schema_identity=CapabilitySchemaIdentity.from_capability(capability),
        agent_version=version, context_profile=version.context_profile,
        idempotency_key="eval:transcript-requirements:v1",
        diagnostic_context=DiagnosticContext(trace_id=uid(7)),
        input_artifact_ids=(uid(8),), cancellation=Cancellation(), event_sink=Events(),
    )


@pytest.mark.asyncio
async def test_versioned_transcript_requirements_dataset_passes_deterministic_evaluator() -> None:
    dataset = load_transcript_requirements_dataset()
    source = AcceptedSourceRevisionRef(
        source_object_id=uid(20), source_revision_id=uid(21), provenance_id=uid(22)
    )
    results = []
    for ordinal, case in enumerate(dataset.cases, start=1):
        segment_id = uid(30 + ordinal)
        locator = TimeRangeLocator(
            source_object_id=source.source_object_id,
            source_revision_id=source.source_revision_id,
            source_revision_digest="a" * 64,
            transcript_artifact_id=uid(23),
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
            anonymous_speaker_label="speaker-1" if case.speaker_known else None,
            recognition_quality=TranscriptRecognitionQuality(case.recognition_quality),
            recognition_policy_reference="transcript-recognition-quality:v1",
        )
        value = ExtractRequirementsInput(
            workspace_id=uid(1), environment_id=uid(2),
            environment=EnvironmentKind.DEVELOPMENT,
            workflow_run_id=uid(4), work_item_id=uid(3),
            client_request=ClientRequestV1(
                message="Prepare proposal requirements", accepted_sources=(source,)
            ),
            authorized_source_refs=(
                (source,)
                if availability is RequirementsEvidenceAvailability.CURRENT
                else ()
            ),
            evidence=(evidence,),
        )
        output = await deterministic_extract_requirements(
            AgentRequest(capability=extract_requirements_capability(), input=value),
            execution_context(),
        )
        results.append((case, output))

    report = evaluate_transcript_requirements(tuple(results))

    assert report.passed, report.failures
    assert "pricingbasis" not in "".join(output.model_dump_json().lower() for _, output in results)
