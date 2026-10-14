"""Cognee adapter for the provider-neutral context contracts."""

from .extraction import (
    AcceptedEntity,
    AcceptedRelation,
    EntityCandidate,
    ExtractionBatch,
    ExtractionRejection,
    ExtractionResult,
    ExtractionSourceRevision,
    GraphProjectionRequest,
    ExtractionStatus,
    ProjectionGraphWriter,
    RelationCandidate,
    extract_projection,
)
from .graph import (
    CogneeGraphProjectionStore,
    CogneeGraphProjectionWriter,
    GraphArtifactMapping,
    GraphProjectionResult,
    GraphProjectionStatus,
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
    "ExtractionSourceRevision",
    "GraphProjectionRequest",
    "ExtractionStatus",
    "ProjectionGraphWriter",
    "RelationCandidate",
    "extract_projection",
    "CogneeGraphProjectionStore",
    "CogneeGraphProjectionWriter",
    "GraphArtifactMapping",
    "GraphProjectionResult",
    "GraphProjectionStatus",
    "ChunkProjectionMapping",
    "CogneeChunkProjectionStore",
    "CogneePublicSdk",
    "CogneeSdk",
    "CogneeVersionError",
    "SUPPORTED_COGNEE_VERSION",
]
