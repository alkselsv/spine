"""Fail-closed validation of Cognee's physical projection metadata.

This module validates adapter observations only.  It does not decide which
projection is active and it does not create a canonical ``ProjectionValidation``
record.  The output is deliberately content-free so that it can be handed to
the Issue #7 persistence boundary without granting the provider authorization.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping
from enum import Enum
from hashlib import sha256
import json
from typing import Literal, Protocol
from uuid import UUID

from pydantic import Field, model_validator

from spine.domain.common import DefinitionModel


_DIGEST = r"[0-9a-f]{64}"
_IDENTIFIER = r"^[a-z][a-z0-9_.:-]{0,127}$"
_ARTIFACT_IDENTIFIER = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"


def _require_id(value: UUID, field_name: str) -> None:
    if value.int == 0:
        raise ValueError(f"{field_name} must be a non-zero UUID")


class ArtifactKind(str, Enum):
    """The physical artifact families produced by the Cognee adapter."""

    CHUNK = "chunk"
    ENTITY = "entity"
    RELATION = "relation"


class PhysicalValidationCode(str, Enum):
    """Stable, content-free reasons for rejecting a physical observation."""

    CROSS_SCOPE = "cross_scope"
    NAMESPACE_MISMATCH = "namespace_mismatch"
    CONFIGURATION_MISMATCH = "configuration_mismatch"
    SOURCE_REVISION_MISMATCH = "source_revision_mismatch"
    ARTIFACT_IDENTITY_MISMATCH = "artifact_identity_mismatch"
    STALE_SOURCE = "stale_source"
    TOMBSTONED_SOURCE = "tombstoned_source"
    LOCATOR_MISMATCH = "locator_mismatch"
    DUPLICATE_ARTIFACT = "duplicate_artifact"
    DUPLICATE_DATA_ID = "duplicate_data_id"
    UNKNOWN_ARTIFACT = "unknown_artifact"
    MISSING_ARTIFACT = "missing_artifact"
    INCOMPLETE_ARTIFACT_SET = "incomplete_artifact_set"


class PhysicalArtifactLocator(DefinitionModel):
    """A stable, content-free locator and its canonical digest."""

    schema_version: Literal["physical-artifact-locator:v1"] = (
        "physical-artifact-locator:v1"
    )
    kind: str = Field(min_length=1, max_length=64, pattern=_IDENTIFIER)
    reference: str = Field(min_length=1, max_length=1024)
    digest: str = Field(pattern=_DIGEST)

    @model_validator(mode="after")
    def validate_digest(self) -> "PhysicalArtifactLocator":
        expected = sha256(self.reference.encode("utf-8")).hexdigest()
        if self.digest != expected:
            raise ValueError("physical artifact locator digest does not match")
        return self

    @classmethod
    def from_payload(cls, *, kind: str, payload: object) -> "PhysicalArtifactLocator":
        """Build a locator reference from a provider-neutral locator model."""

        if hasattr(payload, "model_dump"):
            payload = payload.model_dump(mode="json")
        if isinstance(payload, Mapping):
            encoded = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        else:
            encoded = str(payload)
        return cls(
            kind=kind,
            reference=encoded,
            digest=sha256(encoded.encode("utf-8")).hexdigest(),
        )


class _ChunkMapping(Protocol):
    segment_id: UUID
    data_id: UUID
    workspace_id: UUID
    environment_id: UUID
    namespace_digest: str
    source_object_id: UUID
    source_revision_id: UUID
    revision_digest: str
    revision_number: int
    projection_config_id: UUID
    projection_config_version: str
    projection_config_digest: str
    locator: object


class _GraphMapping(Protocol):
    artifact_kind: str
    artifact_id: str
    data_id: UUID
    workspace_id: UUID
    environment_id: UUID
    namespace_digest: str
    source_object_id: UUID
    source_revision_id: UUID
    revision_digest: str
    revision_number: int
    projection_config_id: UUID
    projection_config_version: str
    projection_config_digest: str
    locators: tuple[PhysicalArtifactLocator, ...]


class PhysicalProjectionSource(DefinitionModel):
    """The source-revision expectation used to validate one adapter result."""

    schema_version: Literal["physical-projection-source:v1"] = (
        "physical-projection-source:v1"
    )
    source_object_id: UUID
    source_revision_id: UUID
    revision_digest: str = Field(pattern=_DIGEST)
    revision_number: int = Field(ge=1)
    status: Literal["current", "stale", "tombstoned"] = "current"

    @model_validator(mode="after")
    def validate_identity(self) -> "PhysicalProjectionSource":
        _require_id(self.source_object_id, "source_object_id")
        _require_id(self.source_revision_id, "source_revision_id")
        return self


class PhysicalArtifact(DefinitionModel):
    """Provider-neutral metadata observed for one physical artifact.

    It intentionally has no text, canonical name, or provider payload.  A
    caller may safely persist or log this model as an adapter diagnostic input.
    """

    schema_version: Literal["physical-projection-artifact:v1"] = (
        "physical-projection-artifact:v1"
    )
    artifact_kind: ArtifactKind
    artifact_id: str = Field(min_length=1, max_length=128, pattern=_ARTIFACT_IDENTIFIER)
    data_id: UUID
    workspace_id: UUID
    environment_id: UUID
    namespace_digest: str = Field(pattern=_DIGEST)
    source_object_id: UUID
    source_revision_id: UUID
    revision_digest: str = Field(pattern=_DIGEST)
    revision_number: int = Field(ge=1)
    projection_config_id: UUID
    projection_config_version: str = Field(min_length=1, max_length=128)
    projection_config_digest: str = Field(pattern=_DIGEST)
    locators: tuple[PhysicalArtifactLocator, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_identity(self) -> "PhysicalArtifact":
        for field_name in (
            "data_id",
            "workspace_id",
            "environment_id",
            "source_object_id",
            "source_revision_id",
            "projection_config_id",
        ):
            _require_id(getattr(self, field_name), field_name)
        if len({locator.digest for locator in self.locators}) != len(self.locators):
            raise ValueError("physical artifact locators must be unique")
        return self

    @classmethod
    def from_chunk_mapping(cls, mapping: _ChunkMapping) -> "PhysicalArtifact":
        """Convert a chunk adapter mapping without reading its payload."""

        locator = mapping.locator
        return cls(
            artifact_kind=ArtifactKind.CHUNK,
            artifact_id=str(mapping.segment_id),
            data_id=mapping.data_id,
            workspace_id=mapping.workspace_id,
            environment_id=mapping.environment_id,
            namespace_digest=mapping.namespace_digest,
            source_object_id=mapping.source_object_id,
            source_revision_id=mapping.source_revision_id,
            revision_digest=mapping.revision_digest,
            revision_number=mapping.revision_number,
            projection_config_id=mapping.projection_config_id,
            projection_config_version=mapping.projection_config_version,
            projection_config_digest=mapping.projection_config_digest,
            locators=(
                PhysicalArtifactLocator.from_payload(
                    kind=getattr(locator, "kind", "locator"),
                    payload=locator,
                ),
            ),
        )

    @classmethod
    def from_graph_mapping(cls, mapping: _GraphMapping) -> "PhysicalArtifact":
        """Convert an entity/relation adapter mapping without graph content."""

        return cls(
            artifact_kind=ArtifactKind(mapping.artifact_kind),
            artifact_id=mapping.artifact_id,
            data_id=mapping.data_id,
            workspace_id=mapping.workspace_id,
            environment_id=mapping.environment_id,
            namespace_digest=mapping.namespace_digest,
            source_object_id=mapping.source_object_id,
            source_revision_id=mapping.source_revision_id,
            revision_digest=mapping.revision_digest,
            revision_number=mapping.revision_number,
            projection_config_id=mapping.projection_config_id,
            projection_config_version=mapping.projection_config_version,
            projection_config_digest=mapping.projection_config_digest,
            locators=tuple(mapping.locators),
        )


class PhysicalProjectionExpectation(DefinitionModel):
    """Expected physical state for one independently validated projection."""

    schema_version: Literal["physical-projection-expectation:v1"] = (
        "physical-projection-expectation:v1"
    )
    workspace_id: UUID
    environment_id: UUID
    namespace_digest: str = Field(pattern=_DIGEST)
    projection_config_id: UUID
    projection_config_version: str = Field(min_length=1, max_length=128)
    projection_config_digest: str = Field(pattern=_DIGEST)
    sources: tuple[PhysicalProjectionSource, ...] = ()
    expected_artifacts: tuple[PhysicalArtifact, ...] = ()
    allows_empty_source_set: bool = False

    @model_validator(mode="after")
    def validate_expectation(self) -> "PhysicalProjectionExpectation":
        _require_id(self.workspace_id, "workspace_id")
        _require_id(self.environment_id, "environment_id")
        _require_id(self.projection_config_id, "projection_config_id")
        if not self.sources and not self.allows_empty_source_set:
            raise ValueError("physical projection requires at least one source")
        if self.sources and self.allows_empty_source_set:
            raise ValueError("empty-source allowance requires an empty source set")
        if self.allows_empty_source_set and self.expected_artifacts:
            raise ValueError("empty-source projection cannot expect physical artifacts")
        source_keys = [source.source_object_id for source in self.sources]
        if len(source_keys) != len(set(source_keys)):
            raise ValueError("physical projection sources must be unique")
        artifact_keys = [
            (artifact.artifact_kind, artifact.artifact_id)
            for artifact in self.expected_artifacts
        ]
        if len(artifact_keys) != len(set(artifact_keys)):
            raise ValueError("expected physical artifacts must be unique")
        source_objects = set(source_keys)
        for artifact in self.expected_artifacts:
            if artifact.source_object_id not in source_objects:
                raise ValueError("expected artifact source is not declared")
            if (
                artifact.workspace_id != self.workspace_id
                or artifact.environment_id != self.environment_id
                or artifact.namespace_digest != self.namespace_digest
                or artifact.projection_config_id != self.projection_config_id
                or artifact.projection_config_version != self.projection_config_version
                or artifact.projection_config_digest != self.projection_config_digest
            ):
                raise ValueError("expected artifact does not match projection scope")
        return self


class PhysicalArtifactCount(DefinitionModel):
    """Typed count supplied to the Issue #7 evidence boundary."""

    schema_version: Literal["physical-artifact-count:v1"] = "physical-artifact-count:v1"
    artifact_kind: ArtifactKind
    value: int = Field(ge=0)


