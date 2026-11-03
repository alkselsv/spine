from datetime import datetime, timezone
from uuid import UUID

from spine.domain.sources import (
    EMAIL_REVISION_METADATA_SCHEMA,
    EmailRevisionMetadata,
    RevisionKind,
    RevisionMetadata,
    SourceRevision,
)
from spine.domain.sources.canonicalization import content_revision
from spine.domain.sources.original_reference import ObjectReference


NOW = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)


def _email_revision(*, thread_reference: str) -> SourceRevision:
    metadata = RevisionMetadata(
        representation_profile="raw_email_mime:v1",
        email_revision=EmailRevisionMetadata(
            provider_kind="google",
            provider_message_id="gmail-message-835",
            internal_date=NOW,
            provider_thread_reference=thread_reference,
            rfc_message_id_claims=("<message@example.invalid>",),
            references_claims=(),
            in_reply_to_claims=(),
        ),
    )
    return content_revision(
        revision_id=UUID(int=1),
        source_object_id=UUID(int=2),
        workspace_id=UUID(int=3),
        environment_id=UUID(int=4),
        kind=RevisionKind.CONTENT,
        revision_schema_version="source-revision:v1",
        revision_metadata_schema=EMAIL_REVISION_METADATA_SCHEMA,
        revision_metadata=metadata,
        original_reference=ObjectReference(
            schema_version=1,
            object_id=UUID(int=5),
            storage_generation=UUID(int=6),
            digest_algorithm="sha256",
            digest_hex="a" * 64,
            byte_length=4,
        ),
        original_sha256="a" * 64,
        byte_length=4,
        media_type="message/rfc822",
        observed_at=NOW,
    )


def test_changed_email_metadata_changes_canonical_revision_digest() -> None:
    original = _email_revision(thread_reference="thread-before")
    changed = _email_revision(thread_reference="thread-after")

    assert changed.revision_metadata_digest != original.revision_metadata_digest
    assert changed.revision_digest != original.revision_digest
