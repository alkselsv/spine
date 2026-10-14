"""Compatibility exports for the provider-neutral physical validation contract."""

from spine.memory.validation import (
    ArtifactKind,
    PhysicalArtifact,
    PhysicalArtifactCount,
    PhysicalArtifactLocator,
    PhysicalDiagnostic,
    PhysicalProjectionExpectation,
    PhysicalProjectionSource,
    PhysicalRepairCandidate,
    PhysicalValidationCode,
    PhysicalValidationResult,
    validate_physical_projection,
)

__all__ = [
    "ArtifactKind",
    "PhysicalArtifact",
    "PhysicalArtifactCount",
    "PhysicalArtifactLocator",
    "PhysicalDiagnostic",
    "PhysicalProjectionExpectation",
    "PhysicalProjectionSource",
    "PhysicalRepairCandidate",
    "PhysicalValidationCode",
    "PhysicalValidationResult",
    "validate_physical_projection",
]