class PhysicalDiagnostic(DefinitionModel):
    """Safe diagnostic containing only stable identifiers and a typed code."""

    schema_version: Literal["physical-projection-diagnostic:v1"] = (
        "physical-projection-diagnostic:v1"
    )
    code: PhysicalValidationCode
    artifact_kind: ArtifactKind | None = None
    artifact_id: str | None = None
    source_object_id: UUID | None = None
    source_revision_id: UUID | None = None

    @property
    def protected_content(self) -> None:
        """Make the disclosure guarantee directly assertable by callers."""

        return None


class PhysicalRepairCandidate(DefinitionModel):
    """Safe, non-executing repair input for the owning workflow."""

    schema_version: Literal["physical-projection-repair-candidate:v1"] = (
        "physical-projection-repair-candidate:v1"
    )
    action: Literal["rebuild_artifact", "remove_artifact", "rebuild_source"]
    artifact_kind: ArtifactKind | None = None
    artifact_id: str | None = None
    source_object_id: UUID | None = None
    source_revision_id: UUID | None = None
    reason: PhysicalValidationCode


class PhysicalValidationResult(DefinitionModel):
    """Deterministic adapter validation output for Issue #7 integration."""

    schema_version: Literal["physical-projection-validation-result:v1"] = (
        "physical-projection-validation-result:v1"
    )
    passed: bool
    input_digest: str = Field(pattern=_DIGEST)
    counts: tuple[PhysicalArtifactCount, ...]
    diagnostics: tuple[PhysicalDiagnostic, ...] = ()
    repair_candidates: tuple[PhysicalRepairCandidate, ...] = ()


