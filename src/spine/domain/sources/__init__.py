"""Immutable source identity, observation and admission contracts."""

from .admission import (
    AdmissionDecision,
    AdmissionDisposition,
    AdmissionReference,
    ReasonCode,
)

from .models import (
    IdentityMode,
    RevisionKind,
    RevisionMetadata,
    SourceObject,
    SourceRevision,
    SourceRevisionProvenance,
    SourceRevisionReceiptAssociation,
    SourceKind,
    canonical_revision_metadata_digest,
)
from .errors import RevisionDigestMismatchError, SourceInvariantError
from .canonicalization import (
    REVISION_PROFILE,
    REVISION_SCHEMA,
    assert_revision_digest,
    content_revision,
    revision_canonical_bytes,
    revision_digest,
    tombstone_revision,
)
from .profile import BCP47_TABLE_DIGEST, REVISION_METADATA_SCHEMA, UNICODE_TABLE_DIGEST
from .profile import OBSERVATION_SCHEMA

__all__ = [
    "AdmissionDecision",
    "AdmissionDisposition",
    "AdmissionReference",
    "ReasonCode",
    "IdentityMode",
    "RevisionDigestMismatchError",
    "RevisionKind",
    "RevisionMetadata",
    "SourceKind",
    "SourceObject",
    "SourceRevision",
    "SourceRevisionProvenance",
    "SourceRevisionReceiptAssociation",
    "canonical_revision_metadata_digest",
    "SourceInvariantError",
    "OBSERVATION_SCHEMA",
    "BCP47_TABLE_DIGEST",
    "REVISION_PROFILE",
    "REVISION_METADATA_SCHEMA",
    "REVISION_SCHEMA",
    "assert_revision_digest",
    "content_revision",
    "revision_canonical_bytes",
    "revision_digest",
    "tombstone_revision",
    "UNICODE_TABLE_DIGEST",
]
