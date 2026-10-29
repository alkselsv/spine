from datetime import datetime, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.domain.artifacts import ArtifactRef, PhysicalLineLocator
from spine.domain.workflows import (
    AcceptedSourceRevisionRef,
    Clarification,
    ClarificationAnswer,
    ClarificationReason,
    Requirement,
    RequirementEvidenceRef,
    RequirementEvidenceRelationship,
    RequirementEvidenceStatus,
    RequirementEvidenceValidationStatus,
    RequirementState,
    RequirementsArtifact,
)
from spine.domain.common import ActorKind, ActorRef


def test_requirement_defaults_to_unaccepted_candidate() -> None:
    requirement = Requirement(
        requirement_id=UUID(int=1), category="functional", statement="Deliver by Friday"
    )

    assert requirement.state is RequirementState.CANDIDATE
    assert requirement.review_state == "needs_review"
    assert requirement.evidence_status is RequirementEvidenceStatus.UNVERIFIED


def test_requirement_preserves_exact_source_and_parsed_evidence_lineage() -> None:
    source_ref = AcceptedSourceRevisionRef(
        source_object_id=UUID(int=10), source_revision_id=UUID(int=11), provenance_id=UUID(int=12)
    )
    evidence_ref = RequirementEvidenceRef(
        source_ref=source_ref,
        parsed_artifact_ref=ArtifactRef(
            id=UUID(int=13), schema_name="parsed-document", schema_version="parsed-document:v1"
        ),
        segment_id=UUID(int=14),
        segment_digest="a" * 64,
        locator=PhysicalLineLocator(
            kind="physical_line", line_number=4, start_codepoint=5, end_codepoint=21
        ),
    )
    requirement = Requirement(
        requirement_id=UUID(int=1), category="functional", statement="Deliver by Friday",
        source_refs=(source_ref,), evidence_refs=(evidence_ref,),
        evidence_status=RequirementEvidenceStatus.REFERENCED,
    )

    assert requirement.evidence_refs == (evidence_ref,)
    assert requirement.evidence_refs[0].locator.line_number == 4
    assert requirement.evidence_refs[0].relationship is RequirementEvidenceRelationship.SUPPORTS
    assert requirement.evidence_refs[0].validation is None


def test_valid_evidence_requires_current_authorization_lineage() -> None:
    source_ref = AcceptedSourceRevisionRef(
        source_object_id=UUID(int=10), source_revision_id=UUID(int=11), provenance_id=UUID(int=12)
    )
    with pytest.raises(ValidationError, match="authorization decision"):
        RequirementEvidenceRef(
            source_ref=source_ref,
            parsed_artifact_ref=ArtifactRef(
                id=UUID(int=13), schema_name="parsed-document", schema_version="parsed-document:v1"
            ),
            segment_id=UUID(int=14),
            segment_digest="a" * 64,
            locator=PhysicalLineLocator(
                kind="physical_line", line_number=4, start_codepoint=0, end_codepoint=5
            ),
            validation={"status": "valid", "validation_reference": UUID(int=15)},
        )


def test_requirement_rejects_evidence_from_an_unreferenced_source_revision() -> None:
    evidence_ref = RequirementEvidenceRef(
        source_ref=AcceptedSourceRevisionRef(
            source_object_id=UUID(int=10), source_revision_id=UUID(int=11), provenance_id=UUID(int=12)
        ),
        parsed_artifact_ref=ArtifactRef(
            id=UUID(int=13), schema_name="parsed-document", schema_version="parsed-document:v1"
        ),
        segment_id=UUID(int=14),
        segment_digest="a" * 64,
        locator=PhysicalLineLocator(
            kind="physical_line", line_number=4, start_codepoint=5, end_codepoint=21
        ),
    )
    with pytest.raises(ValidationError, match="evidence must belong"):
        Requirement(
            requirement_id=UUID(int=1), category="functional", statement="Deliver by Friday",
            evidence_refs=(evidence_ref,), evidence_status=RequirementEvidenceStatus.REFERENCED,
        )


def test_requirements_artifact_preserves_exact_run_scope_and_version() -> None:
    artifact = RequirementsArtifact(
        artifact_id=UUID(int=2),
        artifact_version=1,
        production_identity="requirements-extraction-v1",
        production_digest="0" * 64,
        workspace_id=UUID(int=3),
        environment_id=UUID(int=4),
        work_item_id=UUID(int=5),
        run_id=UUID(int=6),
        requirements=(
            Requirement(
                requirement_id=UUID(int=1), category="functional", statement="Friday"
            ),
        ),
    )

    assert (artifact.workspace_id, artifact.environment_id, artifact.work_item_id, artifact.run_id) == (
        UUID(int=3), UUID(int=4), UUID(int=5), UUID(int=6)
    )
    assert artifact.artifact_version == 1