_DIAGNOSTIC_ORDER = {
    PhysicalValidationCode.DUPLICATE_ARTIFACT: 10,
    PhysicalValidationCode.CROSS_SCOPE: 20,
    PhysicalValidationCode.NAMESPACE_MISMATCH: 30,
    PhysicalValidationCode.CONFIGURATION_MISMATCH: 40,
    PhysicalValidationCode.SOURCE_REVISION_MISMATCH: 50,
    PhysicalValidationCode.ARTIFACT_IDENTITY_MISMATCH: 55,
    PhysicalValidationCode.STALE_SOURCE: 60,
    PhysicalValidationCode.TOMBSTONED_SOURCE: 70,
    PhysicalValidationCode.LOCATOR_MISMATCH: 80,
    PhysicalValidationCode.DUPLICATE_DATA_ID: 85,
    PhysicalValidationCode.UNKNOWN_ARTIFACT: 90,
    PhysicalValidationCode.MISSING_ARTIFACT: 100,
    PhysicalValidationCode.INCOMPLETE_ARTIFACT_SET: 110,
}


def validate_physical_projection(
    expectation: PhysicalProjectionExpectation,
    actual_artifacts: tuple[PhysicalArtifact, ...],
) -> PhysicalValidationResult:
    """Validate identity, provenance, scope and completeness of physical state."""

    expected_by_key = {
        (artifact.artifact_kind, artifact.artifact_id): artifact
        for artifact in expectation.expected_artifacts
    }
    sources_by_object = {source.source_object_id: source for source in expectation.sources}
    actual_by_key: dict[tuple[ArtifactKind, str], list[PhysicalArtifact]] = defaultdict(list)
    diagnostics: list[PhysicalDiagnostic] = []
    repairs: list[PhysicalRepairCandidate] = []

    def add(
        code: PhysicalValidationCode,
        artifact: PhysicalArtifact | None = None,
        *,
        expected: PhysicalArtifact | None = None,
        source: PhysicalProjectionSource | None = None,
        action: Literal["rebuild_artifact", "remove_artifact", "rebuild_source"] | None = None,
    ) -> None:
        selected = artifact or expected
        diagnostic = PhysicalDiagnostic(
            code=code,
            artifact_kind=selected.artifact_kind if selected else None,
            artifact_id=selected.artifact_id if selected else None,
            source_object_id=(
                artifact.source_object_id
                if artifact is not None
                else expected.source_object_id
                if expected is not None
                else source.source_object_id
                if source is not None
                else None
            ),
            source_revision_id=(
                artifact.source_revision_id
                if artifact is not None
                else expected.source_revision_id
                if expected is not None
                else source.source_revision_id
                if source is not None
                else None
            ),
        )
        if diagnostic not in diagnostics:
            diagnostics.append(diagnostic)
        if action is not None:
            candidate = PhysicalRepairCandidate(
                action=action,
                artifact_kind=selected.artifact_kind if selected else None,
                artifact_id=selected.artifact_id if selected else None,
                source_object_id=(
                    artifact.source_object_id
                    if artifact is not None
                    else expected.source_object_id
                    if expected is not None
                    else source.source_object_id
                    if source is not None
                    else None
                ),
                source_revision_id=(
                    artifact.source_revision_id
                    if artifact is not None
                    else expected.source_revision_id
                    if expected is not None
                    else source.source_revision_id
                    if source is not None
                    else None
                ),
                reason=code,
            )
            if not any(
                existing.action == candidate.action
                and existing.artifact_kind == candidate.artifact_kind
                and existing.artifact_id == candidate.artifact_id
                and existing.source_object_id == candidate.source_object_id
                and existing.source_revision_id == candidate.source_revision_id
                for existing in repairs
            ):
                repairs.append(candidate)

    for expected in expectation.expected_artifacts:
        source = sources_by_object[expected.source_object_id]
        if (
            expected.source_revision_id != source.source_revision_id
            or expected.revision_digest != source.revision_digest
            or expected.revision_number != source.revision_number
        ):
            add(
                PhysicalValidationCode.SOURCE_REVISION_MISMATCH,
                expected=expected,
                action="rebuild_artifact",
            )

    for artifact in sorted(actual_artifacts, key=_artifact_sort_key):
        key = (artifact.artifact_kind, artifact.artifact_id)
        actual_by_key[key].append(artifact)
    for key, artifacts in sorted(actual_by_key.items(), key=lambda item: _key_sort(item[0])):
        if len(artifacts) > 1:
            add(PhysicalValidationCode.DUPLICATE_ARTIFACT, artifacts[0], action="rebuild_artifact")
        artifact = artifacts[0]
        expected = expected_by_key.get(key)
        if expected is None:
            source = sources_by_object.get(artifact.source_object_id)
            if source is not None and source.status == "tombstoned":
                add(PhysicalValidationCode.TOMBSTONED_SOURCE, artifact, action="remove_artifact")
            else:
                add(PhysicalValidationCode.UNKNOWN_ARTIFACT, artifact, action="remove_artifact")
            continue
        _validate_artifact(
            artifact,
            expected,
            expectation,
            sources_by_object,
            add,
        )

    data_id_keys: dict[UUID, list[tuple[ArtifactKind, str]]] = defaultdict(list)
    for key, artifacts in actual_by_key.items():
        if key in expected_by_key:
            data_id_keys[artifacts[0].data_id].append(key)
    for data_id, keys in sorted(data_id_keys.items(), key=lambda item: item[0].int):
        if len(keys) > 1:
            for key in sorted(keys, key=_key_sort):
                add(
                    PhysicalValidationCode.DUPLICATE_DATA_ID,
                    artifact=actual_by_key[key][0],
                    action="rebuild_artifact",
                )

    for key, expected in sorted(expected_by_key.items(), key=lambda item: _key_sort(item[0])):
        if key not in actual_by_key:
            add(PhysicalValidationCode.MISSING_ARTIFACT, expected=expected, action="rebuild_artifact")

    expected_counts = Counter(
        (artifact.source_object_id, artifact.artifact_kind)
        for artifact in expectation.expected_artifacts
    )
    actual_counts = Counter(
        (artifact.source_object_id, artifact.artifact_kind)
        for key, artifacts in actual_by_key.items()
        if key in expected_by_key
        for artifact in artifacts[:1]
    )
    for source_kind, expected_count in sorted(
        expected_counts.items(), key=lambda item: (item[0][0].int, item[0][1].value)
    ):
        if actual_counts[source_kind] != expected_count:
            source = sources_by_object[source_kind[0]]
            add(
                PhysicalValidationCode.INCOMPLETE_ARTIFACT_SET,
                source=source,
                action="rebuild_source",
            )

    diagnostics.sort(key=_diagnostic_sort_key)
    repairs.sort(key=_repair_sort_key)
    input_payload = {
        "expectation": expectation.model_dump(mode="json"),
        "actual_artifacts": [
            artifact.model_dump(mode="json")
            for artifact in sorted(actual_artifacts, key=_artifact_sort_key)
        ],
    }
    input_digest = sha256(
        json.dumps(input_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    count_values = {kind: 0 for kind in ArtifactKind}
    for artifact in actual_artifacts:
        count_values[artifact.artifact_kind] += 1
    return PhysicalValidationResult(
        passed=not diagnostics,
        input_digest=input_digest,
        counts=tuple(
            PhysicalArtifactCount(artifact_kind=kind, value=count_values[kind])
            for kind in ArtifactKind
        ),
        diagnostics=tuple(diagnostics),
        repair_candidates=tuple(repairs),
    )


def _validate_artifact(
    artifact: PhysicalArtifact,
    expected: PhysicalArtifact,
    expectation: PhysicalProjectionExpectation,
    sources_by_object: dict[UUID, PhysicalProjectionSource],
    add,
) -> None:
    if (
        artifact.workspace_id != expectation.workspace_id
        or artifact.environment_id != expectation.environment_id
    ):
        add(PhysicalValidationCode.CROSS_SCOPE, artifact, action="remove_artifact")
    if artifact.namespace_digest != expectation.namespace_digest:
        add(PhysicalValidationCode.NAMESPACE_MISMATCH, artifact, action="remove_artifact")
    if (
        artifact.projection_config_id != expectation.projection_config_id
        or artifact.projection_config_version != expectation.projection_config_version
        or artifact.projection_config_digest != expectation.projection_config_digest
    ):
        add(PhysicalValidationCode.CONFIGURATION_MISMATCH, artifact, action="rebuild_artifact")
    if artifact.data_id != expected.data_id:
        add(PhysicalValidationCode.ARTIFACT_IDENTITY_MISMATCH, artifact, action="rebuild_artifact")
    if (
        artifact.source_object_id != expected.source_object_id
        or artifact.source_revision_id != expected.source_revision_id
        or artifact.revision_digest != expected.revision_digest
        or artifact.revision_number != expected.revision_number
    ):
        add(PhysicalValidationCode.SOURCE_REVISION_MISMATCH, artifact, action="rebuild_artifact")
    source = sources_by_object.get(artifact.source_object_id)
    if source is None:
        add(PhysicalValidationCode.SOURCE_REVISION_MISMATCH, artifact, action="rebuild_artifact")
    else:
        if (
            artifact.source_revision_id != source.source_revision_id
            or artifact.revision_digest != source.revision_digest
            or artifact.revision_number != source.revision_number
        ):
            add(PhysicalValidationCode.SOURCE_REVISION_MISMATCH, artifact, action="rebuild_artifact")
        if source.status == "tombstoned":
            add(PhysicalValidationCode.TOMBSTONED_SOURCE, artifact, action="remove_artifact")
        elif source.status == "stale" or artifact.revision_number < source.revision_number:
            add(PhysicalValidationCode.STALE_SOURCE, artifact, action="rebuild_artifact")
    if _locator_sort_key(artifact.locators) != _locator_sort_key(expected.locators):
        add(PhysicalValidationCode.LOCATOR_MISMATCH, artifact, action="rebuild_artifact")


def _artifact_sort_key(artifact: PhysicalArtifact) -> tuple[str, str, int]:
    return artifact.artifact_kind.value, artifact.artifact_id, artifact.data_id.int


def _key_sort(key: tuple[ArtifactKind, str]) -> tuple[str, str]:
    return key[0].value, key[1]


def _locator_sort_key(
    locators: tuple[PhysicalArtifactLocator, ...],
) -> tuple[tuple[str, str, str], ...]:
    return tuple(sorted((locator.kind, locator.reference, locator.digest) for locator in locators))


def _diagnostic_sort_key(diagnostic: PhysicalDiagnostic) -> tuple[int, str, str, str]:
    return (
        _DIAGNOSTIC_ORDER[diagnostic.code],
        diagnostic.artifact_kind.value if diagnostic.artifact_kind else "",
        diagnostic.artifact_id or "",
        str(diagnostic.source_revision_id or ""),
    )


def _repair_sort_key(candidate: PhysicalRepairCandidate) -> tuple[str, str, str]:
    return (
        candidate.action,
        candidate.artifact_kind.value if candidate.artifact_kind else "",
        candidate.artifact_id or str(candidate.source_revision_id or ""),
    )


__all__ = [
    "ArtifactKind",
    "PhysicalArtifact",
    "PhysicalArtifactLocator",
    "PhysicalArtifactCount",
    "PhysicalDiagnostic",
    "PhysicalProjectionExpectation",
    "PhysicalProjectionSource",
    "PhysicalRepairCandidate",
    "PhysicalValidationCode",
    "PhysicalValidationResult",
    "validate_physical_projection",
]
