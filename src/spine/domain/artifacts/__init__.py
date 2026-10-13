"""Typed outputs passed between workflow steps."""

from spine.domain.artifacts.models import (
    Artifact,
    ArtifactRef,
    ArtifactState,
    validate_artifact_lineage,
)

__all__ = ["Artifact", "ArtifactRef", "ArtifactState", "validate_artifact_lineage"]
from .models import (
    ParseCoverage,
    ParseExclusion,
    ParsedDocument,
    ParsedSegment,
    PhysicalLineLocator,
    parsed_document_digest,
    resolve_locator,
)

__all__ = [
    "ParseCoverage",
    "ParseExclusion",
    "ParsedDocument",
    "ParsedSegment",
    "PhysicalLineLocator",
    "parsed_document_digest",
    "resolve_locator",
]
