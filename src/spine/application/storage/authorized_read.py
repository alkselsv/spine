"""Fail-closed gateway for authorized reads of original source bytes.

The object store only understands an opaque, one-shot lease.  This module is
the application seam that turns a trusted source-revision request into that
lease after resolving canonical source evidence and rechecking authorization.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Protocol
from uuid import UUID

from pydantic import model_validator

from spine.domain.common import DefinitionModel, VersionIdentifier
from spine.domain.sources import (
    AdmissionDisposition,
    SourceRevision,
    SourceRevisionReceiptAssociation,
)
from spine.infrastructure.object_storage.contracts import (
    AuthorizedReadGrantRegistry,
)
from spine.infrastructure.object_storage.errors import InvalidReadGrant
from spine.infrastructure.object_storage.port import OriginalObjectStore


_PURPOSE = re.compile(r"^[a-z][a-z0-9_.:-]{0,63}$")


class OriginalReadLifecycle(str, Enum):
    """Canonical lifecycle state relevant to disclosure of one revision."""

    CURRENT = "current"
    SUPERSEDED = "superseded"
    TOMBSTONED = "tombstoned"
    QUARANTINED = "quarantined"
    REJECTED = "rejected"


class OriginalReadRetention(str, Enum):
    """Retention authority's answer for the exact physical evidence."""

    AVAILABLE = "available"
    EXPIRED = "expired"
    DELETED = "deleted"
    UNKNOWN = "unknown"


class OriginalReadDenied(PermissionError):
    """Disclosure-safe failure for every unavailable or unauthorized read."""

    def __init__(self) -> None:
        super().__init__("original content is unavailable")


class OriginalReadTarget(DefinitionModel):
    """One resolver snapshot used to authorize an original-content read."""

    workspace_id: UUID
    environment_id: UUID
    source_object_id: UUID
    source_revision_id: UUID
    revision: SourceRevision
    association: SourceRevisionReceiptAssociation
    current_revision_id: UUID | None
    lifecycle: OriginalReadLifecycle
    lifecycle_version: VersionIdentifier
    disposition: AdmissionDisposition
    retention: OriginalReadRetention

    @model_validator(mode="after")
    def validate_consistency(self) -> "OriginalReadTarget":
        for name in (
            "workspace_id",
            "environment_id",
            "source_object_id",
            "source_revision_id",
        ):
            if getattr(self, name).int == 0:
                raise ValueError(f"{name} must be non-zero")
        if self.current_revision_id is not None and self.current_revision_id.int == 0:
            raise ValueError("current_revision_id must be non-zero")
        if (
            self.revision.revision_id != self.source_revision_id
            or self.revision.source_object_id != self.source_object_id
            or self.revision.workspace_id != self.workspace_id
            or self.revision.environment_id != self.environment_id
        ):
            raise ValueError("revision scope does not match read target")
        association = self.association
        if (
            association.source_object_id != self.source_object_id
            or association.revision_id != self.source_revision_id
            or association.workspace_id != self.workspace_id
            or association.environment_id != self.environment_id
        ):
            raise ValueError("receipt association scope does not match read target")
        if self.revision.original_reference != association.original_reference:
            raise ValueError("revision and receipt references do not match")
        return self


class OriginalReadDecision(DefinitionModel):
    """Minimal current decision that may bind an opaque read grant."""

    allowed: bool
    policy_version: VersionIdentifier
    lifecycle_version: VersionIdentifier
    expires_at: datetime

    @model_validator(mode="after")
    def validate_decision(self) -> "OriginalReadDecision":
        if self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None:
            raise ValueError("expires_at must be timezone-aware")
        return self


class OriginalReadTargetResolver(Protocol):
    """Resolve a complete, tenant-scoped canonical read snapshot."""

    async def resolve_original_read_target(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        source_revision_id: UUID,
    ) -> OriginalReadTarget | None: ...


class OriginalReadAuthorizer(Protocol):
    """Evaluate current Access Policy and trusted purpose authority."""

    async def decide_original_read(
        self,
        *,
        target: OriginalReadTarget,
        purpose: str,
        now: datetime,
    ) -> OriginalReadDecision: ...


