from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.domain.capabilities import (
    ExtractRequirementsInput,
    RequirementsExecutionConfiguration,
    RequirementsExtractionOutcome,
    RequirementsExtractionOutput,
    RequirementsExtractionReason,
    RequirementsExtractionReasonCode,
)
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
