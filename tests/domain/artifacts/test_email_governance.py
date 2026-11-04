from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.domain.artifacts.email_governance import (
    EmailDerivativeProcessingPlan,
    EmailGovernedArtifact,
    EmailLineageRole,
    EmailResolvedLineage,
    EmailSourceLineage,
)
from spine.domain.sources.retention import RetentionArtifactClass


def _artifact(artifact_class: RetentionArtifactClass) -> EmailGovernedArtifact:
    return EmailGovernedArtifact(
        workspace_id=UUID(int=1),
        environment_id=UUID(int=2),
        artifact_class=artifact_class,
        artifact_id=UUID(int=3),
        artifact_version=1,
        artifact_digest="a" * 64,
    )


def _source(role: EmailLineageRole, value: int) -> EmailSourceLineage:
    return EmailSourceLineage(
        role=role,
        source_object_id=UUID(int=value),
        source_revision_id=UUID(int=value + 100),
    )


def test_attachment_lineage_requires_distinct_message_parent_and_attachment_child() -> None:
    artifact = _artifact(RetentionArtifactClass.EMAIL_ATTACHMENT_ORIGINAL)

    with pytest.raises(ValidationError):
        EmailResolvedLineage(
            artifact=artifact,
            sources=(_source(EmailLineageRole.MESSAGE, 10),),
        )

    lineage = EmailResolvedLineage(
        artifact=artifact,
        sources=(
            _source(EmailLineageRole.MESSAGE, 10),
            _source(EmailLineageRole.ATTACHMENT, 20),
        ),
    )
    assert tuple(item.role for item in lineage.sources) == (
        EmailLineageRole.MESSAGE,
        EmailLineageRole.ATTACHMENT,
    )


def test_non_email_retention_class_cannot_be_used_as_email_artifact() -> None:
    with pytest.raises(ValidationError):
        _artifact(RetentionArtifactClass.ORIGINAL)


def test_processing_plan_rejects_duplicate_or_non_email_classes() -> None:
    fields = {
        "workspace_id": UUID(int=1),
        "environment_id": UUID(int=2),
        "source_object_id": UUID(int=3),
        "source_revision_id": UUID(int=4),
    }

    with pytest.raises(ValidationError):
        EmailDerivativeProcessingPlan(
            **fields,
            artifact_classes=(
                RetentionArtifactClass.EMAIL_RAW_MIME,
                RetentionArtifactClass.EMAIL_RAW_MIME,
            ),
        )
    with pytest.raises(ValidationError):
        EmailDerivativeProcessingPlan(
            **fields,
            artifact_classes=(RetentionArtifactClass.ORIGINAL,),
        )
