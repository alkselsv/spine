from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.domain.capabilities import (
    DocumentRequirementsEvidence,
    ExtractRequirementsInput,
    RequirementsEvidenceAvailability,
    RequirementsExecutionConfiguration,
    RequirementsExtractionOutcome,
    RequirementsExtractionOutput,
    RequirementsExtractionReason,
    RequirementsExtractionReasonCode,
    TranscriptRequirementsEvidence,
)
from spine.domain.artifacts import ArtifactRef, PhysicalLineLocator, TimeRangeLocator
from spine.domain.common import EnvironmentKind
from spine.domain.workflows import (
    AcceptedSourceRevisionRef,
    ClientRequestV1,
    Requirement,
    RequirementCategory,
)


def uid(value: int) -> UUID:
    return UUID(int=value)


def extraction_input(**overrides: object) -> ExtractRequirementsInput:
    values: dict[str, object] = {
        "workspace_id": uid(1),
        "environment_id": uid(2),
        "environment": EnvironmentKind.DEVELOPMENT,
        "workflow_run_id": uid(3),
        "work_item_id": uid(4),
        "client_request": ClientRequestV1(message="Prepare a proposal"),
    }
    values.update(overrides)
    return ExtractRequirementsInput(**values)


def test_extract_requirements_input_rejects_unauthorized_source_reference() -> None:
    source = AcceptedSourceRevisionRef(
        source_object_id=uid(10), source_revision_id=uid(11), provenance_id=uid(12)
    )

    with pytest.raises(ValidationError, match="authorized sources"):
        extraction_input(authorized_source_refs=(source,))


def test_extract_requirements_input_accepts_discriminated_document_and_transcript_evidence() -> None:
    source = AcceptedSourceRevisionRef(
        source_object_id=uid(10), source_revision_id=uid(11), provenance_id=uid(12)
    )
    document = DocumentRequirementsEvidence(
        reference={
            "source_ref": source,
            "parsed_artifact_ref": {
                "id": uid(13),
                "schema_name": "parsed-document",
                "schema_version": "parsed-document:v1",
            },
            "segment_id": uid(14),
            "segment_digest": "a" * 64,
            "locator": PhysicalLineLocator(
                line_number=4, start_codepoint=0, end_codepoint=25
            ),
        },
        text="FUNCTIONAL: Export the proposal as PDF.",
    )
    transcript = TranscriptRequirementsEvidence(
        reference={
            "source_ref": source,
            "segment_id": uid(15),
            "segment_digest": "b" * 64,
            "locator_contract_version": "time-range-locator:v1",
            "transcript_locator": TimeRangeLocator(
                source_object_id=source.source_object_id,
                source_revision_id=source.source_revision_id,
                source_revision_digest="c" * 64,
                transcript_artifact_id=uid(16),
                transcript_artifact_digest="d" * 64,
                transcript_artifact_schema_version="transcription-artifact:v1",
                start_ms=1_200,
                end_ms=2_800,
                segment_ids=(uid(15),),
            ),
        },
        text="SCHEDULE: Launch by 20 December.",
        language="ru",
        anonymous_speaker_label="speaker-1",
    )

    result = extraction_input(
        client_request=ClientRequestV1(
            message="Prepare a proposal", accepted_sources=(source,)
        ),
        authorized_source_refs=(source,),
        evidence=(document, transcript),
    )

    assert result.evidence[0].kind == "document"
    assert result.evidence[1].kind == "transcript"
    assert result.evidence[1].reference.transcript_locator is not None
    assert result.evidence[1].reference.transcript_locator.start_ms == 1_200


