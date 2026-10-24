from __future__ import annotations

from hashlib import sha256
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from spine.memory.cognee.validation import (
    ArtifactKind,
    PhysicalArtifact,
    PhysicalArtifactLocator,
    PhysicalProjectionExpectation,
    PhysicalProjectionSource,
    PhysicalValidationCode,
    validate_physical_projection,
)


WORKSPACE = UUID("10000000-0000-4000-8000-000000000001")
OTHER_WORKSPACE = UUID("10000000-0000-4000-8000-000000000002")
ENVIRONMENT = UUID("20000000-0000-4000-8000-000000000001")
SOURCE_OBJECT = UUID("30000000-0000-4000-8000-000000000001")
REVISION = UUID("40000000-0000-4000-8000-000000000001")
CONFIG = UUID("50000000-0000-4000-8000-000000000001")

NAMESPACE_DIGEST = "a" * 64
REVISION_DIGEST = "b" * 64
CONFIG_DIGEST = "c" * 64


def locator(value: str = "line:1:0-5") -> PhysicalArtifactLocator:
    return PhysicalArtifactLocator(
        kind="physical_line",
        reference=value,
        digest=sha256(value.encode("utf-8")).hexdigest(),
    )


def source(*, status: str = "current") -> PhysicalProjectionSource:
    return PhysicalProjectionSource(
        source_object_id=SOURCE_OBJECT,
        source_revision_id=REVISION,
        revision_digest=REVISION_DIGEST,
        revision_number=2,
        status=status,
    )


def artifact(
    *,
    kind: ArtifactKind = ArtifactKind.CHUNK,
    artifact_id: str = "chunk-1",
    workspace_id: UUID = WORKSPACE,
    source_revision_id: UUID = REVISION,
    revision_digest: str = REVISION_DIGEST,
    revision_number: int = 2,
    namespace_digest: str = NAMESPACE_DIGEST,
    config_digest: str = CONFIG_DIGEST,
    artifact_locator: PhysicalArtifactLocator | None = None,
    data_id: UUID | None = None,
) -> PhysicalArtifact:
    return PhysicalArtifact(
        artifact_kind=kind,
        artifact_id=artifact_id,
        data_id=data_id or uuid5(NAMESPACE_URL, f"physical-test:{kind.value}:{artifact_id}"),
        workspace_id=workspace_id,
        environment_id=ENVIRONMENT,
        namespace_digest=namespace_digest,
        source_object_id=SOURCE_OBJECT,
        source_revision_id=source_revision_id,
        revision_digest=revision_digest,
        revision_number=revision_number,
        projection_config_id=CONFIG,
        projection_config_version="config-1",
        projection_config_digest=config_digest,
        locators=(artifact_locator or locator(),),
    )


def expectation(
    *,
    expected_artifacts: tuple[PhysicalArtifact, ...] = (artifact(),),
    expected_source: PhysicalProjectionSource | None = None,
) -> PhysicalProjectionExpectation:
    return PhysicalProjectionExpectation(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        namespace_digest=NAMESPACE_DIGEST,
        projection_config_id=CONFIG,
        projection_config_version="config-1",
        projection_config_digest=CONFIG_DIGEST,
        sources=(expected_source or source(),),
        expected_artifacts=expected_artifacts,
    )


def codes(result) -> tuple[PhysicalValidationCode, ...]:
    return tuple(diagnostic.code for diagnostic in result.diagnostics)


def test_valid_physical_projection_passes_with_content_free_diagnostics() -> None:
    result = validate_physical_projection(expectation(), (artifact(),))

    assert result.passed is True
    assert result.diagnostics == ()
    assert result.repair_candidates == ()
    assert tuple((item.artifact_kind.value, item.value) for item in result.counts) == (
        ("chunk", 1),
        ("entity", 0),
        ("relation", 0),
    )


def test_explicit_empty_source_projection_passes_without_artifacts() -> None:
    empty = PhysicalProjectionExpectation(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        namespace_digest=NAMESPACE_DIGEST,
        projection_config_id=CONFIG,
        projection_config_version="config-1",
        projection_config_digest=CONFIG_DIGEST,
        sources=(),
        expected_artifacts=(),
        allows_empty_source_set=True,
    )

    result = validate_physical_projection(empty, ())

    assert result.passed is True
    assert result.diagnostics == ()


def test_empty_source_projection_requires_explicit_allowance() -> None:
    with pytest.raises(ValueError, match="requires at least one source"):
        PhysicalProjectionExpectation(
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            namespace_digest=NAMESPACE_DIGEST,
            projection_config_id=CONFIG,
            projection_config_version="config-1",
            projection_config_digest=CONFIG_DIGEST,
            sources=(),
            expected_artifacts=(),
        )


def test_foreign_scope_namespace_and_configuration_fail_without_content() -> None:
    expected_artifacts = (
        artifact(),
        artifact(artifact_id="chunk-2"),
        artifact(artifact_id="chunk-3"),
        artifact(artifact_id="chunk-4"),
    )
    result = validate_physical_projection(
        expectation(expected_artifacts=expected_artifacts),
        (
            artifact(workspace_id=OTHER_WORKSPACE),
            artifact(artifact_id="chunk-2", namespace_digest="d" * 64),
            artifact(artifact_id="chunk-3", config_digest="e" * 64),
        ),
    )

    assert result.passed is False
    assert codes(result) == (
        PhysicalValidationCode.CROSS_SCOPE,
        PhysicalValidationCode.NAMESPACE_MISMATCH,
        PhysicalValidationCode.CONFIGURATION_MISMATCH,
        PhysicalValidationCode.MISSING_ARTIFACT,
        PhysicalValidationCode.INCOMPLETE_ARTIFACT_SET,
    )
    assert all(diagnostic.protected_content is None for diagnostic in result.diagnostics)


