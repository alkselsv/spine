"""Pinned canonical identities for source revisions and observations."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .models import (
    ALLOWED_TOMBSTONE_REASONS,
    RevisionKind,
    RevisionMetadata,
    SourceRevision,
    canonical_revision_metadata_digest,
)
from .errors import RevisionDigestMismatchError
from .profile import (
    BCP47_TABLE_DIGEST,
    REVISION_METADATA_SCHEMA,
    REVISION_PROFILE,
    REVISION_SCHEMA,
    UNICODE_TABLE_DIGEST,
)
def revision_canonical_bytes(revision: SourceRevision) -> bytes:
    """Return the exact revision-bearing representation, excluding provenance."""

    payload: dict[str, Any] = {
        "schema": REVISION_SCHEMA,
        "profile": revision.canonicalization_profile,
        "unicode_table": revision.unicode_table_digest,
        "bcp47_table": revision.bcp47_table_digest,
        "kind": revision.kind.value,
    }
    if revision.kind is RevisionKind.CONTENT:
        payload["content"] = {
            "original_sha256": revision.original_sha256,
            "byte_length": str(revision.byte_length),
            "media_type": revision.media_type,
            "metadata_schema": revision.revision_metadata_schema,
            "metadata_digest": revision.revision_metadata_digest,
            "metadata": _metadata_payload(revision.revision_metadata),
            "reappearance_after_tombstone_revision_id": (
                str(revision.reappearance_after_tombstone_revision_id)
                if revision.reappearance_after_tombstone_revision_id
                else None
            ),
        }
    else:
        payload["tombstone"] = {
            "deletion_fact": "deletion-fact:r1-v1",
            "reason": revision.deletion_reason,
        }
    return _canonical_json(payload)


def revision_digest(revision: SourceRevision) -> str:
    return hashlib.sha256(revision_canonical_bytes(revision)).hexdigest()


def content_revision(**fields: object) -> SourceRevision:
    """Build a content revision and derive its digest from its canonical fields."""

    if "revision_digest" in fields:
        raise TypeError("revision_digest is produced by the canonical factory")
    metadata = fields.get("revision_metadata")
    if isinstance(metadata, dict):
        metadata = RevisionMetadata.model_validate(metadata)
        fields["revision_metadata"] = metadata
    if isinstance(metadata, RevisionMetadata):
        expected_metadata_digest = canonical_revision_metadata_digest(metadata)
        supplied_metadata_digest = fields.get("revision_metadata_digest")
        if supplied_metadata_digest is not None and supplied_metadata_digest != expected_metadata_digest:
            raise RevisionDigestMismatchError("Revision metadata digest does not match metadata.")
        fields["revision_metadata_digest"] = expected_metadata_digest
    provisional = SourceRevision.model_validate({**fields, "revision_digest": "0" * 64})
    fields["revision_digest"] = revision_digest(provisional)
    return SourceRevision.model_validate(fields)


def tombstone_revision(**fields: object) -> SourceRevision:
    """Build a tombstone revision and derive its digest from its canonical fields."""

    if "revision_digest" in fields:
        raise TypeError("revision_digest is produced by the canonical factory")
    provisional = SourceRevision.model_validate({**fields, "revision_digest": "0" * 64})
    fields["revision_digest"] = revision_digest(provisional)
    return SourceRevision.model_validate(fields)


def assert_revision_digest(revision: SourceRevision) -> None:
    """Reject caller-supplied digests that do not match revision-bearing data."""

    if revision.revision_digest != revision_digest(revision):
        raise RevisionDigestMismatchError("Revision digest does not match revision data.")


def _metadata_payload(metadata: RevisionMetadata | None) -> dict[str, str | None]:
    if metadata is None:
        return {"document_language": None, "embedded_title": None}
    return {
        "document_language": metadata.document_language,
        "embedded_title": metadata.embedded_title,
    }


def _canonical_json(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


__all__ = [
    "ALLOWED_TOMBSTONE_REASONS",
    "BCP47_TABLE_DIGEST",
    "REVISION_PROFILE",
    "REVISION_METADATA_SCHEMA",
    "REVISION_SCHEMA",
    "UNICODE_TABLE_DIGEST",
    "assert_revision_digest",
    "content_revision",
    "revision_canonical_bytes",
    "revision_digest",
    "tombstone_revision",
]
