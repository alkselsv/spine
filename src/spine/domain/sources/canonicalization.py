"""Pinned canonical identities for source revisions and observations."""

from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID

from pydantic import ConfigDict, model_validator

from spine.domain.common import DefinitionModel

from .models import RevisionKind, RevisionMetadata, SourceRevision


REVISION_PROFILE = "r1-c14n-2026-10"
REVISION_SCHEMA = "source-revision:v1"
OBSERVATION_SCHEMA = "source-observation-command:v1"


class SourceObservationCommand(DefinitionModel):
    """Command identity is intentionally separate from revision identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    command_id: UUID
    source_object_id: UUID
    revision_digest: str
    original_receipt_id: UUID | None = None
    original_receipt_digest: str | None = None
    order_scheme: str | None = None
    order_token: str | None = None
    provenance_reference: str
    producer_kind: str
    producer_reference: str
    event_identity: str

    @model_validator(mode="after")
    def validate_command(self) -> "SourceObservationCommand":
        if self.command_id.int == 0 or self.source_object_id.int == 0:
            raise ValueError("command identities must be non-zero")
        if len(self.revision_digest) != 64 or any(c not in "0123456789abcdef" for c in self.revision_digest):
            raise ValueError("revision_digest must be a SHA-256 hex value")
        if (self.original_receipt_id is None) != (self.original_receipt_digest is None):
            raise ValueError("original receipt identity and digest must be paired")
        if (self.order_scheme is None) != (self.order_token is None):
            raise ValueError("order scheme and token must be paired")
        return self

    def canonical_bytes(self) -> bytes:
        return _canonical_json(
            {
                "schema": OBSERVATION_SCHEMA,
                "command_id": str(self.command_id),
                "source_object_id": str(self.source_object_id),
                "revision_digest": self.revision_digest,
                "original_receipt_id": (
                    str(self.original_receipt_id) if self.original_receipt_id else None
                ),
                "original_receipt_digest": self.original_receipt_digest,
                "order": {
                    "scheme": self.order_scheme,
                    "token": self.order_token,
                },
                "provenance": {
                    "reference": self.provenance_reference,
                    "producer_kind": self.producer_kind,
                    "producer_reference": self.producer_reference,
                    "event_identity": self.event_identity,
                },
            }
        )

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


def revision_canonical_bytes(revision: SourceRevision) -> bytes:
    """Return the exact revision-bearing representation, excluding provenance."""

    payload: dict[str, Any] = {
        "schema": REVISION_SCHEMA,
        "profile": REVISION_PROFILE,
        "kind": revision.kind.value,
    }
    if revision.kind is RevisionKind.CONTENT:
        payload["content"] = {
            "original_sha256": revision.original_sha256,
            "byte_length": str(revision.byte_length),
            "media_type": revision.media_type,
            "metadata_schema": revision.revision_metadata_schema,
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


def observation_command_digest(command: SourceObservationCommand) -> str:
    return command.digest


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
    "OBSERVATION_SCHEMA",
    "REVISION_PROFILE",
    "REVISION_SCHEMA",
    "SourceObservationCommand",
    "observation_command_digest",
    "revision_canonical_bytes",
    "revision_digest",
]
