"""Acquire and settle one deletion fence permit for every source contributor."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
from typing import Callable

from spine.application.persistence.context import TrustedPersistenceContext
from spine.application.private_asr_corpus import (
    PrivateArtifactDeletionAuthorization,
    PrivateArtifactDeletionFenceContributorOutcome,
    PrivateArtifactDeletionFenceOutcome,
    PrivateArtifactDeletionFencePort,
    PrivateArtifactDeletionFenceResolver,
    PrivateArtifactDeletionRequestV2,
    PrivateArtifactDeletionSourceBinding,
    PrivateArtifactSourceLineage,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _AcquiredContributor:
    adapter: object
    outcome: PrivateArtifactDeletionFenceContributorOutcome


class CompositePrivateArtifactDeletionFence(PrivateArtifactDeletionFencePort):
    """Coordinate all source contributors as one commit-scoped fence set.

    Contributors are acquired in canonical workspace/environment/source/revision
    order. A failed acquisition rolls back the shared transaction and aborts
    acquired permits in reverse order. Before commit every permit is revalidated;
    known commit/rollback settles the complete set in reverse order. An unknown
    commit outcome is left unresolved for provider expiry and reconciliation.
    """

    def __init__(
        self,
        *,
        resolver: PrivateArtifactDeletionFenceResolver,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._resolver = resolver
        self._clock = clock
        self._leases: dict[int, tuple[_AcquiredContributor, ...]] = {}

    async def acquire(
        self,
        request: PrivateArtifactDeletionRequestV2,
        authorization: PrivateArtifactDeletionAuthorization,
        *,
        context: TrustedPersistenceContext,
        transaction: object,
    ) -> PrivateArtifactDeletionFenceOutcome:
        acquired: list[_AcquiredContributor] = []
        try:
            now = self._clock()
            if (
                not authorization.authorized
                or authorization.request_digest != request.calculate_digest()
                or authorization.authority_version != request.authority_version
                or not request.requested_at <= now < request.valid_until
                or not request.requested_at <= now < authorization.valid_until
            ):
                raise PermissionError(
                    "Private ASR deletion authorization denied."
                )
            for contributor in _ordered_contributors(request.source_bindings):
                adapter = self._resolver.resolve(contributor, context=context)
                outcome = await adapter.acquire_contributor(
                    request,
                    authorization,
                    contributor,
                    context=context,
                    transaction=transaction,
                )
                if not isinstance(outcome, PrivateArtifactDeletionFenceContributorOutcome):
                    raise PermissionError(
                        "Private ASR deletion authorization denied."
                    )
                acquired.append(_AcquiredContributor(adapter, outcome))
                if _lineage_key(outcome.source_lineage) != _lineage_key(
                    _lineage_for_binding(request, contributor)
                ):
                    raise PermissionError(
                        "Private ASR deletion authorization denied."
                    )

                if (
                    outcome.request_digest != request.calculate_digest()
                    or outcome.authorization_reference
                    != authorization.authorization_ref.reference_id
                    or outcome.source_binding_digest != contributor.calculate_digest()
                ):
                    raise PermissionError(
                        "Private ASR deletion authorization denied."
                    )
            acquired_at = self._clock()
            composite = PrivateArtifactDeletionFenceOutcome(
                artifact_id=request.artifact_id,
                workspace_id=request.workspace_id,
                environment_id=request.environment_id,
                request_digest=request.calculate_digest(),
                authorization_reference=authorization.authorization_ref.reference_id,
                contributors=tuple(item.outcome for item in acquired),
                acquired_at=acquired_at,
            )
            if not composite.matches(request, authorization):
                raise PermissionError(
                    "Private ASR deletion authorization denied."
                )
            self._leases[id(composite)] = tuple(acquired)
            return composite
        except asyncio.CancelledError:
            await self._rollback_and_abort(transaction, acquired)
            raise
        except Exception:
            await self._rollback_and_abort(transaction, acquired)
            raise PermissionError(
                "Private ASR deletion authorization denied."
            ) from None

    async def validate_before_commit(
        self,
        outcome: PrivateArtifactDeletionFenceOutcome,
        *,
        transaction: object,
    ) -> None:
        contributors = self._require_lease(outcome)
        for contributor in contributors:
            await contributor.adapter.validate_contributor_before_commit(
                contributor.outcome,
                transaction=transaction,
            )

    async def complete(
        self,
        outcome: PrivateArtifactDeletionFenceOutcome,
        *,
        transaction: object,
        committed: bool,
    ) -> None:
        contributors = self._require_lease(outcome)
        self._leases.pop(id(outcome), None)
        failures: list[Exception] = []
        for contributor in reversed(contributors):
            try:
                await contributor.adapter.complete_contributor(
                    contributor.outcome,
                    transaction=transaction,
                    committed=committed,
                )
            except Exception as error:
                failures.append(error)
        if failures and committed:
            _LOGGER.warning(
                "Committed private-ASR deletion fence settlement is pending provider expiry and reconciliation."
            )
        elif failures:
            raise PermissionError(
                "Private ASR deletion authorization denied."
            ) from None

    async def abandon(
        self,
        outcome: PrivateArtifactDeletionFenceOutcome,
    ) -> None:
        """Forget a lease with unknown commit status without settling providers."""
        self._leases.pop(id(outcome), None)

    def _require_lease(
        self,
        outcome: PrivateArtifactDeletionFenceOutcome,
    ) -> tuple[_AcquiredContributor, ...]:
        contributors = self._leases.get(id(outcome))
        if contributors is None:
            raise PermissionError(
                "Private ASR deletion authorization denied."
            )
        return contributors

    async def _rollback_and_abort(
        self,
        transaction: object,
        acquired: list[_AcquiredContributor],
    ) -> None:
        try:
            await transaction.rollback()  # type: ignore[attr-defined]
        except Exception:
            # Keep external permits held until expiry if transaction closure is
            # uncertain; releasing them could let revocation pass a live write.
            return
        for contributor in reversed(acquired):
            try:
                await contributor.adapter.complete_contributor(
                    contributor.outcome,
                    transaction=transaction,
                    committed=False,
                )
            except Exception:
                # Continue aborting the remaining permits; all failures deny.
                continue


def _ordered_contributors(
    bindings: tuple[PrivateArtifactDeletionSourceBinding, ...],
) -> tuple[PrivateArtifactDeletionSourceBinding, ...]:
    return tuple(sorted(bindings, key=lambda binding: (
        str(binding.workspace_id),
        str(binding.environment_id),
        str(binding.source_object_id),
        str(binding.source_revision_id),
        binding.revision_digest,
    )))


def _lineage_for_binding(
    request: PrivateArtifactDeletionRequestV2,
    binding: PrivateArtifactDeletionSourceBinding,
) -> PrivateArtifactSourceLineage:
    for source in request.expected_source_lineage:
        if (
            source.source_object_id == binding.source_object_id
            and source.source_revision_id == binding.source_revision_id
            and source.source_revision_digest == binding.revision_digest
        ):
            return source
    raise PermissionError("Private ASR deletion authorization denied.")


def _lineage_key(source: PrivateArtifactSourceLineage) -> str:
    return json.dumps(
        source.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


__all__ = ["CompositePrivateArtifactDeletionFence"]
