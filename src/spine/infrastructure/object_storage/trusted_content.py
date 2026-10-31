"""Object-storage adapter for exact trusted-content verification reads."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import datetime, timedelta, timezone

from spine.application.storage.trusted_content import TrustedContentVerificationReader
from spine.domain.sources import (
    SourceRevision,
    SourceRevisionReceiptAssociation,
    VerificationCleanupOutcome,
    assert_exact_revision_receipt_binding,
)

from .contracts import AuthorizedReadGrantRegistry
from .port import OriginalObjectStore


_VERIFICATION_PURPOSE = "trusted-content.verification.v1"
_VERIFICATION_AUTHORITY = "trusted-content-verifier:v1"
_VERIFICATION_LIFECYCLE = "finalized-association:v1"


class ExactAssociationContentReader(TrustedContentVerificationReader):
    """Issue a one-shot read lease bound to one finalized association."""

    def __init__(
        self,
        *,
        object_store: OriginalObjectStore,
        grant_registry: AuthorizedReadGrantRegistry,
        clock: Callable[[], datetime] | None = None,
        grant_lifetime: timedelta = timedelta(minutes=5),
    ) -> None:
        if grant_lifetime <= timedelta(0):
            raise ValueError("grant_lifetime must be positive")
        self._object_store = object_store
        self._grant_registry = grant_registry
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._grant_lifetime = grant_lifetime

    def read_exact(
        self,
        *,
        revision: SourceRevision,
        association: SourceRevisionReceiptAssociation,
        max_bytes: int,
    ) -> AsyncIterator[bytes]:
        assert_exact_revision_receipt_binding(
            revision=revision, association=association
        )
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("verification clock must be timezone-aware")
        reference = association.original_reference
        grant = self._grant_registry.issue(
            workspace_id=association.workspace_id,
            environment_id=association.environment_id,
            source_revision_id=association.revision_id,
            object_reference=reference,
            purpose=_VERIFICATION_PURPOSE,
            authorization_decision_version=_VERIFICATION_AUTHORITY,
            lifecycle_version=_VERIFICATION_LIFECYCLE,
            expires_at=now + self._grant_lifetime,
        )
        lease = self._grant_registry.consume(
            grant,
            workspace_id=association.workspace_id,
            environment_id=association.environment_id,
            source_revision_id=association.revision_id,
            object_reference=reference,
            purpose=_VERIFICATION_PURPOSE,
            authorization_decision_version=_VERIFICATION_AUTHORITY,
            lifecycle_version=_VERIFICATION_LIFECYCLE,
            now=now,
        )
        return self._object_store.open_bounded_read(lease, max_bytes=max_bytes)

    async def cleanup(
        self,
        *,
        revision: SourceRevision,
        association: SourceRevisionReceiptAssociation,
    ) -> VerificationCleanupOutcome:
        try:
            assert_exact_revision_receipt_binding(
                revision=revision, association=association
            )
        except ValueError:
            return VerificationCleanupOutcome.UNCERTAIN
        return VerificationCleanupOutcome.NOT_REQUIRED


__all__ = ["ExactAssociationContentReader"]