def test_clarification_preserves_question_answer_and_successor_lineage() -> None:
    asked_at = datetime(2026, 10, 29, 9, tzinfo=timezone.utc)
    answered_at = datetime(2026, 10, 29, 10, tzinfo=timezone.utc)
    clarification = Clarification(
        clarification_id=UUID(int=20),
        run_id=UUID(int=6),
        requirement_id=UUID(int=1),
        originating_artifact_id=UUID(int=2),
        originating_artifact_version=1,
        question="Please provide the delivery deadline.",
        reason=ClarificationReason.MISSING_INFORMATION,
        asked_by=ActorRef(kind=ActorKind.AGENT, id=UUID(int=21)),
        asked_event_id=UUID(int=22),
        asked_at=asked_at,
        correlation_id=UUID(int=23),
        answer=ClarificationAnswer(
            text="December 20.",
            answered_by=ActorRef(kind=ActorKind.HUMAN, id=UUID(int=24)),
            answered_event_id=UUID(int=25),
            answered_at=answered_at,
        ),
        resulting_artifact_version=2,
        affected_validation_references=(UUID(int=26),),
    )
    artifact = RequirementsArtifact(
        artifact_id=UUID(int=2),
        artifact_version=2,
        predecessor_version=1,
        production_identity="clarification-answer:25",
        production_digest="0" * 64,
        workspace_id=UUID(int=3),
        environment_id=UUID(int=4),
        work_item_id=UUID(int=5),
        run_id=UUID(int=6),
        requirements=(
            Requirement(
                requirement_id=UUID(int=1),
                category="functional",
                statement="Deliver by the confirmed deadline.",
            ),
        ),
        clarifications=(clarification,),
    )

    assert artifact.clarifications[0].answer is not None
    assert artifact.clarifications[0].answer.text == "December 20."
    assert artifact.clarifications[0].resulting_artifact_version == 2


def test_open_clarification_rejects_answer_only_lineage() -> None:
    with pytest.raises(ValidationError, match="answered clarification"):
        Clarification(
            clarification_id=UUID(int=20),
            run_id=UUID(int=6),
            requirement_id=UUID(int=1),
            originating_artifact_id=UUID(int=2),
            originating_artifact_version=1,
            question="Please clarify the scope.",
            reason="ambiguous_information",
            asked_by=ActorRef(kind=ActorKind.AGENT, id=UUID(int=21)),
            asked_event_id=UUID(int=22),
            asked_at=datetime(2026, 10, 29, 9, tzinfo=timezone.utc),
            correlation_id=UUID(int=23),
            resulting_artifact_version=2,
        )


def test_requirement_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        Requirement(
            requirement_id=UUID(int=1),
            category="functional",
            statement="Friday",
            accepted=True,
        )


def test_requirement_rejects_unknown_category() -> None:
    with pytest.raises(ValidationError):
        Requirement(
            requirement_id=UUID(int=1),
            category="pricing",
            statement="Friday",
        )


@pytest.mark.parametrize(
    ("state", "review_state"),
    [
        (RequirementState.PARTIAL, "needs_review"),
        (RequirementState.UNSUPPORTED, "needs_review"),
        (RequirementState.UNVERIFIED, "needs_review"),
    ],
)
def test_non_candidate_outcomes_remain_unaccepted(state: RequirementState, review_state: str) -> None:
    requirement = Requirement(
        requirement_id=UUID(int=11), category="unresolved_information", statement="No date was confirmed",
        state=state, review_state=review_state,
        confidence=0.99,
    )

    assert requirement.state is state
    assert requirement.review_state.value == "needs_review"


def test_requirement_rejects_invalid_confidence_and_zero_lineage() -> None:
    with pytest.raises(ValidationError):
        Requirement(
            requirement_id=UUID(int=12), category="functional", statement="Friday",
            confidence=1.1,
        )
    with pytest.raises(ValidationError):
        Requirement(
            requirement_id=UUID(int=12), category="functional", statement="Friday",
            upstream_artifact_ids=(UUID(int=0),),
        )
