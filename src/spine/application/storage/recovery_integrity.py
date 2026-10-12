"""Application coordination for append-only recovery evidence integrity."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Protocol

from pydantic import ValidationError

from spine.domain.storage.recovery import (
    RecoveryEvidenceEventKind,
    RecoveryEvidenceInvalidation,
    RecoveryEvidenceVerification,
    RecoveryReadiness,
    RecoveryTrustState,
    StorageRecoveryManifest,
    derive_recovery_readiness,
    verify_recovery_evidence_chain,
)

from .recovery import RecoveryCoordinationError


class RecoveryEvidenceConflictError(RecoveryCoordinationError):
    """A writer used an obsolete sequence or predecessor digest."""


class RecoveryEvidenceValidationError(RecoveryCoordinationError):
    """Evidence or a replacement reference failed validation."""


class RecoveryEvidenceTransaction(Protocol):
    async def list_manifests(
        self, deployment_scope_id: str
    ) -> tuple[StorageRecoveryManifest, ...]: ...

    async def list_invalidations(
        self, deployment_scope_id: str
    ) -> tuple[RecoveryEvidenceInvalidation, ...]: ...

    async def add_invalidation(self, event: RecoveryEvidenceInvalidation) -> None: ...


class RecoveryEvidenceRepository(Protocol):
    @asynccontextmanager
    async def transaction(
        self, deployment_scope_id: str
    ) -> AsyncIterator[RecoveryEvidenceTransaction]: ...


class RecoveryEvidenceCoordinator:
    """Serialize append-only evidence and derive restore/readiness trust."""

    def __init__(
        self,
        *,
        repository: RecoveryEvidenceRepository,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._repository = repository
        self._clock = clock

    async def append_invalidation(
        self,
        deployment_scope_id: str,
        *,
        recovery_set_id: str,
        manifest_digest: str,
        reason_code: str,
        event_kind: RecoveryEvidenceEventKind = RecoveryEvidenceEventKind.INVALIDATED,
        replacement_recovery_set_id: str | None = None,
        replacement_manifest_digest: str | None = None,
        replacement_validation_reference: str | None = None,
        replacement_approval_reference: str | None = None,
        expected_sequence: int | None = None,
        expected_previous_event_digest: str | None = None,
    ) -> RecoveryEvidenceInvalidation:
        """Append one event atomically after checking the caller's expected head."""

        async with self._repository.transaction(deployment_scope_id) as transaction:
            manifests = await transaction.list_manifests(deployment_scope_id)
            target = next(
                (
                    value
                    for value in manifests
                    if value.recovery_set_id == recovery_set_id
                ),
                None,
            )
            if target is None or target.manifest_digest != manifest_digest:
                raise RecoveryEvidenceValidationError("target manifest evidence is unavailable")
            if event_kind is RecoveryEvidenceEventKind.REPLACED:
                replacement = next(
                    (
                        value
                        for value in manifests
                        if value.recovery_set_id == replacement_recovery_set_id
                    ),
                    None,
                )
                if (
                    replacement is None
                    or replacement.manifest_status.value != "coherent"
                    or replacement.manifest_digest != replacement_manifest_digest
                ):
                    raise RecoveryEvidenceValidationError(
                        "replacement manifest evidence is unavailable"
                    )

            current_events = await transaction.list_invalidations(deployment_scope_id)
            ordered_events = tuple(sorted(current_events, key=lambda value: value.sequence))
            next_sequence = (ordered_events[-1].sequence + 1) if ordered_events else 1
            previous_digest = ordered_events[-1].event_digest if ordered_events else None
            if expected_sequence is not None and expected_sequence != next_sequence:
                raise RecoveryEvidenceConflictError("recovery evidence sequence is stale")
            if expected_sequence is not None and expected_previous_event_digest != previous_digest:
                raise RecoveryEvidenceConflictError(
                    "recovery evidence predecessor is stale"
                )
            try:
                event = RecoveryEvidenceInvalidation.build(
                    deployment_scope_id=deployment_scope_id,
                    sequence=next_sequence,
                    recovery_set_id=recovery_set_id,
                    manifest_digest=manifest_digest,
                    event_kind=event_kind,
                    reason_code=reason_code,
                    replacement_recovery_set_id=replacement_recovery_set_id,
                    replacement_manifest_digest=replacement_manifest_digest,
                    replacement_validation_reference=replacement_validation_reference,
                    replacement_approval_reference=replacement_approval_reference,
                    previous_event_digest=previous_digest,
                    occurred_at=self._clock(),
                )
            except ValidationError as error:
                raise RecoveryEvidenceValidationError(
                    "recovery evidence event is invalid"
                ) from error
            try:
                verification = verify_recovery_evidence_chain(
                    deployment_scope_id,
                    manifests=manifests,
                    invalidations=(*ordered_events, event),
                )
            except ValueError as error:
                raise RecoveryEvidenceValidationError(
                    "recovery evidence chain is invalid"
                ) from error
            if not verification.chain_complete:
                raise RecoveryEvidenceValidationError(
                    "recovery evidence chain is not complete"
                )
            await transaction.add_invalidation(event)
            return event

    async def verify(self, deployment_scope_id: str) -> RecoveryEvidenceVerification:
        """Verify all restored records for one scope without mutating them."""

        async with self._repository.transaction(deployment_scope_id) as transaction:
            return verify_recovery_evidence_chain(
                deployment_scope_id,
                manifests=await transaction.list_manifests(deployment_scope_id),
                invalidations=await transaction.list_invalidations(deployment_scope_id),
            )

    async def readiness(self, deployment_scope_id: str) -> RecoveryReadiness:
        """Return a fail-closed readiness signal for the recovery scope."""

        try:
            return derive_recovery_readiness(await self.verify(deployment_scope_id))
        except Exception:
            return RecoveryReadiness(
                ready=False,
                code="recovery.evidence_unavailable",
                trust_state=RecoveryTrustState.UNAVAILABLE,
            )

    async def require_readiness(self, deployment_scope_id: str) -> None:
        """Raise a sanitized coordination failure when recovery is not trusted."""

        readiness = await self.readiness(deployment_scope_id)
        if not readiness.ready:
            raise RecoveryCoordinationError("recovery evidence is not ready")

    @staticmethod
    def reconcile_isolated_restore(
        deployment_scope_id: str,
        *,
        manifests: Iterable[StorageRecoveryManifest],
        invalidations: Iterable[RecoveryEvidenceInvalidation],
        expected_last_sequence: int,
        expected_last_event_digest: str,
    ) -> RecoveryEvidenceVerification:
        """Validate a restored snapshot without writing to the live repository."""

        return verify_recovery_evidence_chain(
            deployment_scope_id,
            manifests=manifests,
            invalidations=invalidations,
            expected_last_sequence=expected_last_sequence,
            expected_last_event_digest=expected_last_event_digest,
        )


__all__ = [
    "RecoveryEvidenceConflictError",
    "RecoveryEvidenceCoordinator",
    "RecoveryEvidenceRepository",
    "RecoveryEvidenceTransaction",
    "RecoveryEvidenceValidationError",
]
