"""Shared parity checks for the active context adapter seams.

The adapters intentionally have different physical representations.  These
tests compare only the provider-neutral contract outcome and disclosure
invariants; identity values and item ordering are compared only where the
contract makes them meaningful.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
from types import SimpleNamespace
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from spine.application.diagnostics import DiagnosticContext
from spine.application.testing import DeterministicCurrentProcessingDispositionAuthority

from spine.application.authorized_context_broker import (
    AuthorizedContextBrokerApplicationService,
    AuthorizedContextRequest,
    ContextBrokerReadModel,
    InMemoryContextBundleStore,
)
from spine.application.context_profiles import (
    ContextProfileResolution,
    ContextProfileResolutionOutcome,
    EffectiveContextRetrievalPolicy,
)
from spine.application.content_authorization import (
    AuthorizationContextMode,
    TrustedAuthorizationContext,
)
from spine.application.authorized_retrieval_scope import (
    AuthorizedRetrievalScopeOutcome,
    AuthorizedRetrievalScopeReason,
    AuthorizedRetrievalScopeResolution,
)
from spine.application.evidence_lineage_verification import VerifiedEvidenceItem
from spine.domain.context_profiles import (
    CompletenessRequirement,
    ContextProfileVersion,
    PartialAnswerFallback,
    RetrievalStrategy,
)
from spine.domain.projections import (
    ProjectionReconciliationOutcome,
    ProjectionReconciliationResult,
    projection_reconciliation_findings_digest,
    PublishedActivePublication,
    UnpublishedActivePublication,
)
from spine.domain.sources import CanonicalBoundary as DomainCanonicalBoundary
from spine.memory.broker import (
    ContextBrokerV1,
    DeterministicFakePublicationResolver,
    ExplicitFakeContextBundleIssuer,
    FakeContextProfileResolver,
    FakeContextRetrieval,
    FakeTrustedContextValidator,
    LegacyContextBrokerAdapter,
)
from spine.memory.contracts import (
    ContextOutcome,
    ContextProfile,
    ContextProfileRef,
    ContextReasonCode,
    ContextRequest,
    EvidenceReference,
    ProjectionChunk,
    ProjectionFailure,
    ProjectionFailureCode,
    ProjectionDiagnostic,
    ProjectionOutcome,
    ProjectionRetrievalResult,
    ProjectionNamespaceState,
    RetrievalScore,
    StableLocator,
    RetrievalQuery,
)
from spine.memory.cognee.context_projection import CogneeContextProjection
from spine.memory.fake import DeterministicContextProjection, DeterministicPhysicalStore

from .test_conformance import (
    ENVIRONMENT_ID as PROJECTION_ENVIRONMENT_ID,
    WORKSPACE_ID as PROJECTION_WORKSPACE_ID,
    command_context,
    evidence,
    retrieve_command,
)


NOW = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)
# The authorized application fixtures intentionally use the compact IDs from
# the application contract tests.  Projection fixtures use the public contract
# IDs imported above and remain independent from this broker matrix.
WORKSPACE_ID = UUID(int=1)
ENVIRONMENT_ID = UUID(int=2)
REQUEST_ID = UUID("60000000-0000-4000-8000-000000000001")
PROFILE_ID = UUID("50000000-0000-4000-8000-000000000001")
TRUSTED_REF = UUID("30000000-0000-4000-8000-000000000001")
AUTHORIZATION_REF = UUID("40000000-0000-4000-8000-000000000001")


class BrokerScenario(str, Enum):
    ALLOWED = "allowed"
    DENIED = "denied"
    STALE = "stale"
    TOMBSTONED = "tombstoned"
    PARTIAL = "partial"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class NormalizedParityResult:
    adapter: str
    contract_version: str
    outcome: str
    reason: str
    bundle_present: bool
    publication_pin_present: bool
    safe: bool


def _expected_context_result(scenario: BrokerScenario) -> tuple[ContextOutcome, ContextReasonCode]:
    return {
        BrokerScenario.ALLOWED: (ContextOutcome.ALLOWED, ContextReasonCode.ALLOWED),
        BrokerScenario.DENIED: (
            ContextOutcome.DENIED,
            ContextReasonCode.INVALID_TRUSTED_CONTEXT,
        ),
        BrokerScenario.STALE: (ContextOutcome.STALE, ContextReasonCode.STALE_CONTEXT),
        # Context Broker v1 has no public tombstone outcome.  A tombstoned
        # revision is therefore normalized to insufficient evidence.
        BrokerScenario.TOMBSTONED: (
            ContextOutcome.INSUFFICIENT,
            ContextReasonCode.INSUFFICIENT_EVIDENCE,
        ),
        BrokerScenario.PARTIAL: (
            ContextOutcome.PARTIAL,
            ContextReasonCode.PARTIAL_CONTEXT,
        ),
        BrokerScenario.DEGRADED: (
            ContextOutcome.DEGRADED,
            ContextReasonCode.DEGRADED_CONTEXT,
        ),
        BrokerScenario.UNAVAILABLE: (
            ContextOutcome.UNAVAILABLE,
            ContextReasonCode.PUBLICATION_UNAVAILABLE,
        ),
        BrokerScenario.CANCELLED: (ContextOutcome.CANCELLED, ContextReasonCode.CANCELLED),
    }[scenario]


def _context_request() -> ContextRequest:
    profile_ref = ContextProfileRef(
        profile_id=PROFILE_ID,
        profile_version=1,
        profile_digest="a" * 64,
    )
    return ContextRequest(
        request_id=REQUEST_ID,
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        query="where is the contract?",
        profile_ref=profile_ref,
        trusted_request_context_ref=TRUSTED_REF,
        authorization_context_ref=AUTHORIZATION_REF,
        limit=5,
    )


def _publication_for_scenario(scenario: BrokerScenario) -> PublishedActivePublication | UnpublishedActivePublication:
    publication = _publication()
    if scenario is BrokerScenario.STALE:
        publication = publication.model_copy(update={"freshness": "stale"})
    elif scenario is BrokerScenario.DEGRADED:
        publication = publication.model_copy(update={"health": "degraded"})
    elif scenario is BrokerScenario.UNAVAILABLE:
        publication = UnpublishedActivePublication(
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
            projection_kind="qa",
        )

    return publication


def _retrieval_outcome_for_scenario(scenario: BrokerScenario) -> ContextOutcome:
    return {
        BrokerScenario.ALLOWED: ContextOutcome.ALLOWED,
        BrokerScenario.PARTIAL: ContextOutcome.PARTIAL,
        BrokerScenario.TOMBSTONED: ContextOutcome.INSUFFICIENT,
        BrokerScenario.UNAVAILABLE: ContextOutcome.UNAVAILABLE,
        BrokerScenario.CANCELLED: ContextOutcome.CANCELLED,
    }.get(scenario, ContextOutcome.ALLOWED)


def _build_context_broker(
    scenario: BrokerScenario,
    profile: ContextProfile,
    *,
    retrieval_error: Exception | None = None,
) -> ContextBrokerV1:
    return ContextBrokerV1(
        trusted_context_validator=FakeTrustedContextValidator(
            allowed=scenario is not BrokerScenario.DENIED,
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
        ),
        profile_resolver=FakeContextProfileResolver({profile.ref: profile}),
        retrieval=FakeContextRetrieval(
            outcome=_retrieval_outcome_for_scenario(scenario),
            error=retrieval_error,
        ),
        bundle_issuer=ExplicitFakeContextBundleIssuer(),
        publication_resolver=DeterministicFakePublicationResolver(
            _publication_for_scenario(scenario)
        ),
    )


def _legacy_broker(
    scenario: BrokerScenario,
    *,
    retrieval_error: Exception | None = None,
) -> ContextBrokerV1:
    request = _context_request()
    profile = ContextProfile(
        ref=request.profile_ref,
        retrieval_strategy="hybrid",
        require_current_publication=scenario is BrokerScenario.STALE,
        require_healthy_publication=scenario is BrokerScenario.DEGRADED,
    )
    return _build_context_broker(
        scenario,
        profile,
        retrieval_error=retrieval_error,
    )


def _legacy_compatibility_broker(scenario: BrokerScenario) -> ContextBrokerV1:
    """Build the active legacy surface with its own profile-name mapping."""

    profile_name = "profile"
    profile = ContextProfile(
        ref=ContextProfileRef(
            profile_id=uuid5(NAMESPACE_URL, f"spine:legacy-profile:{profile_name}"),
            profile_version=1,
            profile_digest=sha256(profile_name.encode()).hexdigest(),
        ),
        retrieval_strategy="hybrid",
        require_current_publication=scenario is BrokerScenario.STALE,
        require_healthy_publication=scenario is BrokerScenario.DEGRADED,
    )
    return _build_context_broker(
        scenario,
        profile,
    )


class _TrustedContext:
    def __init__(self, scenario: BrokerScenario) -> None:
        self.scenario = scenario

    async def resolve(self, request: AuthorizedContextRequest) -> TrustedAuthorizationContext | None:
        if self.scenario is BrokerScenario.DENIED:
            return None
        return TrustedAuthorizationContext(
            mode=AuthorizationContextMode.HUMAN_DISCLOSURE,
            workspace_id=request.workspace_id,
            environment_id=request.environment_id,
            canonical_human_id=UUID(int=9),
            purpose="answer_question",
            operation="retrieve_context",
            membership_generation=1,
            authority_generation=1,
            authority_snapshot_reference=UUID(int=10),
            trace_id=UUID(int=11),
            issued_at=NOW,
        )


class _ProfileResolver:
    def __init__(self, profile: ContextProfileVersion, available: bool = True) -> None:
        self.profile = profile
        self.available = available

    async def resolve(self, request: object) -> ContextProfileResolution:
        del request
        if not self.available:
            return ContextProfileResolution(
                outcome=ContextProfileResolutionOutcome.REJECTED,
                reason="profile_unavailable",
            )
        return ContextProfileResolution(
            outcome=ContextProfileResolutionOutcome.RESOLVED,
            reason="resolved",
            profile=self.profile,
            policy=EffectiveContextRetrievalPolicy(
                workspace_id=WORKSPACE_ID,
                environment_id=ENVIRONMENT_ID,
                profile_ref=self.profile.reference,
                retrieval_strategy=RetrievalStrategy.HYBRID,
                max_results=10,
                max_tokens=1000,
                max_relation_hops=2,
                freshness_max_age_seconds=60,
                completeness_requirement=CompletenessRequirement.BEST_EFFORT,
                partial_answer_fallback=PartialAnswerFallback.ALLOWED,
            ),
        )


class _Publication:
    def __init__(self, value: object) -> None:
        self.value = value
        self.calls = 0

    async def resolve_active_publication(self, projection_kind: str) -> object:
        assert projection_kind == "qa"
        self.calls += 1
        return self.value


class _Scope:
    def __init__(self, context: object, scenario: BrokerScenario) -> None:
        self.context = context
        self.scenario = scenario
        self.calls = 0

    async def resolve(self, request: object) -> AuthorizedRetrievalScopeResolution:
        del request
        self.calls += 1
        if self.scenario is BrokerScenario.UNAVAILABLE:
            return AuthorizedRetrievalScopeResolution(
                outcome=AuthorizedRetrievalScopeOutcome.UNAVAILABLE,
                reason=AuthorizedRetrievalScopeReason.AUTHORIZATION_UNAVAILABLE,
            )
        return AuthorizedRetrievalScopeResolution(
            outcome=AuthorizedRetrievalScopeOutcome.RESOLVED,
            reason=AuthorizedRetrievalScopeReason.RESOLVED,
            scope=self.context.authorized_scope,
        )


class _ReadModel:
    def __init__(self, context: object, publication: PublishedActivePublication) -> None:
        self.context = context
        self.publication = publication
        self.calls = 0

    async def load(self, **kwargs: object) -> ContextBrokerReadModel:
        del kwargs
        self.calls += 1
        return ContextBrokerReadModel(
            projection_run=self.context.projection_run,
            snapshot_members=self.context.snapshot_members,
            partitions=self.context.partitions,
            receipts=self.context.receipts,
            source_references=self.context.source_references,
            canonical_boundary=DomainCanonicalBoundary(
                boundary_id=self.publication.canonical_boundary_id,
                workspace_id=WORKSPACE_ID,
                environment_id=ENVIRONMENT_ID,
                projection_kind="qa",
                scope_version="r1",
                accepted_through_generation=self.publication.accepted_through_generation,
                boundary_time=self.publication.as_of.boundary_time,
                manifest_digest=self.publication.canonical_boundary_manifest_digest,
                idempotency_receipt_id=UUID(int=103),
            ),
            provider_capability_reference="provider:qa",
        )


class _Plan:
    def __init__(self) -> None:
        self.calls = 0

    async def resolve(self, context: object) -> object:
        del context
        self.calls += 1
        return SimpleNamespace()


class _Retrieval:
    def __init__(
        self,
        result: ProjectionRetrievalResult,
        error: Exception | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.calls = 0

    async def execute(self, plan: object) -> ProjectionRetrievalResult:
        del plan
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


class _Evidence:
    async def verify(
        self,
        *,
        item: object,
        publication_pin: object,
        authorization_context: object,
    ) -> VerifiedEvidenceItem:
        del authorization_context
        return VerifiedEvidenceItem(
            item=item,
            publication_pin_id=publication_pin.pin_id,
            authorization_decision_reference=UUID(int=31),
            decision_reference=UUID(int=31),
        )


def _publication() -> PublishedActivePublication:
    # Reuse the already-qualified deterministic publication fixture.  Keeping
    # this seam fake-backed means the parity suite never needs provider state.
    from tests.memory.test_physical_read_plan import _config, _publication as make_publication

    base = make_publication(_config())
    findings: dict[str, object] = {}
    reconciliation = ProjectionReconciliationResult(
        reconciliation_result_id=UUID(int=100),
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        projection_kind="qa",
        projection_snapshot_id=base.projection_snapshot.projection_snapshot_id,
        projection_activation_id=base.projection_activation.projection_activation_id,
        activation_generation=base.activation_generation,
        attempt_identity="reconcile-1",
        attempt_digest="c" * 64,
        outcome=ProjectionReconciliationOutcome.HEALTHY,
        findings_schema_version="reconciliation-findings:v1",
        findings_payload=findings,
        findings_digest=projection_reconciliation_findings_digest(
            findings_schema_version="reconciliation-findings:v1",
            findings_payload=findings,
        ),
        artifact_evidence_reference="artifact:reconcile-1",
        artifact_evidence_digest="d" * 64,
        idempotency_receipt_id=UUID(int=101),
        audit_event_reference="audit:reconcile-1",
        checked_at=NOW,
    )
    return base.model_copy(update={"effective_reconciliation": reconciliation})


def _authorized_request(profile: ContextProfileVersion, scenario: BrokerScenario) -> AuthorizedContextRequest:
    return AuthorizedContextRequest(
        request_id=REQUEST_ID,
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        query="where is the contract?",
        profile_ref=profile.reference,
        trusted_request_context_ref=TRUSTED_REF,
        authorization_context_ref=AUTHORIZATION_REF,
        limit=5,
        run_id=UUID("70000000-0000-4000-8000-000000000001"),
        attempt_id=UUID("80000000-0000-4000-8000-000000000001"),
        idempotency_key="parity-attempt",
        diagnostic_context=DiagnosticContext(trace_id=UUID(int=1000)),
        cancelled=scenario is BrokerScenario.CANCELLED,
    )


def _service_retrieval_result(scenario: BrokerScenario, context: object) -> ProjectionRetrievalResult:
    outcome = {
        BrokerScenario.ALLOWED: ProjectionOutcome.SUCCEEDED,
        BrokerScenario.PARTIAL: ProjectionOutcome.PARTIAL,
        BrokerScenario.TOMBSTONED: ProjectionOutcome.TOMBSTONED,
        BrokerScenario.UNAVAILABLE: ProjectionOutcome.FAILED,
    }.get(scenario, ProjectionOutcome.SUCCEEDED)
    if outcome in {ProjectionOutcome.SUCCEEDED, ProjectionOutcome.PARTIAL}:
        reference = EvidenceReference(
            evidence_id="parity-evidence",
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
            source_object_id=context.authorized_scope.source_constraints[0].source_object_id,
            source_revision_id=context.authorized_scope.source_constraints[0].source_revision_id,
            revision_digest="f" * 64,
            revision_number=1,
            processing_receipt_id=UUID(int=102),
            locator_contract_version="locator:v1",
            locator=StableLocator(kind="page", value="1"),
            evidence_digest="1" * 64,
        )
        item = ProjectionChunk(
            chunk_id="parity-chunk",
            text="protected parity text",
            evidence=(reference,),
        )
        return ProjectionRetrievalResult(
            outcome=outcome,
            command_identity="parity-retrieve",
            command_digest="2" * 64,
            chunks=(item,),
            evidence=(reference,),
            retrieval_strategy="hybrid",
        )
    return ProjectionRetrievalResult(
        outcome=outcome,
        command_identity="parity-retrieve",
        command_digest="2" * 64,
        retrieval_strategy="hybrid",
                failure=(
            ProjectionFailure(
                code=ProjectionFailureCode.PHYSICAL_FAILURE,
                retryable=True,
                safe_detail=ProjectionDiagnostic(),
            )
            if outcome is ProjectionOutcome.FAILED
            else None
        ),
    )


def _authorized_broker(
    scenario: BrokerScenario,
    *,
    retrieval_error: Exception | None = None,
) -> tuple[AuthorizedContextBrokerApplicationService, object, _ReadModel, _Retrieval]:
    profile = ContextProfileVersion.create(
        profile_id=PROFILE_ID,
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        profile_version=1,
        retrieval_strategy=RetrievalStrategy.HYBRID,
        max_results=10,
        max_tokens=1000,
        max_relation_hops=2,
        freshness_max_age_seconds=60,
        completeness_requirement=CompletenessRequirement.BEST_EFFORT,
        partial_answer_fallback=PartialAnswerFallback.ALLOWED,
        server_approved=True,
        created_at=NOW,
    )
    publication: object = _publication()
    if scenario is BrokerScenario.STALE:
        publication = publication.model_copy(update={"freshness": "stale"})
    elif scenario is BrokerScenario.DEGRADED:
        publication = publication.model_copy(update={"health": "degraded"})
    elif scenario is BrokerScenario.UNAVAILABLE:
        publication = UnpublishedActivePublication(
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
            projection_kind="qa",
        )
    context, _ = __import__("tests.memory.test_physical_read_plan", fromlist=["_context"])._context()
    read_model = _ReadModel(context, _publication())
    retrieval = _Retrieval(
        _service_retrieval_result(scenario, context),
        error=retrieval_error,
    )
    publication_port = _Publication(publication)
    service = AuthorizedContextBrokerApplicationService(
        trusted_context=_TrustedContext(scenario),
        profile_resolver=_ProfileResolver(
            profile,
            available=True,
        ),
        publication=publication_port,
        scope_resolver=_Scope(context, scenario),
        read_model=read_model,
        plan_builder=_Plan(),
        retrieval=retrieval,
        evidence_verifier=_Evidence(),
        bundles=InMemoryContextBundleStore(),
        processing_authority=DeterministicCurrentProcessingDispositionAuthority(),
        clock=lambda: NOW,
    )
    return service, _authorized_request(profile, scenario), read_model, retrieval


async def _run_broker(scenario: BrokerScenario, adapter: str) -> NormalizedParityResult:
    if adapter == "context-broker-v1":
        broker = _legacy_broker(scenario)
        response = await broker.retrieve(_context_request())
        contract_version = "context-broker:v1"
    else:
        broker, request, _, _ = _authorized_broker(scenario)
        response = await broker.retrieve(request)
        contract_version = "authorized-context-broker:v1"
    safe = "protected parity text" not in response.model_dump_json()
    return NormalizedParityResult(
        adapter=adapter,
        contract_version=contract_version,
        outcome=response.outcome.value,
        reason=response.reason_code.value,
        bundle_present=response.context_bundle_ref is not None,
        publication_pin_present=response.publication_pin_id is not None,
        safe=safe,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", tuple(BrokerScenario))
async def test_active_context_brokers_have_equivalent_normalized_outcomes(
    scenario: BrokerScenario,
) -> None:
    results = await asyncio.gather(
        _run_broker(scenario, "context-broker-v1"),
        _run_broker(scenario, "authorized-context-broker"),
    )
    expected_outcome, expected_reason = _expected_context_result(scenario)

    assert all(result.outcome == expected_outcome.value for result in results), results
    assert all(result.reason == expected_reason.value for result in results), results
    assert all(result.safe for result in results), results
    assert all(result.adapter and result.contract_version for result in results)
    assert results[0].bundle_present == results[1].bundle_present
    assert results[0].publication_pin_present == results[1].publication_pin_present
    assert results[0].bundle_present is (scenario in {BrokerScenario.ALLOWED, BrokerScenario.PARTIAL})
    assert results[0].publication_pin_present is (
        scenario not in {BrokerScenario.DENIED, BrokerScenario.UNAVAILABLE}
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", tuple(BrokerScenario))
async def test_active_legacy_broker_adapter_preserves_normalized_outcomes(
    scenario: BrokerScenario,
) -> None:
    adapter = LegacyContextBrokerAdapter(_legacy_compatibility_broker(scenario))
    result = await adapter.retrieve(
        RetrievalQuery(
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
            query="where is the contract?",
            requested_by={"kind": "human", "id": TRUSTED_REF},
            context_profile="profile",
        )
    )
    expected_outcome, _ = _expected_context_result(scenario)

    assert result.outcome is expected_outcome
    assert result.context == ()
    assert result.entities == ()
    assert result.relations == ()
    assert "protected" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_authorized_broker_does_not_call_physical_read_after_denial() -> None:
    broker, request, read_model, retrieval = _authorized_broker(BrokerScenario.DENIED)

    response = await broker.retrieve(request)

    assert response.outcome is ContextOutcome.DENIED
    assert read_model.calls == 0
    assert retrieval.calls == 0


@pytest.mark.asyncio
async def test_broker_adapters_map_provider_failures_without_diagnostics() -> None:
    legacy = _legacy_broker(
        BrokerScenario.ALLOWED,
        retrieval_error=RuntimeError("provider secret must not escape"),
    )
    authorized, request, _, _ = _authorized_broker(
        BrokerScenario.ALLOWED,
        retrieval_error=RuntimeError("provider secret must not escape"),
    )

    legacy_response, authorized_response = await asyncio.gather(
        legacy.retrieve(_context_request()),
        authorized.retrieve(request),
    )

    for response in (legacy_response, authorized_response):
        assert response.outcome is ContextOutcome.UNAVAILABLE
        assert response.reason_code is ContextReasonCode.RETRIEVAL_UNAVAILABLE
        assert "provider secret" not in response.model_dump_json()


@pytest.mark.asyncio
async def test_broker_parity_keeps_partial_distinct_from_complete() -> None:
    complete = await _run_broker(BrokerScenario.ALLOWED, "authorized-context-broker")
    partial = await _run_broker(BrokerScenario.PARTIAL, "authorized-context-broker")

    assert complete.outcome == ContextOutcome.ALLOWED.value
    assert partial.outcome == ContextOutcome.PARTIAL.value
    assert complete.outcome != partial.outcome


def _active_projection_adapters() -> tuple[tuple[str, object, object], ...]:
    deterministic = DeterministicContextProjection()
    composed_retrieval = DeterministicContextProjection()
    composed = CogneeContextProjection(
        effect_store=DeterministicPhysicalStore(),
        graph_effect_store=DeterministicPhysicalStore(),
        retrieval=composed_retrieval,
    )
    return (
        ("deterministic-context-projection", deterministic, deterministic),
        ("cognee-context-projection-composition", composed, composed_retrieval),
    )


@pytest.mark.parametrize("scenario", ("missing", "stale", "tombstoned", "shadow"))
@pytest.mark.asyncio
async def test_active_projection_adapters_normalize_retrieval_failures(scenario: str) -> None:
    observations: list[NormalizedParityResult] = []
    for adapter_name, projection, retrieval in _active_projection_adapters():
        if scenario == "missing":
            result = await projection.retrieve(
                retrieve_command(identity=f"parity-{scenario}", digest="a" * 64)
            )
        elif scenario == "shadow":
            namespace = command_context(state=ProjectionNamespaceState.SHADOW)["physical_namespace"]
            result = await projection.retrieve(
                retrieve_command(
                    identity=f"parity-{scenario}",
                    digest="b" * 64,
                    namespace=namespace,
                    state=ProjectionNamespaceState.SHADOW,
                )
            )
        else:
            status = "tombstoned" if scenario == "tombstoned" else "stale"
            retrieval.seed_retrieval(
                workspace_id=PROJECTION_WORKSPACE_ID,
                environment_id=PROJECTION_ENVIRONMENT_ID,
                chunks=(
                    ProjectionChunk(
                        chunk_id=f"parity-{scenario}",
                        text="protected projection text",
                        evidence=(evidence(status=status),),
                    ),
                ),
            )
            result = await projection.retrieve(
                retrieve_command(identity=f"parity-{scenario}", digest="c" * 64)
            )
        observations.append(
            NormalizedParityResult(
                adapter=adapter_name,
                contract_version="context-projection:v1",
                outcome=result.outcome.value,
                reason=result.failure.code.value if result.failure else "succeeded",
                bundle_present=False,
                publication_pin_present=bool(result.read_context),
                safe="protected projection text" not in result.model_dump_json(),
            )
        )
    assert {observation.outcome for observation in observations} == {
        {"missing": "missing", "stale": "stale", "tombstoned": "tombstoned", "shadow": "failed"}[scenario]
    }
    assert all(observation.safe for observation in observations)
    assert all(observation.contract_version == "context-projection:v1" for observation in observations)


@pytest.mark.asyncio
async def test_active_projection_adapters_preserve_score_presence_and_provenance() -> None:
    observations: list[tuple[str, bool, object, object]] = []
    for adapter_name, _, _ in _active_projection_adapters():
        for score in (
            None,
            RetrievalScore(
                value=0.75,
                metric="cosine",
                direction="higher_is_better",
                provider_contract_version=f"{adapter_name}:v1",
            ),
        ):
            score_suffix = "present" if score is not None else "absent"
            _, projection, retrieval = next(
                adapter
                for adapter in _active_projection_adapters()
                if adapter[0] == adapter_name
            )
            reference = evidence()
            chunk = ProjectionChunk(
                chunk_id=f"score-{adapter_name}-{score is not None}",
                text="grounded projection text",
                evidence=(reference,),
                score=score,
            )
            retrieval.seed_retrieval(
                workspace_id=PROJECTION_WORKSPACE_ID,
                environment_id=PROJECTION_ENVIRONMENT_ID,
                chunks=(chunk,),
            )
            result = await projection.retrieve(
                retrieve_command(
                    identity=f"score-parity-{adapter_name}-{score_suffix}",
                    digest=("d" if score is None else "e") * 64,
                )
            )
            observations.append(
                (
                    adapter_name,
                    result.chunks[0].score is not None,
                    result.chunks[0].evidence[0].source_revision_id,
                    result.chunks[0].evidence[0].locator,
                )
            )
    assert observations == [
        (adapter_name, has_score, evidence().source_revision_id, evidence().locator)
        for adapter_name in (
            "deterministic-context-projection",
            "cognee-context-projection-composition",
        )
        for has_score in (False, True)
    ]
