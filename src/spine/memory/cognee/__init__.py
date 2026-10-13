"""Cognee adapter for the provider-neutral context contracts."""

from .extraction import (
    AcceptedEntity,
    AcceptedRelation,
    EntityCandidate,
    ExtractionBatch,
    ExtractionRejection,
    ExtractionResult,
    ExtractionStatus,
    ProjectionGraphWriter,
    RelationCandidate,
    extract_projection,
)

__all__ = [
    "AcceptedEntity",
    "AcceptedRelation",
    "EntityCandidate",
    "ExtractionBatch",
    "ExtractionRejection",
    "ExtractionResult",
    "ExtractionStatus",
    "ProjectionGraphWriter",
    "RelationCandidate",
    "extract_projection",
]
