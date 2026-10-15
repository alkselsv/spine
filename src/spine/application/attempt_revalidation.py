"""Broker-side revalidation of immutable ContextBundle attempts.

This module owns the small seam consumed by the production Context Broker and
later answer-generation code.  It evaluates safe, content-free observations;
it does not read provider payloads, generate answers, or mutate a bundle.
"""

from __future__ import annotations

import asyncio
from typing import Protocol
from uuid import UUID

from spine.domain.context_bundles import (
    ContextBundle,
    ContextBundleOutcome,
    RevalidationAvailability,
    RevalidationCheckpointResult,
    RevalidationDisposition,
    RevalidationFailureCode,
    RevalidationObservation,
    RevalidationPublicationImpact,
    RevalidationRegenerationPlan,
    RevalidationRequest,
    regeneration_ids,
)


class RevalidationStateProvider(Protocol):
    """Resolve one atomic set of current authorization and evidence facts."""

    async def resolve(self, request: RevalidationRequest) -> RevalidationObservation: ...


class RevalidationResultRepository(Protocol):
    """Atomically persist or replay terminal checkpoint results."""

    async def resolve(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        idempotency_key: str,
    ) -> RevalidationCheckpointResult | None: ...

    async def save(
        self, result: RevalidationCheckpointResult
    ) -> RevalidationCheckpointResult | None: ...


class RevalidationUnavailableError(RuntimeError):
    """A provider could not establish a safe observation."""


class RevalidationCancelledError(RuntimeError):
    """A provider stopped a checkpoint before returning an observation."""


class InMemoryRevalidationResultRepository:
    """Deterministic adapter used by offline replay and application tests."""

    def __init__(self) -> None:
        self._results: dict[tuple[UUID, UUID, str], RevalidationCheckpointResult] = {}

    async def resolve(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        idempotency_key: str,
    ) -> RevalidationCheckpointResult | None:
        return self._results.get((workspace_id, environment_id, idempotency_key))

    async def save(self, result: RevalidationCheckpointResult) -> RevalidationCheckpointResult | None:
        key = (result.workspace_id, result.environment_id, result.idempotency_key)
        existing = self._results.get(key)
        if existing is not None and existing.command_digest != result.command_digest:
            raise ValueError("revalidation idempotency key already has another command")
        if existing is not None:
            return existing
        self._results[key] = result
        return None


