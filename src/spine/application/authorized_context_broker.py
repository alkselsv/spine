"""The production application service for one authorized context attempt.

The service is deliberately an orchestration boundary.  It owns the order in
which existing Context Broker authorities are composed, but it does not know
about HTTP, a database, Cognee, a workflow engine, or an LLM provider.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Literal, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from spine.application.authorized_retrieval_scope import (
    AuthorizedRetrievalScope,
    AuthorizedRetrievalScopeOutcome,
    AuthorizedRetrievalScopeRequest,
    AuthorizedRetrievalScopeResolver,
)
from spine.application.context_profiles import (
    ContextProfileResolution,
    ContextProfileResolutionOutcome,
    ContextProfileResolutionPort,
    ContextProfileResolutionRequest,
)
from spine.application.diagnostics import DiagnosticContext
from spine.application.evidence_lineage_verification import (
    EvidenceItem,
    EvidenceLineageVerifier,
    EvidenceVerificationOutcome,
    EvidenceVerificationReasonCode,
    RejectedEvidenceItem,
    EvidenceVerificationDiagnostic,
)
from spine.application.retrieval_gates import (
    RetrievalGateDecision,
    RetrievalGateRequest,
    RetrievalGateService,
)
from spine.application.persistence.errors import ContextBundleConflictError
from spine.domain.context_bundles import (
    ContextBundle,
    ContextBundleEvidenceReference,
    ContextBundleEvidenceStatus,
    ContextBundleOutcome,
    ContextBundleRef,
    ContextBundleState,
    EvidenceLocator,
    PolicyDecisionGeneration,
    RevalidationCheckpointResult,
    RevalidationRequest,
)
from spine.domain.common import ActorKind
from spine.domain.projections import (
    ProjectionRun,
    ProjectionSnapshotMember,
    ProjectionPartition,
    ProjectionReceipt,
    SourceProjectionReference,
    PublishedActivePublication,
    UnavailableActivePublication,
    UnpublishedActivePublication,
)
from spine.domain.sources import CanonicalBoundary as DomainCanonicalBoundary
from spine.memory.contracts import (
    ContextOutcome,
    ContextReasonCode,
    ContextRequest,
    ContextResponse,
    ProjectionOutcome,
    ProjectionRetrievalResult,
    PublicationPin,
)
from spine.memory.read_plan import (
    PhysicalReadContext,
    PhysicalReadPlanBuilder,
    PhysicalReadPlanResolution,
    PhysicalReadIsolationRequirement,
)
from spine.memory.retrieval import PhysicalReadPlanExecutor
from spine.application.content_authorization import TrustedAuthorizationContext
from spine.application.qa_observation.context_capture import (
    AuthorizedContextCapture,
    AuthorizedContextCaptureContext,
    AuthorizedContextCaptureResult,
    AuthorizedContextCaptureSink,
    CaptureDisposition,
    retrieval_result_from_projection,
)


class TrustedRequestContextPort(Protocol):
    """Resolve a server-issued request reference into one auth context."""

    async def resolve(
        self, request: "AuthorizedContextRequest"
    ) -> TrustedAuthorizationContext | None: ...


class ActivePublicationPort(Protocol):
    """Read the active publication exactly once for one attempt."""

    async def resolve_active_publication(
        self, projection_kind: str
    ) -> (
        PublishedActivePublication
        | UnpublishedActivePublication
        | UnavailableActivePublication
    ): ...


@dataclass(frozen=True, slots=True)
class ContextBrokerReadModel:
    """Canonical metadata required to construct one pinned physical read."""

    projection_run: ProjectionRun
    snapshot_members: tuple[ProjectionSnapshotMember, ...]
    partitions: tuple[ProjectionPartition, ...]
    receipts: tuple[ProjectionReceipt, ...]
    source_references: tuple[SourceProjectionReference, ...]
    canonical_boundary: DomainCanonicalBoundary
    provider_capability_reference: str
    max_partitions: int = 64


class ContextBrokerReadPort(Protocol):
    """Load canonical read metadata after source authorization succeeds."""

    async def load(
        self,
        *,
        publication: PublishedActivePublication,
        scope: AuthorizedRetrievalScope,
    ) -> ContextBrokerReadModel: ...


class ContextBundleStore(Protocol):
    """Transactional replay/persistence port for immutable bundle handoffs.

    Implementations must perform ``create_or_replay`` and its response handoff
    in one UnitOfWork.  A changed bundle for an existing attempt is a conflict.
    """

    async def resolve_attempt(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        run_id: UUID,
        attempt_id: UUID,
        idempotency_key: str,
    ) -> ContextBundle | None: ...

    async def create_or_replay(self, bundle: ContextBundle) -> ContextBundle: ...

    async def resolve(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        reference: ContextBundleRef,
    ) -> ContextBundle: ...


class SavedContextBundlePort(Protocol):
    """Downstream handoff for loading and revalidating a saved bundle."""

    async def load(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        reference: ContextBundleRef,
    ) -> ContextBundle: ...

    async def revalidate(
        self, request: RevalidationRequest
    ) -> RevalidationCheckpointResult: ...


class ContextBundleRevalidator(Protocol):
    async def revalidate(
        self, request: RevalidationRequest
    ) -> RevalidationCheckpointResult: ...


class PhysicalReadPlanPort(Protocol):
    async def resolve(
        self, context: PhysicalReadContext
    ) -> PhysicalReadPlanResolution: ...


class StructuredReadPort(Protocol):
    async def execute(
        self, plan: PhysicalReadPlanResolution
    ) -> ProjectionRetrievalResult: ...


class AuthorizedContextRequest(ContextRequest):
    """The closed, server-routed request envelope for one run/attempt."""

    schema_version: Literal["authorized-context-request:v1"] = (
        "authorized-context-request:v1"
    )
    run_id: UUID
    attempt_id: UUID
    idempotency_key: str
    diagnostic_context: DiagnosticContext
    max_tokens: int | None = None
    max_relation_hops: int | None = None
    freshness_max_age_seconds: int | None = None
    allow_partial_answer: bool | None = None
    cancelled: bool = False


class InMemoryContextBundleStore:
    """Small deterministic adapter used by application and replay tests."""

    def __init__(self) -> None:
        self._bundles: dict[tuple[UUID, UUID, UUID, UUID, str], ContextBundle] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _key(
        *,
        workspace_id: UUID,
        environment_id: UUID,
        run_id: UUID,
        attempt_id: UUID,
        idempotency_key: str,
    ) -> tuple[UUID, UUID, UUID, UUID, str]:
        return workspace_id, environment_id, run_id, attempt_id, idempotency_key

    async def resolve_attempt(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        run_id: UUID,
        attempt_id: UUID,
        idempotency_key: str,
    ) -> ContextBundle | None:
        return self._bundles.get(
            self._key(
                workspace_id=workspace_id,
                environment_id=environment_id,
                run_id=run_id,
                attempt_id=attempt_id,
                idempotency_key=idempotency_key,
            )
        )

    async def create_or_replay(self, bundle: ContextBundle) -> ContextBundle:
        async with self._lock:
            key = self._key(
                workspace_id=bundle.workspace_id,
                environment_id=bundle.environment_id,
                run_id=bundle.run_id,
                attempt_id=bundle.attempt_id,
                idempotency_key=bundle.idempotency_key,
            )
            existing = self._bundles.get(key)
            if existing is not None:
                if existing != bundle:
                    raise ContextBundleConflictError(
                        "Context bundle attempt conflicts with history."
                    )
                return existing
            self._bundles[key] = bundle
            return bundle

    async def resolve(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        reference: ContextBundleRef,
    ) -> ContextBundle:
        for bundle in self._bundles.values():
            if (
                bundle.workspace_id == workspace_id
                and bundle.environment_id == environment_id
                and bundle.reference == reference
            ):
                return bundle
        raise ValueError("context bundle is unavailable")


class SavedContextBundleAccess:
    """Connect bundle loading with the existing safe revalidation service."""

    def __init__(
        self, *, store: ContextBundleStore, revalidator: ContextBundleRevalidator
    ) -> None:
        self._store = store
        self._revalidator = revalidator

    async def load(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        reference: ContextBundleRef,
    ) -> ContextBundle:
        return await self._store.resolve(
            workspace_id=workspace_id,
            environment_id=environment_id,
            reference=reference,
        )

    async def revalidate(
        self, request: RevalidationRequest
    ) -> RevalidationCheckpointResult:
        return await self._revalidator.revalidate(request)


class AuthorizedContextBrokerApplicationService:
    """Compose one authorized, version-pinned retrieval attempt."""

    def __init__(
        self,
        *,
        trusted_context: TrustedRequestContextPort,
        profile_resolver: ContextProfileResolutionPort,
        publication: ActivePublicationPort,
        scope_resolver: AuthorizedRetrievalScopeResolver,
        read_model: ContextBrokerReadPort,
        plan_builder: PhysicalReadPlanPort | PhysicalReadPlanBuilder,
        retrieval: StructuredReadPort | PhysicalReadPlanExecutor,
        evidence_verifier: EvidenceLineageVerifier,
        bundles: ContextBundleStore,
        provider_capability_reference: str = "context.retrieve",
        projection_kind: str = "qa",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._trusted_context = trusted_context
        self._profile_resolver = profile_resolver
        self._publication = publication
        self._scope_resolver = scope_resolver
        self._read_model = read_model
        self._plan_builder = plan_builder
        self._retrieval = retrieval
        self._evidence_verifier = evidence_verifier
        self._bundles = bundles
        self._provider_capability_reference = provider_capability_reference
        self._projection_kind = projection_kind
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._attempt_locks: dict[tuple[UUID, UUID, UUID, UUID, str], asyncio.Lock] = {}
        self._attempt_locks_guard = asyncio.Lock()

    async def retrieve(self, request: AuthorizedContextRequest) -> ContextResponse:
        """Serialize one attempt identity before reading or issuing a bundle."""

        if type(request) is not AuthorizedContextRequest:
            return self._response(
                ContextOutcome.DENIED, ContextReasonCode.INVALID_TRUSTED_CONTEXT
            )
        key = (
            request.workspace_id,
            request.environment_id,
            request.run_id,
            request.attempt_id,
            request.idempotency_key,
        )
        async with await self._attempt_lock(key):
            return await self._retrieve(request)

    async def retrieve_with_capture(
        self,
        request: AuthorizedContextRequest,
        *,
        capture_context: AuthorizedContextCaptureContext,
        capture_sink: AuthorizedContextCaptureSink,
        capture_timeout_seconds: float = 1.0,
    ) -> AuthorizedContextCaptureResult:
        """Capture typed values from this call without changing its response."""

        disposition = CaptureDisposition.NOT_ELIGIBLE
        if (
            type(capture_context) is not AuthorizedContextCaptureContext
            or not callable(getattr(capture_sink, "capture", None))
            or type(request) is not AuthorizedContextRequest
            or capture_context.identity.workspace_id != request.workspace_id
            or capture_context.identity.environment_id != request.environment_id
            or capture_context.identity.run_id != request.run_id
            or capture_context.identity.attempt_id != request.attempt_id
            or capture_context.question.subject.text != request.query
            or capture_context.subscription.expires_at <= self._clock()
            or capture_timeout_seconds <= 0
        ):
            response = await self.retrieve(request)
            return AuthorizedContextCaptureResult(
                response=response,
                disposition=CaptureDisposition.INVALID_CONTEXT,
            )

        async def capture_success(
            projection: ProjectionRetrievalResult,
            response: ContextResponse,
            bundle: ContextBundle,
            pin: PublicationPin,
            trusted: TrustedAuthorizationContext,
        ) -> None:
            nonlocal disposition
            try:
                if capture_context.subscription.expires_at <= self._clock():
                    disposition = CaptureDisposition.EXPIRED
                    return
                if (
                    capture_context.identity.requester.kind is not ActorKind.HUMAN
                    or capture_context.identity.requester.id
                    != trusted.canonical_human_id
                    or capture_context.question.correlation.trace_id != trusted.trace_id
                ):
                    disposition = CaptureDisposition.INVALID_CONTEXT
                    return
                retrieval_result = retrieval_result_from_projection(
                    projection,
                    retrieval_strategy=bundle.retrieval_strategy,
                    outcome=response.outcome,
                )
                capture = AuthorizedContextCapture(
                    capture_context=capture_context,
                    response=response,
                    projection_retrieval=projection,
                    retrieval=retrieval_result,
                    context_bundle=bundle,
                    publication_pin=pin,
                )
            except Exception:
                disposition = CaptureDisposition.INVALID_LINEAGE
                return
            try:
                await asyncio.wait_for(
                    capture_sink.capture(capture), timeout=capture_timeout_seconds
                )
            except asyncio.TimeoutError:
                disposition = CaptureDisposition.SINK_TIMEOUT
            except Exception:
                disposition = CaptureDisposition.SINK_FAILED
            else:
                disposition = CaptureDisposition.CAPTURED

        key = (
            request.workspace_id,
            request.environment_id,
            request.run_id,
            request.attempt_id,
            request.idempotency_key,
        )
        async with await self._attempt_lock(key):
            response = await self._retrieve(request, capture_success=capture_success)
        return AuthorizedContextCaptureResult(
            response=response,
            disposition=disposition,
        )

    async def _attempt_lock(
        self, key: tuple[UUID, UUID, UUID, UUID, str]
    ) -> asyncio.Lock:
        async with self._attempt_locks_guard:
            return self._attempt_locks.setdefault(key, asyncio.Lock())

    async def _retrieve(
        self,
        request: AuthorizedContextRequest,
        *,
        capture_success: (
            Callable[
                [
                    ProjectionRetrievalResult,
                    ContextResponse,
                    ContextBundle,
                    PublicationPin,
                    TrustedAuthorizationContext,
                ],
                Awaitable[None],
            ]
            | None
        ) = None,
    ) -> ContextResponse:
        """Execute one complete flow and return only the typed v1 response."""

        if type(request) is not AuthorizedContextRequest:
            return self._response(
                ContextOutcome.DENIED, ContextReasonCode.INVALID_TRUSTED_CONTEXT
            )

        trusted = await self._resolve_trusted_context(request)
        if trusted is None:
            return self._response(
                ContextOutcome.DENIED, ContextReasonCode.INVALID_TRUSTED_CONTEXT
            )

        command_digest = request_command_digest(request)
        expected_bundle_id = context_bundle_id_for_request(request)
        try:
            replay = await self._resolve_replay(request)
        except Exception:
            return self._response(
                ContextOutcome.UNAVAILABLE, ContextReasonCode.BUNDLE_UNAVAILABLE
            )
        if replay is not None:
            if replay.bundle_id != expected_bundle_id:
                return self._response(
                    ContextOutcome.UNAVAILABLE, ContextReasonCode.RETRIEVAL_UNAVAILABLE
                )
            return self._response_from_bundle(replay)

        profile_resolution = await self._resolve_profile(request)
        if (
            profile_resolution is None
            or profile_resolution.outcome
            is not ContextProfileResolutionOutcome.RESOLVED
            or profile_resolution.profile is None
            or profile_resolution.policy is None
            or profile_resolution.profile.reference != request.profile_ref
            or profile_resolution.policy.workspace_id != request.workspace_id
            or profile_resolution.policy.environment_id != request.environment_id
        ):
            return self._response(
                ContextOutcome.UNAVAILABLE, ContextReasonCode.PROFILE_UNAVAILABLE
            )

        publication = await self._resolve_publication()
        if not isinstance(publication, PublishedActivePublication):
            return self._response(
                ContextOutcome.UNAVAILABLE, ContextReasonCode.PUBLICATION_UNAVAILABLE
            )
        if (
            publication.workspace_id != request.workspace_id
            or publication.environment_id != request.environment_id
            or publication.projection_kind != self._projection_kind
        ):
            return self._response(
                ContextOutcome.UNAVAILABLE, ContextReasonCode.PUBLICATION_UNAVAILABLE
            )
        try:
            pin = PublicationPin.from_publication(publication)
        except Exception:
            return self._response(
                ContextOutcome.UNAVAILABLE, ContextReasonCode.PUBLICATION_UNAVAILABLE
            )
        if not pin.has_valid_digest():
            return self._response(
                ContextOutcome.UNAVAILABLE, ContextReasonCode.PUBLICATION_UNAVAILABLE
            )
        if request.cancelled:
            return self._response(
                ContextOutcome.CANCELLED,
                ContextReasonCode.CANCELLED,
                publication_pin_id=pin.pin_id,
            )

        scope_resolution = await self._resolve_scope(
            trusted, profile_resolution, publication
        )
        if scope_resolution.outcome is not AuthorizedRetrievalScopeOutcome.RESOLVED:
            return self._scope_response(scope_resolution)
        assert scope_resolution.scope is not None
        scope = scope_resolution.scope

        try:
            read_model = await self._read_model.load(
                publication=publication,
                scope=scope,
            )
            self._validate_read_model_lineage(read_model, publication)
            context = self._physical_read_context(
                request=request,
                pin=pin,
                scope=scope,
                read_model=read_model,
            )
            plan = await self._plan_builder.resolve(context)
            if request.cancelled:
                retrieval = self._cancelled_retrieval(command_digest)
                verifications: tuple[EvidenceVerificationOutcome, ...] = ()
            else:
                retrieval = await self._retrieval.execute(plan)
                verifications = await self._verify_items(retrieval, pin, trusted)
        except asyncio.CancelledError:
            return self._response(ContextOutcome.CANCELLED, ContextReasonCode.CANCELLED)
        except Exception:
            return self._response(
                ContextOutcome.UNAVAILABLE, ContextReasonCode.RETRIEVAL_UNAVAILABLE
            )

        gate = self._gate(
            request=request,
            profile=profile_resolution,
            publication=publication,
            retrieval=retrieval,
            verifications=verifications,
            scope=scope,
        )
        if gate.publication_pin is not None and gate.publication_pin != pin:
            return self._response(
                ContextOutcome.UNAVAILABLE, ContextReasonCode.PUBLICATION_UNAVAILABLE
            )
        if not gate.bundle_eligible or gate.outcome not in {
            ContextOutcome.ALLOWED,
            ContextOutcome.PARTIAL,
        }:
            return self._response(
                gate.outcome,
                gate.reason_code,
                publication_pin_id=pin.pin_id,
            )

        try:
            bundle = self._build_bundle(
                request=request,
                profile=profile_resolution,
                publication=publication,
                pin=pin,
                scope=scope,
                gate=gate,
                canonical_boundary=read_model.canonical_boundary,
                bundle_id=expected_bundle_id,
            )
            stored = await self._bundles.create_or_replay(bundle)
        except (ContextBundleConflictError, ValueError, TypeError):
            return self._response(
                ContextOutcome.UNAVAILABLE, ContextReasonCode.RETRIEVAL_UNAVAILABLE
            )
        except Exception:
            return self._response(
                ContextOutcome.UNAVAILABLE, ContextReasonCode.BUNDLE_UNAVAILABLE
            )
        response = self._response_from_bundle(stored)
        if capture_success is not None:
            try:
                await capture_success(retrieval, response, stored, pin, trusted)
            except Exception:
                # Observability failures never change the authorized response.
                pass
        return response

    async def _resolve_trusted_context(
        self, request: AuthorizedContextRequest
    ) -> TrustedAuthorizationContext | None:
        try:
            context = await self._trusted_context.resolve(request)
        except Exception:
            return None
        if (
            not isinstance(context, TrustedAuthorizationContext)
            or context.workspace_id != request.workspace_id
            or context.environment_id != request.environment_id
            or context.purpose != "answer_question"
            or context.operation != "retrieve_context"
        ):
            return None
        return context

    async def _resolve_profile(
        self, request: AuthorizedContextRequest
    ) -> ContextProfileResolution | None:
        try:
            return await self._profile_resolver.resolve(
                ContextProfileResolutionRequest(
                    workspace_id=request.workspace_id,
                    environment_id=request.environment_id,
                    profile_ref=request.profile_ref,
                    trusted_request_context_ref=request.trusted_request_context_ref,
                    authorization_context_ref=request.authorization_context_ref,
                    limit=request.limit,
                    max_tokens=request.max_tokens,
                    max_relation_hops=request.max_relation_hops,
                    freshness_max_age_seconds=request.freshness_max_age_seconds,
                    allow_partial_answer=request.allow_partial_answer,
                )
            )
        except Exception:
            return None

    async def _resolve_publication(
        self,
    ) -> (
        PublishedActivePublication
        | UnpublishedActivePublication
        | UnavailableActivePublication
        | None
    ):
        try:
            return await self._publication.resolve_active_publication(
                self._projection_kind
            )
        except Exception:
            return None

    async def _resolve_scope(
        self,
        trusted: TrustedAuthorizationContext,
        profile: ContextProfileResolution,
        publication: PublishedActivePublication,
    ):
        try:
            return await self._scope_resolver.resolve(
                AuthorizedRetrievalScopeRequest(
                    trusted_context=trusted,
                    profile=profile.policy,
                    publication=publication,
                )
            )
        except Exception:
            return self._scope_resolver_failure()

    def _physical_read_context(
        self,
        *,
        request: AuthorizedContextRequest,
        pin: PublicationPin,
        scope: AuthorizedRetrievalScope,
        read_model: ContextBrokerReadModel,
    ) -> PhysicalReadContext:
        constraints = tuple(
            sorted(scope.source_constraints, key=lambda item: item.source_object_id.hex)
        )
        object_ids = tuple(item.source_object_id for item in constraints)
        revision_ids = tuple(item.source_revision_id for item in constraints)
        return PhysicalReadContext(
            query=request.query,
            diagnostic_context=request.diagnostic_context,
            publication_pin=pin,
            authorized_scope=scope,
            projection_run=read_model.projection_run,
            snapshot_members=read_model.snapshot_members,
            partitions=read_model.partitions,
            receipts=read_model.receipts,
            source_references=read_model.source_references,
            source_isolation=PhysicalReadIsolationRequirement(
                provider_capability_reference=(
                    read_model.provider_capability_reference
                    or self._provider_capability_reference
                ),
                source_object_ids=object_ids,
                source_revision_ids=revision_ids,
                pre_retrieval_enforced=True,
            ),
            max_partitions=read_model.max_partitions,
        )

    @staticmethod
    def _validate_read_model_lineage(
        read_model: ContextBrokerReadModel,
        publication: PublishedActivePublication,
    ) -> None:
        boundary = read_model.canonical_boundary
        if (
            boundary.workspace_id != publication.workspace_id
            or boundary.environment_id != publication.environment_id
            or boundary.boundary_id != publication.canonical_boundary_id
            or boundary.manifest_digest
            != publication.canonical_boundary_manifest_digest
            or boundary.accepted_through_generation
            != publication.accepted_through_generation
            or not read_model.provider_capability_reference
            or read_model.max_partitions < 1
        ):
            raise ValueError("read model is outside the pinned publication")

    async def _verify_items(
        self,
        retrieval: ProjectionRetrievalResult,
        pin: PublicationPin,
        trusted: TrustedAuthorizationContext,
    ) -> tuple[EvidenceVerificationOutcome, ...]:
        items: tuple[EvidenceItem, ...] = (
            *retrieval.chunks,
            *retrieval.entities,
            *retrieval.relations,
        )
        results: list[EvidenceVerificationOutcome] = []
        for item in items:
            try:
                results.append(
                    await self._evidence_verifier.verify(
                        item=item,
                        publication_pin=pin,
                        authorization_context=trusted,
                    )
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                results.append(
                    RejectedEvidenceItem(
                        reason_code=EvidenceVerificationReasonCode.VERIFICATION_UNAVAILABLE,
                        decision_reference=uuid5(
                            NAMESPACE_URL,
                            f"spine:evidence-verification-failure:{trusted.trace_id}:{len(results)}",
                        ),
                        diagnostic=EvidenceVerificationDiagnostic(retryable=True),
                    )
                )
        return tuple(results)

    def _gate(
        self,
        *,
        request: AuthorizedContextRequest,
        profile: ContextProfileResolution,
        publication: PublishedActivePublication,
        retrieval: ProjectionRetrievalResult,
        verifications: tuple[EvidenceVerificationOutcome, ...],
        scope: AuthorizedRetrievalScope,
    ) -> RetrievalGateDecision:
        generations = tuple(
            PolicyDecisionGeneration(
                source_object_id=item.source_object_id,
                policy_version_id=item.policy_version_id,
                policy_generation=item.policy_generation,
                membership_generation=item.membership_generation,
                authority_generation=item.authority_generation,
                service_authority_generation=item.service_authority_generation,
                decision_reference=item.decision_reference,
            )
            for item in scope.source_constraints
        )
        return RetrievalGateService().evaluate(
            RetrievalGateRequest(
                workspace_id=request.workspace_id,
                environment_id=request.environment_id,
                profile=profile.policy,
                publication=publication,
                retrieval=retrieval,
                evidence_verifications=verifications,
                policy_decision_generations=generations,
                cancelled=request.cancelled,
            )
        )

    def _build_bundle(
        self,
        *,
        request: AuthorizedContextRequest,
        profile: ContextProfileResolution,
        publication: PublishedActivePublication,
        pin: PublicationPin,
        scope: AuthorizedRetrievalScope,
        gate: RetrievalGateDecision,
        canonical_boundary: DomainCanonicalBoundary,
        bundle_id: UUID,
    ) -> ContextBundle:
        if gate.publication_pin is None:
            raise ValueError("bundle requires a publication pin")
        evidence = tuple(
            ContextBundleEvidenceReference(
                evidence_id=item.evidence_id,
                workspace_id=item.workspace_id,
                environment_id=item.environment_id,
                projection_snapshot_id=pin.projection_snapshot.projection_snapshot_id,
                activation_generation=pin.activation_generation,
                source_object_id=item.source_object_id,
                source_revision_id=item.source_revision_id,
                revision_digest=item.revision_digest,
                revision_number=item.revision_number,
                processing_receipt_id=item.processing_receipt_id,
                locator_contract_version=item.locator_contract_version,
                locator=EvidenceLocator(
                    kind=item.locator.kind, value=item.locator.value
                ),
                evidence_digest=item.evidence_digest,
                status=ContextBundleEvidenceStatus(item.status),
            )
            for item in gate.evidence_references
        )
        outcome = ContextBundleOutcome(gate.outcome.value)
        state = (
            ContextBundleState.PARTIAL
            if outcome is ContextBundleOutcome.PARTIAL
            else ContextBundleState.COMPLETE
        )
        return ContextBundle.create(
            bundle_id=bundle_id,
            bundle_version=1,
            workspace_id=request.workspace_id,
            environment_id=request.environment_id,
            profile_ref=profile.profile.reference,
            trusted_request_context_ref=request.trusted_request_context_ref,
            authorization_context_ref=request.authorization_context_ref,
            request_id=request.request_id,
            run_id=request.run_id,
            attempt_id=request.attempt_id,
            publication_pin_id=pin.pin_id,
            idempotency_key=request.idempotency_key,
            projection_snapshot=publication.projection_snapshot,
            projection_activation=publication.projection_activation,
            activation_generation=publication.activation_generation,
            canonical_boundary=canonical_boundary,
            canonical_as_of=publication.as_of,
            projection_config_version=publication.projection_config_version,
            freshness=publication.freshness,
            health=publication.health,
            effective_reconciliation=publication.effective_reconciliation,
            retrieval_strategy=scope.retrieval_strategy.value,
            outcome=outcome,
            reason_code=gate.reason_code.value,
            evidence_references=evidence,
            policy_decision_generations=gate.policy_decision_generations,
            state=state,
            created_at=self._clock(),
        )

    async def _resolve_replay(
        self, request: AuthorizedContextRequest
    ) -> ContextBundle | None:
        return await self._bundles.resolve_attempt(
            workspace_id=request.workspace_id,
            environment_id=request.environment_id,
            run_id=request.run_id,
            attempt_id=request.attempt_id,
            idempotency_key=request.idempotency_key,
        )

    @staticmethod
    def _scope_resolver_failure():
        from spine.application.authorized_retrieval_scope import (
            AuthorizedRetrievalScopeReason,
            AuthorizedRetrievalScopeResolution,
        )

        return AuthorizedRetrievalScopeResolution(
            outcome=AuthorizedRetrievalScopeOutcome.UNAVAILABLE,
            reason=AuthorizedRetrievalScopeReason.AUTHORIZATION_UNAVAILABLE,
        )

    @staticmethod
    def _cancelled_retrieval(command_digest: str) -> ProjectionRetrievalResult:
        return ProjectionRetrievalResult(
            outcome=ProjectionOutcome.CANCELLED,
            command_identity=f"cancelled-{command_digest[:24]}",
            command_digest=command_digest,
        )

    @staticmethod
    def _scope_response(resolution) -> ContextResponse:
        from spine.application.authorized_retrieval_scope import (
            AuthorizedRetrievalScopeReason,
        )

        if resolution.outcome is AuthorizedRetrievalScopeOutcome.DENIED:
            reason = (
                ContextReasonCode.INVALID_TRUSTED_CONTEXT
                if resolution.reason
                is AuthorizedRetrievalScopeReason.INVALID_TRUSTED_CONTEXT
                else ContextReasonCode.AUTHORIZATION_DENIED
            )
            return ContextResponse(
                outcome=ContextOutcome.DENIED,
                reason_code=reason,
            )
        if resolution.reason is AuthorizedRetrievalScopeReason.PUBLICATION_UNAVAILABLE:
            return ContextResponse(
                outcome=ContextOutcome.UNAVAILABLE,
                reason_code=ContextReasonCode.PUBLICATION_UNAVAILABLE,
            )
        return ContextResponse(
            outcome=ContextOutcome.UNAVAILABLE,
            reason_code=ContextReasonCode.RETRIEVAL_UNAVAILABLE,
        )

    @staticmethod
    def _response(
        outcome: ContextOutcome,
        reason: ContextReasonCode,
        *,
        publication_pin_id: UUID | None = None,
    ) -> ContextResponse:
        return ContextResponse(
            outcome=outcome,
            reason_code=reason,
            publication_pin_id=publication_pin_id,
        )

    @staticmethod
    def _response_from_bundle(bundle: ContextBundle) -> ContextResponse:
        outcome = ContextOutcome(bundle.outcome.value)
        reason = ContextReasonCode(bundle.reason_code)
        reference = (
            bundle.reference
            if outcome
            in {
                ContextOutcome.ALLOWED,
                ContextOutcome.PARTIAL,
            }
            else None
        )
        return ContextResponse(
            outcome=outcome,
            context_bundle_ref=reference,
            reason_code=reason,
            publication_pin_id=bundle.publication_pin_id,
        )


def request_command_digest(request: AuthorizedContextRequest) -> str:
    """Return the stable digest used to distinguish replay conflicts."""

    payload = request.model_dump(mode="json")
    return sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def context_bundle_id_for_request(request: AuthorizedContextRequest) -> UUID:
    """Return the public immutable identity for one request attempt."""

    command_digest = request_command_digest(request)
    return uuid5(
        NAMESPACE_URL,
        f"spine:context-bundle:v1:{request.workspace_id}:{request.environment_id}:"
        f"{request.run_id}:{request.attempt_id}:{request.idempotency_key}:{command_digest}",
    )


__all__ = [
    "ActivePublicationPort",
    "AuthorizedContextBrokerApplicationService",
    "AuthorizedContextRequest",
    "ContextBrokerReadModel",
    "ContextBrokerReadPort",
    "ContextBundleStore",
    "ContextBundleRevalidator",
    "InMemoryContextBundleStore",
    "PhysicalReadPlanPort",
    "SavedContextBundlePort",
    "SavedContextBundleAccess",
    "StructuredReadPort",
    "TrustedRequestContextPort",
    "context_bundle_id_for_request",
    "request_command_digest",
]