class AuthorizedOriginalReadGateway:
    """Resolve, revalidate, and stream one bounded original read."""

    def __init__(
        self,
        *,
        resolver: OriginalReadTargetResolver,
        authorizer: OriginalReadAuthorizer,
        object_store: OriginalObjectStore,
        grant_registry: AuthorizedReadGrantRegistry,
        clock: Callable[[], datetime] | None = None,
        max_grant_lifetime: timedelta = timedelta(minutes=5),
    ) -> None:
        if max_grant_lifetime <= timedelta(0):
            raise ValueError("max_grant_lifetime must be positive")
        self._resolver = resolver
        self._authorizer = authorizer
        self._object_store = object_store
        self._grant_registry = grant_registry
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._max_grant_lifetime = max_grant_lifetime

    def read_original(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        source_revision_id: UUID,
        purpose: str,
        max_bytes: int,
    ) -> AsyncIterator[bytes]:
        """Return a stream whose first byte is gated by current authority.

        ``ObjectReference`` is deliberately absent from this public method. A
        reference possessed by a caller is never sufficient to disclose bytes.
        """

        self._validate_request(
            workspace_id=workspace_id,
            environment_id=environment_id,
            source_revision_id=source_revision_id,
            purpose=purpose,
            max_bytes=max_bytes,
        )
        return self._read(
            workspace_id=workspace_id,
            environment_id=environment_id,
            source_revision_id=source_revision_id,
            purpose=purpose,
            max_bytes=max_bytes,
        )

    async def _read(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        source_revision_id: UUID,
        purpose: str,
        max_bytes: int,
    ) -> AsyncIterator[bytes]:
        try:
            initial = await self._resolve(
                workspace_id=workspace_id,
                environment_id=environment_id,
                source_revision_id=source_revision_id,
            )
            self._assert_disclosable(initial)
            first_now = self._now()
            first_decision = await self._authorizer.decide_original_read(
                target=initial,
                purpose=purpose,
                now=first_now,
            )
            self._assert_decision(initial, first_decision, first_now)

            # This second resolution is intentionally immediately before grant
            # issuance. It closes the policy/tombstone race without treating an
            # earlier positive decision as durable authority.
            current = await self._resolve(
                workspace_id=workspace_id,
                environment_id=environment_id,
                source_revision_id=source_revision_id,
            )
            self._assert_disclosable(current)
            if current != initial:
                raise OriginalReadDenied()
            second_now = self._now()
            second_decision = await self._authorizer.decide_original_read(
                target=current,
                purpose=purpose,
                now=second_now,
            )
            self._assert_decision(current, second_decision, second_now)
            if first_decision.policy_version != second_decision.policy_version:
                raise OriginalReadDenied()
            if second_decision.expires_at > second_now + self._max_grant_lifetime:
                raise OriginalReadDenied()

            reference = current.association.original_reference
            grant = self._grant_registry.issue(
                workspace_id=workspace_id,
                environment_id=environment_id,
                source_revision_id=source_revision_id,
                object_reference=reference,
                purpose=purpose,
                authorization_decision_version=second_decision.policy_version,
                lifecycle_version=current.lifecycle_version,
                expires_at=second_decision.expires_at,
            )
            lease = self._grant_registry.consume(
                grant,
                workspace_id=workspace_id,
                environment_id=environment_id,
                source_revision_id=source_revision_id,
                object_reference=reference,
                purpose=purpose,
                authorization_decision_version=second_decision.policy_version,
                lifecycle_version=current.lifecycle_version,
                now=second_now,
            )
            stream = self._object_store.open_bounded_read(lease, max_bytes=max_bytes)
        except OriginalReadDenied:
            raise
        except (InvalidReadGrant, ValueError, TypeError, AttributeError) as error:
            raise OriginalReadDenied() from error
        except Exception as error:
            # Authorization/resolution failures must not disclose existence or
            # provider details. Cancellation is not an Exception and passes
            # through to preserve normal cooperative cancellation semantics.
            raise OriginalReadDenied() from error

        observed = 0
        async for chunk in stream:
            if not isinstance(chunk, bytes):
                raise OriginalReadDenied()
            observed += len(chunk)
            if observed > max_bytes:
                raise OriginalReadDenied()
            yield chunk

    async def _resolve(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        source_revision_id: UUID,
    ) -> OriginalReadTarget:
        target = await self._resolver.resolve_original_read_target(
            workspace_id=workspace_id,
            environment_id=environment_id,
            source_revision_id=source_revision_id,
        )
        if not isinstance(target, OriginalReadTarget):
            raise OriginalReadDenied()
        return target

    @staticmethod
    def _assert_disclosable(target: OriginalReadTarget) -> None:
        reference = target.revision.original_reference
        if (
            target.lifecycle is not OriginalReadLifecycle.CURRENT
            or target.current_revision_id != target.source_revision_id
            or target.revision.kind.value != "content"
            or target.disposition is not AdmissionDisposition.ADMITTED
            or target.retention is not OriginalReadRetention.AVAILABLE
            or reference is None
            or reference != target.association.original_reference
        ):
            raise OriginalReadDenied()

    @staticmethod
    def _assert_decision(
        target: OriginalReadTarget,
        decision: OriginalReadDecision,
        now: datetime,
    ) -> None:
        if (
            not decision.allowed
            or decision.lifecycle_version != target.lifecycle_version
            or decision.expires_at <= now
        ):
            raise OriginalReadDenied()

    def _now(self) -> datetime:
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise OriginalReadDenied()
        return now

    @staticmethod
    def _validate_request(
        *,
        workspace_id: UUID,
        environment_id: UUID,
        source_revision_id: UUID,
        purpose: str,
        max_bytes: int,
    ) -> None:
        if (
            not isinstance(workspace_id, UUID)
            or workspace_id.int == 0
            or not isinstance(environment_id, UUID)
            or environment_id.int == 0
            or not isinstance(source_revision_id, UUID)
            or source_revision_id.int == 0
        ):
            raise ValueError("read scope identities must be non-zero UUIDs")
        if not isinstance(purpose, str) or _PURPOSE.fullmatch(purpose) is None:
            raise ValueError("read purpose must be a bounded identifier")
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
            raise ValueError("max_bytes must be positive")


__all__ = [
    "AdmissionDisposition",
    "AuthorizedOriginalReadGateway",
    "OriginalReadAuthorizer",
    "OriginalReadDecision",
    "OriginalReadDenied",
    "OriginalReadLifecycle",
    "OriginalReadRetention",
    "OriginalReadTarget",
    "OriginalReadTargetResolver",
]
