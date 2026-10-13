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
from .indexing import (
    ChunkProjectionMapping,
    CogneeChunkProjectionStore,
    CogneePublicSdk,
    CogneeSdk,
    CogneeVersionError,
    SUPPORTED_COGNEE_VERSION,
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
    "ChunkProjectionMapping",
    "CogneeChunkProjectionStore",
    "CogneePublicSdk",
    "CogneeSdk",
    "CogneeVersionError",
    "SUPPORTED_COGNEE_VERSION",
]