def test_extract_requirements_input_rejects_discriminator_reference_mismatch() -> None:
    source = AcceptedSourceRevisionRef(
        source_object_id=uid(10), source_revision_id=uid(11), provenance_id=uid(12)
    )
    parsed_reference = {
        "source_ref": source,
        "parsed_artifact_ref": ArtifactRef(
            id=uid(13),
            schema_name="parsed-document",
            schema_version="parsed-document:v1",
        ),
        "segment_id": uid(14),
        "segment_digest": "a" * 64,
        "locator": PhysicalLineLocator(
            line_number=4, start_codepoint=0, end_codepoint=25
        ),
    }

    with pytest.raises(ValidationError, match="transcript evidence requires"):
        TranscriptRequirementsEvidence(
            reference=parsed_reference,
            text="FUNCTIONAL: Export the proposal as PDF.",
        )


def test_extract_requirements_input_keeps_unavailable_evidence_content_free() -> None:
    source = AcceptedSourceRevisionRef(
        source_object_id=uid(10), source_revision_id=uid(11), provenance_id=uid(12)
    )

    with pytest.raises(ValidationError, match="cannot carry protected text"):
        DocumentRequirementsEvidence(
            availability=RequirementsEvidenceAvailability.DENIED,
            reference={
                "source_ref": source,
                "parsed_artifact_ref": {
                    "id": uid(13),
                    "schema_name": "parsed-document",
                    "schema_version": "parsed-document:v1",
                },
                "segment_id": uid(14),
                "segment_digest": "a" * 64,
                "locator": PhysicalLineLocator(
                    line_number=4, start_codepoint=0, end_codepoint=25
                ),
            },
            text="protected",
        )


def test_extract_requirements_output_requires_typed_reason_for_abstention() -> None:
    with pytest.raises(ValidationError, match="abstained extraction"):
        RequirementsExtractionOutput(
            workspace_id=uid(1),
            environment_id=uid(2),
            workflow_run_id=uid(3),
            work_item_id=uid(4),
            outcome=RequirementsExtractionOutcome.ABSTAINED,
        )

    result = RequirementsExtractionOutput(
        workspace_id=uid(1),
        environment_id=uid(2),
        workflow_run_id=uid(3),
        work_item_id=uid(4),
        outcome=RequirementsExtractionOutcome.ABSTAINED,
        reason=RequirementsExtractionReason(
            code=RequirementsExtractionReasonCode.INSUFFICIENT_INFORMATION
        ),
    )
    assert result.reason is not None


def test_extract_requirements_output_rejects_failure_reason_on_partial_result() -> None:
    with pytest.raises(ValidationError, match="partial extraction"):
        RequirementsExtractionOutput(
            workspace_id=uid(1),
            environment_id=uid(2),
            workflow_run_id=uid(3),
            work_item_id=uid(4),
            outcome=RequirementsExtractionOutcome.PARTIAL,
            reason=RequirementsExtractionReason(
                code=RequirementsExtractionReasonCode.PROCESSING_FAILED
            ),
        )


def test_extract_requirements_output_accepts_success_and_typed_failure() -> None:
    success = RequirementsExtractionOutput(
        workspace_id=uid(1),
        environment_id=uid(2),
        workflow_run_id=uid(3),
        work_item_id=uid(4),
        outcome=RequirementsExtractionOutcome.SUCCESS,
        requirements=(
            Requirement(
                requirement_id=uid(30),
                category=RequirementCategory.FUNCTIONAL,
                statement="The proposal must support delivery tracking.",
            ),
        ),
    )
    failure = RequirementsExtractionOutput(
        workspace_id=uid(1),
        environment_id=uid(2),
        workflow_run_id=uid(3),
        work_item_id=uid(4),
        outcome=RequirementsExtractionOutcome.FAILED,
        reason=RequirementsExtractionReason(
            code=RequirementsExtractionReasonCode.PROCESSING_FAILED
        ),
    )
    assert success.requirements[0].statement.startswith("The proposal")
    assert failure.reason is not None


def test_execution_configuration_is_bounded_and_serializable() -> None:
    configuration = RequirementsExecutionConfiguration(
        max_requirements=16, max_clarifications=2
    )
    assert configuration.runtime_constraints() == {
        "max_requirements": 16,
        "max_clarifications": 2,
    }
    with pytest.raises(ValidationError):
        RequirementsExecutionConfiguration(max_requirements=129)
