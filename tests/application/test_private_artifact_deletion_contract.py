from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.application.private_asr_corpus import (
    PrivateArtifactDeletionRequest,
    PrivateArtifactDeletionRequestV2,
)
from spine.domain.evaluations.private_asr_corpus import PrivateReference
from spine.domain.sources.retention import RetentionArtifactClass

NOW = datetime(2026, 11, 4, tzinfo=timezone.utc)
WORKSPACE = UUID(int=1)
ENVIRONMENT = UUID(int=2)


def _id(value: int) -> UUID:
    return UUID(int=value)


def _request(*, source_ids: tuple[int, ...] = (10, 11), reverse: bool = False):
    lineage = [
        {
            "source_object_id": _id(source_id),
            "source_revision_id": _id(source_id + 100),
            "source_revision_digest": f"{source_id:064x}",
        }
        for source_id in source_ids
    ]
    bindings = [
        {
            "workspace_id": WORKSPACE,
            "environment_id": ENVIRONMENT,
            "source_object_id": item["source_object_id"],
            "source_revision_id": item["source_revision_id"],
            "revision_digest": item["source_revision_digest"],
            "applicability_id": _id(source_id + 200),
            "applicability_generation": 1,
            "expected_current_applicability_id": _id(source_id + 200),
            "expected_current_generation": 1,
            "artifact_class": RetentionArtifactClass.PRIVATE_ASR_SOURCE_ORIGINAL,
            "retention_policy_id": _id(299),
            "retention_policy_version_id": _id(300),
            "retention_policy_digest": "a" * 64,
            "approving_authority_reference": _id(301),
            "approving_authority_version_id": _id(302),
            "approving_authority_digest": "f" * 64,
            "applicability_evidence_reference": _id(400),
            "applicability_evidence_digest": "b" * 64,
            "approval_evidence_reference": _id(500),
            "terms_version": "retention:v1",
            "terms_digest": "c" * 64,
            "deletion_obligation_reference": _id(600),
            "backup_obligation_reference": _id(700),
        }
        for source_id, item in zip(source_ids, lineage, strict=True)
    ]
    if reverse:
        lineage.reverse()
        bindings.reverse()
    return PrivateArtifactDeletionRequestV2(
        artifact_id=_id(800), workspace_id=WORKSPACE, environment_id=ENVIRONMENT,
        purpose="asr_model_evaluation", artifact_kind="original",
        action="retention_expiration", expected_state_digest="d" * 64,
        retention_profile_ref=PrivateReference(reference_id=_id(900), reference_kind="retention_profile"),
        retention_deadline=NOW, requested_at=NOW,
        expected_source_lineage=tuple(lineage), source_bindings=tuple(bindings),
        authority_version="deletion-authority:v1", valid_until=NOW + timedelta(minutes=5),
    )


def test_v2_canonicalizes_identity_keyed_bindings_and_digest() -> None:
    first = _request()
    reversed_input = _request(reverse=True)

    assert first.source_bindings == reversed_input.source_bindings
    assert first.expected_source_lineage == reversed_input.expected_source_lineage
    assert first.calculate_digest() == reversed_input.calculate_digest()
    assert first.model_dump(mode="json")["schema_version"] == "private-asr-artifact-deletion-request:v2"


@pytest.mark.parametrize("binding_count", [1, 3])
def test_v2_rejects_missing_or_extra_source_binding(binding_count: int) -> None:
    request = _request()
    changed = request.model_dump(mode="python")
    if binding_count == 1:
        changed["source_bindings"] = request.source_bindings[:1]
    else:
        changed["source_bindings"] = request.source_bindings + (request.source_bindings[0].model_copy(update={"source_object_id": _id(999)}),)
    with pytest.raises(ValidationError, match="exactly cover artifact lineage"):
        PrivateArtifactDeletionRequestV2.model_validate(changed)


def test_v2_rejects_duplicate_source_binding_identity() -> None:
    request = _request()
    with pytest.raises(ValidationError, match="distinct exact identities"):
        PrivateArtifactDeletionRequestV2.model_validate({**request.model_dump(mode="python"), "source_bindings": request.source_bindings + (request.source_bindings[0],)})


def test_v2_rejects_substituted_revision_digest() -> None:
    request = _request()
    substituted = request.source_bindings[0].model_copy(update={"revision_digest": "e" * 64})
    with pytest.raises(ValidationError, match="exactly cover artifact lineage"):
        PrivateArtifactDeletionRequestV2.model_validate({**request.model_dump(mode="python"), "source_bindings": (substituted, *request.source_bindings[1:])})


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("workspace_id", _id(999), "share artifact workspace"),
        ("environment_id", _id(999), "share artifact workspace"),
        ("valid_until", NOW, "validity must extend"),
        ("requested_at", NOW - timedelta(seconds=1), "cannot precede its deadline"),
    ],
)
def test_v2_rejects_scope_and_authority_fence_changes(field: str, value: object, message: str) -> None:
    request = _request()
    with pytest.raises(ValidationError, match=message):
        PrivateArtifactDeletionRequestV2.model_validate({**request.model_dump(mode="python"), field: value})


def test_v2_rejects_stale_applicability_generation() -> None:
    request = _request()
    binding = request.source_bindings[0].model_dump(mode="python")
    binding["expected_current_generation"] = 2
    with pytest.raises(ValidationError, match="current applicability head"):
        type(request.source_bindings[0]).model_validate(binding)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("purpose", "Bad Purpose"),
        ("artifact_kind", "unknown"),
        ("action", "unknown"),
        ("expected_state_digest", "not-a-digest"),
        ("authority_version", "Bad Version"),
    ],
)
def test_v2_rejects_malformed_authority_and_artifact_identity(field: str, value: object) -> None:
    request = _request()
    with pytest.raises(ValidationError):
        PrivateArtifactDeletionRequestV2.model_validate({**request.model_dump(mode="python"), field: value})


def test_v2_rejects_retention_class_substitution() -> None:
    request = _request()
    changed = request.source_bindings[0].model_dump(mode="python")
    changed["artifact_class"] = RetentionArtifactClass.PRIVATE_ASR_ANNOTATION
    with pytest.raises(ValidationError, match="current applicability head|retention class"):
        PrivateArtifactDeletionRequestV2.model_validate({
            **request.model_dump(mode="python"),
            "source_bindings": (type(request.source_bindings[0]).model_validate(changed), *request.source_bindings[1:]),
        })


def test_v1_serialization_and_digest_remain_stable() -> None:
    request = PrivateArtifactDeletionRequest(
        artifact_id=_id(1), workspace_id=WORKSPACE, environment_id=ENVIRONMENT,
        artifact_kind="original", action="retention_expiration",
        expected_state_digest="a" * 64,
        retention_profile_ref=PrivateReference(reference_id=_id(2), reference_kind="retention_profile"),
        retention_deadline=NOW, requested_at=NOW,
        source_lineage=({"source_object_id": _id(3), "source_revision_id": _id(4),
                         "source_revision_digest": "b" * 64},),
    )
    assert request.model_dump(mode="json")["schema_version"] == "private-asr-artifact-deletion-request:v1"
    assert request.calculate_digest() == "a61548327b41a657a433e561de51eee7260cb2872fc01d7e66f894eae6b74294"
