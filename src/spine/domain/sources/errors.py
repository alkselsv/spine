"""Domain failures for immutable source identities and observations."""

from __future__ import annotations


class SourceInvariantError(ValueError):
    """A source observation violates a domain invariant."""


class RevisionDigestMismatchError(SourceInvariantError):
    """Revision-bearing fields do not match the supplied canonical digest."""


__all__ = ["RevisionDigestMismatchError", "SourceInvariantError"]
