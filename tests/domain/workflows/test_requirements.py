from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.domain.workflows import Requirement, RequirementState, RequirementsArtifact


def test_requirement_defaults_to_unaccepted_candidate() -> None:
    requirement = Requirement(
        requirement_id=UUID(int=1), category="functional", statement="Deliver by Friday"
    )

    assert requirement.state is RequirementState.CANDIDATE
    assert requirement.review_state == "needs_review"


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