def test_stale_and_tombstoned_sources_are_rejected_and_repairable() -> None:
    stale = artifact(revision_number=1, revision_digest="d" * 64)
    stale_result = validate_physical_projection(expectation(), (stale,))

    tombstone_expectation = expectation(
        expected_artifacts=(),
        expected_source=source(status="tombstoned"),
    )
    tombstone_result = validate_physical_projection(tombstone_expectation, (artifact(),))

    assert codes(stale_result) == (
        PhysicalValidationCode.SOURCE_REVISION_MISMATCH,
        PhysicalValidationCode.STALE_SOURCE,
    )
    assert codes(tombstone_result) == (PhysicalValidationCode.TOMBSTONED_SOURCE,)
    assert len(stale_result.repair_candidates) == 1
    assert stale_result.repair_candidates[0].artifact_id == "chunk-1"


def test_duplicate_missing_and_incomplete_sets_are_deterministic() -> None:
    expected = expectation(
        expected_artifacts=(
            artifact(artifact_id="chunk-1"),
            artifact(kind=ArtifactKind.ENTITY, artifact_id="entity-1"),
            artifact(kind=ArtifactKind.RELATION, artifact_id="relation-1"),
        )
    )
    actual = (
        artifact(artifact_id="chunk-1"),
        artifact(artifact_id="chunk-1"),
        artifact(kind=ArtifactKind.ENTITY, artifact_id="entity-1"),
    )

    first = validate_physical_projection(expected, actual)
    second = validate_physical_projection(expected, tuple(reversed(actual)))

    assert first.model_dump() == second.model_dump()
    assert codes(first) == (
        PhysicalValidationCode.DUPLICATE_ARTIFACT,
        PhysicalValidationCode.MISSING_ARTIFACT,
        PhysicalValidationCode.INCOMPLETE_ARTIFACT_SET,
    )
    assert any(candidate.artifact_id == "relation-1" for candidate in first.repair_candidates)


def test_locator_mismatch_is_reported_as_a_safe_repair_candidate() -> None:
    result = validate_physical_projection(
        expectation(),
        (artifact(artifact_locator=locator("line:2:0-5")),),
    )

    assert codes(result) == (PhysicalValidationCode.LOCATOR_MISMATCH,)
    assert result.repair_candidates[0].action == "rebuild_artifact"


def test_changed_data_identity_is_not_accepted_as_the_same_artifact() -> None:
    result = validate_physical_projection(
        expectation(),
        (
            artifact(
                # The artifact key is unchanged, but Cognee returned another
                # physical object for it.
                # The validator must not silently treat that as a replay.
                artifact_locator=locator(),
            ).model_copy(
                update={"data_id": UUID("60000000-0000-4000-8000-000000000002")}
            ),
        ),
    )

    assert codes(result) == (PhysicalValidationCode.ARTIFACT_IDENTITY_MISMATCH,)


def test_expected_artifact_with_wrong_source_revision_is_not_accepted() -> None:
    wrong_revision = UUID("40000000-0000-4000-8000-000000000099")
    expected_artifact = artifact(
        source_revision_id=wrong_revision,
        revision_digest="d" * 64,
        revision_number=3,
    )
    expected = expectation(expected_artifacts=(expected_artifact,))

    result = validate_physical_projection(expected, (expected_artifact,))

    assert codes(result) == (PhysicalValidationCode.SOURCE_REVISION_MISMATCH,)


def test_valid_entity_and_relation_fixture_sets_pass() -> None:
    expected_artifacts = (
        artifact(
            kind=ArtifactKind.ENTITY,
            artifact_id="entity-1",
        ),
        artifact(
            kind=ArtifactKind.RELATION,
            artifact_id="relation-1",
        ),
    )
    expected = expectation(expected_artifacts=expected_artifacts)

    result = validate_physical_projection(expected, expected_artifacts)

    assert result.passed is True
    assert tuple((item.artifact_kind.value, item.value) for item in result.counts) == (
        ("chunk", 0),
        ("entity", 1),
        ("relation", 1),
    )


def test_two_artifact_keys_cannot_share_one_physical_data_id() -> None:
    shared_data_id = UUID("60000000-0000-4000-8000-000000000099")
    expected_artifacts = (
        artifact(artifact_id="chunk-1", data_id=shared_data_id),
        artifact(kind=ArtifactKind.ENTITY, artifact_id="entity-1", data_id=shared_data_id),
    )
    expected = expectation(expected_artifacts=expected_artifacts)

    result = validate_physical_projection(expected, expected_artifacts)

    assert result.passed is False
    assert all(
        diagnostic.code is PhysicalValidationCode.DUPLICATE_DATA_ID
        for diagnostic in result.diagnostics
    )


def test_locator_order_does_not_change_the_validation_digest_or_result() -> None:
    first = locator("line:1:0-5")
    second = locator("line:2:0-5")
    expected_artifact = artifact().model_copy(update={"locators": (first, second)})
    actual_artifact = expected_artifact.model_copy(update={"locators": (second, first)})
    expected = expectation(expected_artifacts=(expected_artifact,))

    result = validate_physical_projection(expected, (actual_artifact,))

    assert result.passed is True