class AttemptRevalidationService:
    """Deep broker module that classifies one pinned attempt before protected use."""

    def __init__(
        self,
        *,
        state_provider: RevalidationStateProvider,
        result_repository: RevalidationResultRepository,
    ) -> None:
        self._state_provider = state_provider
        self._result_repository = result_repository
        self._lock = asyncio.Lock()

    async def revalidate(self, request: RevalidationRequest) -> RevalidationCheckpointResult:
        """Return a stable replay, bounded regeneration plan, or safe rejection."""

        async with self._lock:
            return await self._revalidate(request)

    async def _revalidate(self, request: RevalidationRequest) -> RevalidationCheckpointResult:

        existing = await self._result_repository.resolve(
            workspace_id=request.workspace_id,
            environment_id=request.environment_id,
            idempotency_key=request.idempotency_key,
        )
        if existing is not None:
            if existing.command_digest == request.command_digest:
                return existing
            return self._result(
                request,
                disposition=RevalidationDisposition.REJECTED,
                failure_codes=(RevalidationFailureCode.IDEMPOTENCY_CONFLICT,),
            )

        if request.cancelled:
            return await self._store(
                self._result(
                    request,
                    disposition=RevalidationDisposition.CANCELLED,
                    bundle_outcome=ContextBundleOutcome.CANCELLED,
                )
            )

        try:
            observation = await self._state_provider.resolve(request)
        except RevalidationCancelledError:
            return await self._store(
                self._result(
                    request,
                    disposition=RevalidationDisposition.CANCELLED,
                    bundle_outcome=ContextBundleOutcome.CANCELLED,
                )
            )
        except RevalidationUnavailableError:
            return await self._handle_unavailable(request)

        if observation.availability is RevalidationAvailability.CANCELLED:
            return await self._store(
                self._result(
                    request,
                    disposition=RevalidationDisposition.CANCELLED,
                    bundle_outcome=ContextBundleOutcome.CANCELLED,
                    activation_changed=observation.publication.activation_changed,
                    active_projection_changed=observation.publication.active_projection_changed,
                )
            )
        if observation.availability is RevalidationAvailability.RETRYABLE_FAILURE:
            return await self._handle_unavailable(request)
        if observation.availability is RevalidationAvailability.UNAVAILABLE:
            return await self._store(
                self._result(
                    request,
                    disposition=RevalidationDisposition.REJECTED,
                    failure_codes=(RevalidationFailureCode.REVALIDATION_UNAVAILABLE,),
                )
            )

        result = self._evaluate(request, observation)
        if result.disposition is RevalidationDisposition.RETRYABLE_FAILURE:
            return result
        return await self._store(result)

    async def _handle_unavailable(
        self, request: RevalidationRequest
    ) -> RevalidationCheckpointResult:
        if request.retry_attempt < request.max_retry_attempts:
            return self._result(
                request,
                disposition=RevalidationDisposition.RETRYABLE_FAILURE,
                failure_codes=(RevalidationFailureCode.REVALIDATION_UNAVAILABLE,),
                retryable=True,
            )
        return await self._store(
            self._result(
                request,
                disposition=RevalidationDisposition.REJECTED,
                failure_codes=(RevalidationFailureCode.REVALIDATION_UNAVAILABLE,),
            )
        )

    async def _store(
        self, result: RevalidationCheckpointResult
    ) -> RevalidationCheckpointResult:
        existing = await self._result_repository.save(result)
        return existing or result

    def _evaluate(
        self,
        request: RevalidationRequest,
        observation: RevalidationObservation,
    ) -> RevalidationCheckpointResult:
        bundle = request.bundle
        activation_changed = observation.publication.activation_changed
        active_projection_changed = observation.publication.active_projection_changed

        integrity_failure = _bundle_integrity_failure(bundle, request)
        if integrity_failure is not None:
            return self._result(
                request,
                disposition=RevalidationDisposition.REJECTED,
                failure_codes=(integrity_failure,),
                activation_changed=activation_changed,
                active_projection_changed=active_projection_changed,
            )
        if (
            observation.workspace_id != request.workspace_id
            or observation.environment_id != request.environment_id
            or not observation.publication.current_publication_available
            or (
                observation.publication.current_activation_generation is None
                and observation.publication.current_publication_available
            )
            or (
                observation.publication.current_snapshot_id is None
                and observation.publication.current_publication_available
            )
        ):
            return self._result(
                request,
                disposition=RevalidationDisposition.REJECTED,
                failure_codes=(RevalidationFailureCode.REVALIDATION_UNAVAILABLE,),
                activation_changed=activation_changed,
                active_projection_changed=active_projection_changed,
            )
        if (
            observation.publication.pinned_snapshot_id
            != bundle.projection_snapshot.projection_snapshot_id
            or observation.publication.pinned_activation_generation
            != bundle.activation_generation
            or observation.publication.pinned_publication_pin_id
            != bundle.publication_pin_id
        ):
            return self._result(
                request,
                disposition=RevalidationDisposition.REJECTED,
                failure_codes=(RevalidationFailureCode.PIN_INCONSISTENT,),
                activation_changed=activation_changed,
                active_projection_changed=active_projection_changed,
            )
        if (
            not observation.authorization_context_current
            or not observation.publication.pin_consistent
        ):
            reason = (
                RevalidationFailureCode.AUTHORITY_GENERATION_CHANGED
                if not observation.authorization_context_current
                else RevalidationFailureCode.PIN_INCONSISTENT
            )
            return self._result(
                request,
                disposition=RevalidationDisposition.REJECTED,
                failure_codes=(reason,),
                activation_changed=activation_changed,
                active_projection_changed=active_projection_changed,
            )

        expected_sources = {item.source_object_id for item in bundle.evidence_references}
        source_by_id = {item.source_object_id: item for item in observation.source_states}
        expected_decisions = {
            item.source_object_id: item for item in bundle.policy_decision_generations
        }
        unsafe: list[RevalidationFailureCode] = []
        recoverable: list[RevalidationFailureCode] = []

        if set(source_by_id) != expected_sources or set(expected_decisions) != expected_sources:
            unsafe.append(RevalidationFailureCode.BUNDLE_LINEAGE_MISMATCH)
        for source_id in sorted(expected_sources, key=str):
            state = source_by_id.get(source_id)
            decision = expected_decisions.get(source_id)
            if state is None or decision is None:
                continue
            if state.current_policy_version_id != decision.policy_version_id:
                unsafe.append(RevalidationFailureCode.POLICY_CHANGED)
            if state.current_policy_generation != decision.policy_generation:
                unsafe.append(RevalidationFailureCode.POLICY_GENERATION_CHANGED)
            if state.membership_generation != decision.membership_generation:
                unsafe.append(RevalidationFailureCode.AUTHORITY_GENERATION_CHANGED)
            if state.authority_generation != decision.authority_generation:
                unsafe.append(RevalidationFailureCode.AUTHORITY_GENERATION_CHANGED)
            if state.service_authority_generation != decision.service_authority_generation:
                unsafe.append(RevalidationFailureCode.AUTHORITY_GENERATION_CHANGED)
            if not state.service_grant_valid:
                unsafe.append(RevalidationFailureCode.SERVICE_GRANT_EXPIRED)
            if not state.contributor_authorized:
                unsafe.append(RevalidationFailureCode.CONTRIBUTOR_UNAUTHORIZED)
            if not state.retention_eligible:
                unsafe.append(RevalidationFailureCode.RETENTION_INELIGIBLE)
            if state.tombstoned:
                unsafe.append(RevalidationFailureCode.SOURCE_TOMBSTONED)
            if state.superseded:
                unsafe.append(RevalidationFailureCode.SOURCE_SUPERSEDED)
            if state.current_revision_id is None:
                unsafe.append(RevalidationFailureCode.SOURCE_REVISION_UNAVAILABLE)
            elif state.current_revision_digest is None:
                unsafe.append(RevalidationFailureCode.SOURCE_REVISION_UNAVAILABLE)
            else:
                matching_retrieval = tuple(
                    item
                    for item in bundle.evidence_references
                    if item.source_object_id == source_id
                )
                if any(
                    item.source_revision_id != state.current_revision_id
                    or (
                        state.current_revision_digest is not None
                        and item.revision_digest != state.current_revision_digest
                    )
                    for item in matching_retrieval
                ) or not state.source_revision_current:
                    recoverable.append(RevalidationFailureCode.SOURCE_REVISION_REPLACED)

        expected_evidence = {item.evidence_id: item for item in bundle.evidence_references}
        evidence_by_id = {item.evidence_id: item for item in observation.evidence_states}
        if set(evidence_by_id) != set(expected_evidence):
            recoverable.append(RevalidationFailureCode.EVIDENCE_MISSING)
        for evidence_id in sorted(expected_evidence):
            expected = expected_evidence[evidence_id]
            state = evidence_by_id.get(evidence_id)
            if state is None:
                continue
            if state.source_object_id != expected.source_object_id:
                unsafe.append(RevalidationFailureCode.EVIDENCE_LINEAGE_MISMATCH)
            elif (
                state.workspace_id != request.workspace_id
                or state.environment_id != request.environment_id
                or state.projection_snapshot_id
                != bundle.projection_snapshot.projection_snapshot_id
                or state.activation_generation != bundle.activation_generation
                or state.source_revision_id != expected.source_revision_id
                or state.revision_digest != expected.revision_digest
                or state.revision_number != expected.revision_number
                or state.processing_receipt_id != expected.processing_receipt_id
                or state.locator_contract_version != expected.locator_contract_version
                or state.locator != expected.locator
                or state.evidence_digest != expected.evidence_digest
            ):
                unsafe.append(RevalidationFailureCode.EVIDENCE_LINEAGE_MISMATCH)
            elif not state.present:
                recoverable.append(RevalidationFailureCode.EVIDENCE_MISSING)
            elif not state.digest_matches:
                recoverable.append(RevalidationFailureCode.EVIDENCE_CORRUPT)
            elif not state.provenance_valid:
                recoverable.append(RevalidationFailureCode.PROVENANCE_UNVERIFIED)

        if observation.publication.impact in {
            RevalidationPublicationImpact.MATERIAL,
            RevalidationPublicationImpact.UNKNOWN,
        }:
            recoverable.append(RevalidationFailureCode.ACTIVE_PROJECTION_CHANGED)

        unsafe = _unique(unsafe)
        recoverable = _unique(recoverable)
        if unsafe:
            return self._result(
                request,
                disposition=RevalidationDisposition.REJECTED,
                failure_codes=tuple(unsafe),
                activation_changed=activation_changed,
                active_projection_changed=active_projection_changed,
            )
        if recoverable:
            if request.regeneration_count >= request.regeneration_budget:
                return self._result(
                    request,
                    disposition=RevalidationDisposition.REJECTED,
                    failure_codes=tuple((*recoverable, RevalidationFailureCode.REGENERATION_LIMIT)),
                    activation_changed=activation_changed,
                    active_projection_changed=active_projection_changed,
                )
            new_attempt, new_pin, new_key = regeneration_ids(
                bundle=bundle, request=request
            )
            plan = RevalidationRegenerationPlan(
                prior_bundle=bundle.reference,
                prior_attempt_id=bundle.attempt_id,
                new_attempt_id=new_attempt,
                new_publication_pin_id=new_pin,
                new_idempotency_key=new_key,
                regeneration_number=request.regeneration_count + 1,
                reasons=tuple(recoverable),
            )
            return self._result(
                request,
                disposition=RevalidationDisposition.SAFE_REGENERATION,
                failure_codes=tuple(recoverable),
                regeneration=plan,
                activation_changed=activation_changed,
                active_projection_changed=active_projection_changed,
            )
        return self._result(
            request,
            disposition=RevalidationDisposition.SAFE_REPLAY,
            activation_changed=activation_changed,
            active_projection_changed=active_projection_changed,
        )

    @staticmethod
    def _result(
        request: RevalidationRequest,
        *,
        disposition: RevalidationDisposition,
        failure_codes: tuple[RevalidationFailureCode, ...] = (),
        regeneration: RevalidationRegenerationPlan | None = None,
        bundle_outcome: ContextBundleOutcome | None = None,
        activation_changed: bool = False,
        active_projection_changed: bool = False,
        retryable: bool = False,
    ) -> RevalidationCheckpointResult:
        return RevalidationCheckpointResult(
            disposition=disposition,
            checkpoint=request.checkpoint,
            workspace_id=request.workspace_id,
            environment_id=request.environment_id,
            bundle_reference=(
                request.bundle.reference
                if disposition is RevalidationDisposition.SAFE_REPLAY
                else None
            ),
            bundle_outcome=(
                bundle_outcome
                if bundle_outcome is not None
                else (
                    request.bundle.outcome
                    if disposition is RevalidationDisposition.SAFE_REPLAY
                    else None
                )
            ),
            idempotency_key=request.idempotency_key,
            command_digest=request.command_digest,
            failure_codes=failure_codes,
            regeneration=regeneration,
            activation_changed=activation_changed,
            active_projection_changed=active_projection_changed,
            retryable=retryable,
            retry_attempt=request.retry_attempt,
        )


def _bundle_integrity_failure(
    bundle: ContextBundle, request: RevalidationRequest
) -> RevalidationFailureCode | None:
    try:
        if ContextBundle.calculate_digest(bundle) != bundle.bundle_digest:
            return RevalidationFailureCode.BUNDLE_INTEGRITY
        ContextBundle.model_validate(bundle.model_dump(mode="python"))
    except Exception:
        return RevalidationFailureCode.BUNDLE_INTEGRITY
    if bundle.workspace_id != request.workspace_id or bundle.environment_id != request.environment_id:
        return RevalidationFailureCode.BUNDLE_LINEAGE_MISMATCH
    return None


def _unique(values: list[RevalidationFailureCode]) -> list[RevalidationFailureCode]:
    return list(dict.fromkeys(values))


__all__ = [
    "AttemptRevalidationService",
    "InMemoryRevalidationResultRepository",
    "RevalidationCancelledError",
    "RevalidationResultRepository",
    "RevalidationStateProvider",
    "RevalidationUnavailableError",
]
