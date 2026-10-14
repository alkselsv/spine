"""Cognee retrieval adapter implementing the ContextProjection port."""

from .adapter import (
    CogneePublicStructuredRetrievalSdk,
    CogneeStructuredRetrieval,
    CogneeStructuredRetrievalSdk,
)
from spine.memory.cognee_memory import ask, format_answers, remember_texts
from spine.memory.contracts import ContextProjection
from spine.memory.fake import DeterministicContextProjection, DeterministicPhysicalStore
from spine.memory.namespace import (
    NamespaceLifecycleError,
    NamespaceRoutingError,
    ProjectionNamespaceContext,
    ProjectionNamespaceRegistry,
    ProjectionNamespaceRouter,
    namespace_identity_digest,
)

__all__ = [
    "CogneePublicStructuredRetrievalSdk",
    "CogneeStructuredRetrieval",
    "CogneeStructuredRetrievalSdk",
    "ContextProjection",
    "DeterministicContextProjection",
    "DeterministicPhysicalStore",
    "NamespaceLifecycleError",
    "NamespaceRoutingError",
    "ProjectionNamespaceContext",
    "ProjectionNamespaceRegistry",
    "ProjectionNamespaceRouter",
    "ask",
    "format_answers",
    "namespace_identity_digest",
    "remember_texts",
]
