"""Deterministic application-boundary fakes for offline contract tests."""

from .projection_result_mapping import DeterministicCanonicalProjectionGateway
from .semantic_support import DeterministicSemanticSupportValidator

__all__ = [
    "DeterministicCanonicalProjectionGateway",
    "DeterministicSemanticSupportValidator",
]
