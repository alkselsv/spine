import hashlib
import json
from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.application.private_asr_corpus import (
    CompositeArtifactRetentionDecision,
    CompositeDeletionAuthorizationEvidence,
    CompositeSourceRetentionState,
    PrivateArtifactSourceLineage,
)
from spine.domain.evaluations.private_asr_corpus import PrivateReference


def _source(index: int, **updates: object) -> CompositeSourceRetentionState:
    values: dict[str, object] = {
        "source_object_id": UUID(int=index),
        "source_revision_id": UUID(int=index + 100),
        "source_revision_digest": f"{index:064x}",
        "current_generation": 2,
        "resolved_generation": 2,
        "current_applicability_resolved": True,
        "required_permission_current": True,
        "revoked": False,
        "deletion_eligible": True,
        "retention_elapsed": True,
        "active_hold": False,
    }
    values.update(updates)
    return CompositeSourceRetentionState.model_validate(values)


def _decision(*states: CompositeSourceRetentionState, authorized: bool = False):
    lineage = tuple(
        PrivateArtifactSourceLineage(
            source_object_id=state.source_object_id,
            source_revision_id=state.source_revision_id,
            source_revision_digest=state.source_revision_digest,
        )
        for state in states
    )
    lineage_digest = hashlib.sha256(
        json.dumps(
            sorted(
                (
                    str(source.source_object_id),
                    str(source.source_revision_id),
                    source.source_revision_digest,
                )
                for source in lineage
            ),
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return CompositeArtifactRetentionDecision(
        artifact_id=UUID(int=999),
        workspace_id=UUID(int=998),
        environment_id=UUID(int=997),
        expected_source_lineage=lineage,
        source_states=states,
        whole_artifact_authorization=(
            CompositeDeletionAuthorizationEvidence(
                artifact_id=UUID(int=999),
                workspace_id=UUID(int=998),
                environment_id=UUID(int=997),
                lineage_sha256=lineage_digest,
                authorization_ref=PrivateReference(
                    reference_id=UUID(int=900),
                    reference_kind="private_asr_deletion_authorization",
                ),
            )
            if authorized
            else None
        ),
    )


def test_composite_all_sources_eligible_requires_one_whole_artifact_authorization() -> None:
    decision = _decision(_source(1), _source(2))

    assert decision.available
    assert decision.deletion_eligible
    assert not decision.deletion_permitted
    assert _decision(_source(1), _source(2), authorized=True).deletion_permitted


@pytest.mark.parametrize(
    "source, expected_available, expected_deletion_eligible",
    [
        (_source(1, required_permission_current=False), False, True),
        (_source(1, deletion_eligible=False), True, False),
        (_source(1, active_hold=True), True, False),
        (_source(1, resolved_generation=1), False, False),
        (_source(1, revoked=True), False, True),
        (_source(1, retention_elapsed=False), True, False),
        (_source(1, current_applicability_resolved=False), False, False),
    ],
)
def test_one_source_state_controls_whole_composite(
    source: CompositeSourceRetentionState,
    expected_available: bool,
    expected_deletion_eligible: bool,
) -> None:
    decision = _decision(_source(2), source)

    assert decision.available is expected_available
    assert decision.deletion_eligible is expected_deletion_eligible
    assert not decision.deletion_permitted


def test_composite_rejects_partial_authorization_when_one_source_is_held() -> None:
    with pytest.raises(ValidationError):
        _decision(_source(1), _source(2, active_hold=True), authorized=True)


def test_composite_rejects_duplicate_source_identity() -> None:
    with pytest.raises(ValidationError):
        _decision(_source(1), _source(1))


def test_composite_rejects_missing_source_state() -> None:
    source = _source(1)
    with pytest.raises(ValidationError):
        CompositeArtifactRetentionDecision(
            artifact_id=UUID(int=999),
            workspace_id=UUID(int=998),
            environment_id=UUID(int=997),
            expected_source_lineage=(
                PrivateArtifactSourceLineage(
                    source_object_id=source.source_object_id,
                    source_revision_id=source.source_revision_id,
                    source_revision_digest=source.source_revision_digest,
                ),
                PrivateArtifactSourceLineage(
                    source_object_id=UUID(int=2),
                    source_revision_id=UUID(int=102),
                    source_revision_digest=f"{2:064x}",
                ),
            ),
            source_states=(source,),
        )


def test_composite_rejects_authorization_for_another_artifact_scope() -> None:
    decision = _decision(_source(1), authorized=True)
    mismatched_evidence = decision.whole_artifact_authorization.model_copy(
        update={"environment_id": UUID(int=996)}
    )

    with pytest.raises(ValidationError):
        CompositeArtifactRetentionDecision.model_validate(
            {
                **decision.model_dump(),
                "whole_artifact_authorization": mismatched_evidence,
            }
        )
