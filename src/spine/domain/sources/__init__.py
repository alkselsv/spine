"""Immutable source identity and observation contracts."""

from .models import (
    IdentityMode,
    RevisionKind,
    RevisionMetadata,
    SourceObject,
    SourceRevision,
    SourceRevisionProvenance,
    SourceKind,
)
from .errors import RevisionDigestMismatchError, SourceInvariantError
from .canonicalization import (
    OBSERVATION_SCHEMA,
    REVISION_PROFILE,
    REVISION_SCHEMA,
    SourceObservationCommand,
    assert_revision_digest,
    content_revision,
    observation_command_digest,
    revision_canonical_bytes,
    revision_digest,
    tombstone_revision,
)
from .profile import BCP47_TABLE_DIGEST, REVISION_METADATA_SCHEMA, UNICODE_TABLE_DIGEST

__all__ = [
    "IdentityMode",
    "RevisionDigestMismatchError",
    "RevisionKind",
    "RevisionMetadata",
    "SourceKind",
    "SourceObject",
    "SourceRevision",
    "SourceRevisionProvenance",
    "SourceInvariantError",
    "OBSERVATION_SCHEMA",
    "BCP47_TABLE_DIGEST",
    "REVISION_PROFILE",
    "REVISION_METADATA_SCHEMA",
    "REVISION_SCHEMA",
    "SourceObservationCommand",
    "assert_revision_digest",
    "content_revision",
    "observation_command_digest",
    "revision_canonical_bytes",
    "revision_digest",
    "tombstone_revision",
    "UNICODE_TABLE_DIGEST",
]
