from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import UUID

import pytest

from spine.application.diagnostics import DiagnosticContext

from spine.application.authorized_context_broker import (
    AuthorizedContextBrokerApplicationService,
    AuthorizedContextRequest,
    InMemoryContextBundleStore,
    SavedContextBundleAccess,
    _references_by_evidence_id,
    context_bundle_id_for_request,
)
from spine.application.qa_observation.context_capture import (
    AuthorizedContextCapture,
    AuthorizedContextCaptureContext,
    CaptureDisposition,
)
from spine.application.qa_observation.contracts import (
    AdmittedExecutionIdentity,
    CaptureSubscription,
)
from spine.application.context_profiles import (
    EffectiveContextRetrievalPolicy,
    ContextProfileResolution,
    ContextProfileResolutionReason,
    ContextProfileResolutionOutcome,
)
from spine.domain.context_profiles import (
    CompletenessRequirement,
    ContextProfileRef,
    ContextProfileVersion,
    PartialAnswerFallback,
    RetrievalStrategy,
)
from spine.domain.context_bundles import ContextBundleRef
from spine.domain.common import ActorKind, ActorRef
from spine.domain.capabilities import (
    AnswerQuestionInput,
    AnswerQuestionScope,
    QuestionSubject,
    RunAttemptCorrelation,
)
from spine.application.content_authorization import (
    AuthorizationContextMode,
    TrustedAuthorizationContext,
)
from datetime import datetime, timezone
from spine.memory.contracts import ContextOutcome, ContextReasonCode
from spine.domain.projections import UnpublishedActivePublication
from spine.domain.projections import (
    CanonicalAsOf,
    ProjectionReconciliationOutcome,
    ProjectionReconciliationResult,
    projection_reconciliation_findings_digest,
)
from spine.domain.sources import CanonicalBoundary
from spine.memory.contracts import (
    EvidenceReference,
    ProjectionChunk,
    ProjectionOutcome,
    ProjectionRetrievalResult,
    PublicationPin,
    RetrievalReadContext,
    StableLocator,
)
from spine.application.authorized_retrieval_scope import (
    AuthorizedRetrievalScopeOutcome,
    AuthorizedRetrievalScopeResolution,
    AuthorizedRetrievalScopeReason,
)
from spine.application.evidence_lineage_verification import VerifiedEvidenceItem
from tests.yandex_disk_contract import YANDEX_SOURCE_OBJECT_ID, YANDEX_SOURCE_REVISION_ID

WORKSPACE = UUID(int=1)
ENVIRONMENT = UUID(int=2)
NOW = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)
BITRIX24_SOURCE_OBJECT_ID = UUID(int=666)
BITRIX24_SOURCE_REVISION_ID = UUID(int=667)


def request(**overrides: object) -> AuthorizedContextRequest:
    values: dict[str, object] = {
        "request_id": UUID(int=3),
        "workspace_id": WORKSPACE,
        "environment_id": ENVIRONMENT,
        "query": "where is the contract?",
        "profile_ref": ContextProfileRef(
            profile_id=UUID(int=4), profile_version=1, profile_digest="a" * 64
        ),
        "trusted_request_context_ref": UUID(int=5),
        "authorization_context_ref": UUID(int=6),
        "run_id": UUID(int=7),
        "attempt_id": UUID(int=8),
        "idempotency_key": "attempt-1",
        "diagnostic_context": DiagnosticContext(trace_id=UUID(int=1000)),
    }
    values.update(overrides)
    return AuthorizedContextRequest(**values)


class Trusted:
    def __init__(self, value: TrustedAuthorizationContext | None = None) -> None:
        self.value = value

    async def resolve(self, request: AuthorizedContextRequest) -> None:
        del request
        return self.value


class Profiles:
    async def resolve(self, request: object) -> ContextProfileResolution:
        del request
        return ContextProfileResolution(
            outcome=ContextProfileResolutionOutcome.REJECTED,
            reason=ContextProfileResolutionReason.PROFILE_UNAVAILABLE,
        )


class ResolvedProfiles:
    def __init__(self, profile: ContextProfileVersion) -> None:
        self.profile = profile

    async def resolve(self, request: object) -> ContextProfileResolution:
        del request
        return ContextProfileResolution(
            outcome=ContextProfileResolutionOutcome.RESOLVED,
            reason=ContextProfileResolutionReason.RESOLVED,
            profile=self.profile,
            policy=EffectiveContextRetrievalPolicy(
                workspace_id=WORKSPACE,
                environment_id=ENVIRONMENT,
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


class NeverCalled:
    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"unexpected collaborator call: {name}")


class ProcessingAllowed:
    def __init__(self, eligible: bool = True) -> None:
        self.eligible = eligible

    async def resolve(self, **values: object) -> object:
        del values
        return type("Eligibility", (), {"eligible": self.eligible})()


def service(
    *,
    bundles: object | None = None,
    trusted_available: bool = True,
    profiles: object | None = None,
    publication: object | None = None,
    processing_eligible: bool = True,
) -> AuthorizedContextBrokerApplicationService:
    trusted = (
        Trusted(
            TrustedAuthorizationContext(
                mode=AuthorizationContextMode.HUMAN_DISCLOSURE,
                workspace_id=WORKSPACE,
                environment_id=ENVIRONMENT,
                canonical_human_id=UUID(int=9),
                purpose="answer_question",
                operation="retrieve_context",
                membership_generation=1,
                authority_generation=1,
                authority_snapshot_reference=UUID(int=10),
                trace_id=UUID(int=11),
                issued_at=NOW,
            )
        )
        if trusted_available
        else Trusted()
    )
    return AuthorizedContextBrokerApplicationService(
        trusted_context=trusted,
        profile_resolver=profiles or Profiles(),
        publication=publication or NeverCalled(),
        scope_resolver=NeverCalled(),
        read_model=NeverCalled(),
        plan_builder=NeverCalled(),
        retrieval=NeverCalled(),
        evidence_verifier=NeverCalled(),
        bundles=bundles or InMemoryContextBundleStore(),
        processing_authority=ProcessingAllowed(processing_eligible),
    )


@pytest.mark.asyncio
async def test_missing_trusted_context_denies_without_disclosing_profile_state() -> (
    None
):
    response = await service(trusted_available=False).retrieve(request())

    assert response.outcome is ContextOutcome.DENIED
    assert response.reason_code is ContextReasonCode.INVALID_TRUSTED_CONTEXT
    assert response.context_bundle_ref is None


@pytest.mark.asyncio
async def test_denied_request_does_not_send_a_capture_to_the_observer() -> None:
    current = request()
    capture_context = AuthorizedContextCaptureContext(
        identity=AdmittedExecutionIdentity(
            command_id=UUID(int=700),
            run_id=current.run_id,
            session_id=UUID(int=701),
            message_id=UUID(int=702),
            workspace_id=current.workspace_id,
            environment_id=current.environment_id,
            requester=ActorRef(kind=ActorKind.HUMAN, id=UUID(int=9)),
            attempt_id=current.attempt_id,
            attempt_number=1,
        ),
        subscription=CaptureSubscription(
            capture_id=UUID(int=705),
            subscription_id=UUID(int=706),
            expires_at=NOW.replace(year=2027),
        ),
        question=AnswerQuestionInput(
            subject=QuestionSubject(text=current.query),
            scope=AnswerQuestionScope(
                workspace_id=current.workspace_id,
                environment_id=current.environment_id,
            ),
            correlation=RunAttemptCorrelation(
                run_id=current.run_id,
                attempt_id=current.attempt_id,
                trace_id=UUID(int=11),
            ),
        ),
    )

    class CaptureSink:
        calls = 0

        async def capture(self, value: object) -> None:
            del value
            self.calls += 1

    sink = CaptureSink()
    result = await service(trusted_available=False).retrieve_with_capture(
        current,
        capture_context=capture_context,
        capture_sink=sink,
    )

    assert result.response.outcome is ContextOutcome.DENIED
    assert result.disposition is CaptureDisposition.NOT_ELIGIBLE
    assert sink.calls == 0

    unavailable = await service().retrieve_with_capture(
        current,
        capture_context=capture_context,
        capture_sink=sink,
    )
    assert unavailable.response.outcome is ContextOutcome.UNAVAILABLE
    assert unavailable.disposition is CaptureDisposition.NOT_ELIGIBLE
    assert sink.calls == 0

    cross_workspace = await service().retrieve_with_capture(
        request(workspace_id=UUID(int=99)),
        capture_context=capture_context,
        capture_sink=sink,
    )
    assert cross_workspace.response.outcome is ContextOutcome.DENIED
    assert cross_workspace.disposition is CaptureDisposition.INVALID_CONTEXT
    assert sink.calls == 0


@pytest.mark.asyncio
async def test_profile_resolution_failure_is_unavailable_and_does_not_read_publication() -> (
    None
):
    response = await service().retrieve(request())

    assert response.outcome is ContextOutcome.UNAVAILABLE
    assert response.reason_code is ContextReasonCode.PROFILE_UNAVAILABLE


@pytest.mark.asyncio
async def test_profile_reference_mismatch_logs_only_content_free_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:
    mismatched = ContextProfileVersion.create(
        profile_id=UUID(int=40),
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        profile_version=1,
        retrieval_strategy=RetrievalStrategy.HYBRID,
        max_results=10,
        max_tokens=1000,
        max_relation_hops=2,
        freshness_max_age_seconds=60,
        completeness_requirement=CompletenessRequirement.BEST_EFFORT,
        partial_answer_fallback=PartialAnswerFallback.DISALLOWED,
        server_approved=True,
        created_at=NOW,
    )

    response = await service(profiles=ResolvedProfiles(mismatched)).retrieve(request())

    assert response.reason_code is ContextReasonCode.PROFILE_UNAVAILABLE
    records = [
        record
        for record in caplog.records
        if record.message == "authorized context profile rejected"
    ]
    assert len(records) == 1
    assert records[0].safe_stage == "context_profile_resolution"
    assert records[0].safe_reason_code == "profile_reference_mismatch"
    assert str(mismatched.profile_id) not in caplog.text
    assert mismatched.profile_digest not in caplog.text


@pytest.mark.asyncio
async def test_bundle_store_failure_is_safe_unavailable_without_restarting_retrieval() -> (
    None
):
    class FailingStore:
        async def resolve_attempt(self, **kwargs: object) -> None:
            del kwargs
            raise RuntimeError("database details must stay private")

    response = await service(bundles=FailingStore()).retrieve(request())

    assert response.outcome is ContextOutcome.UNAVAILABLE
    assert response.reason_code is ContextReasonCode.BUNDLE_UNAVAILABLE
    assert "database" not in response.model_dump_json()


@pytest.mark.asyncio
async def test_workspace_mismatch_denies_without_leaking_other_workspace_state() -> (
    None
):
    response = await service().retrieve(request(workspace_id=UUID(int=99)))

    assert response.outcome is ContextOutcome.DENIED
    assert response.reason_code is ContextReasonCode.INVALID_TRUSTED_CONTEXT
    assert "99" not in response.model_dump_json()


@pytest.mark.asyncio
async def test_unpublished_active_projection_is_safe_unavailable() -> None:
    profile = ContextProfileVersion.create(
        profile_id=UUID(int=4),
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
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
    current = request(profile_ref=profile.reference)

    class Publication:
        async def resolve_active_publication(self, projection_kind: str) -> object:
            assert projection_kind == "qa"
            return UnpublishedActivePublication(
                workspace_id=WORKSPACE,
                environment_id=ENVIRONMENT,
                projection_kind="qa",
            )

    response = await service(
        profiles=ResolvedProfiles(profile), publication=Publication()
    ).retrieve(current)

    assert response.outcome is ContextOutcome.UNAVAILABLE
    assert response.reason_code is ContextReasonCode.PUBLICATION_UNAVAILABLE


@pytest.mark.asyncio
async def test_replay_returns_immutable_bundle_without_reinvoking_profile_or_publication() -> (
    None
):
    bundle_id = UUID(int=90)
    stored = SimpleNamespace(
        bundle_id=bundle_id,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        outcome=ContextOutcome.ALLOWED,
        reason_code=ContextReasonCode.ALLOWED.value,
        reference=ContextBundleRef(
            bundle_id=bundle_id, bundle_version=1, bundle_digest="b" * 64
        ),
        publication_pin_id=UUID(int=91),
        evidence_references=(
            SimpleNamespace(
                source_object_id=UUID(int=92),
                source_revision_id=UUID(int=93),
            ),
        ),
    )

    class ReplayStore:
        async def resolve_attempt(self, **kwargs: object) -> object:
            del kwargs
            return stored

        async def create_or_replay(self, bundle: object) -> object:
            raise AssertionError("replay must not create a bundle")

    current = request()
    expected = context_bundle_id_for_request(current)
    stored.bundle_id = expected
    response = await service(bundles=ReplayStore()).retrieve(current)

    assert response.outcome is ContextOutcome.ALLOWED
    assert response.context_bundle_ref is stored.reference
    assert response.publication_pin_id == UUID(int=91)


@pytest.mark.asyncio
async def test_replay_is_denied_when_cached_revision_is_now_opaque() -> None:
    current = request()
    bundle_id = context_bundle_id_for_request(current)
    stored = SimpleNamespace(
        bundle_id=bundle_id,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        outcome=ContextOutcome.ALLOWED,
        reason_code=ContextReasonCode.ALLOWED.value,
        reference=ContextBundleRef(
            bundle_id=bundle_id,
            bundle_version=1,
            bundle_digest="b" * 64,
        ),
        publication_pin_id=UUID(int=91),
        evidence_references=(
            SimpleNamespace(
                source_object_id=UUID(int=92),
                source_revision_id=UUID(int=93),
            ),
        ),
    )

    class ReplayStore:
        async def resolve_attempt(self, **kwargs: object) -> object:
            del kwargs
            return stored

        async def create_or_replay(self, bundle: object) -> object:
            raise AssertionError("opaque replay must not create a bundle")

    response = await service(
        bundles=ReplayStore(), processing_eligible=False
    ).retrieve(current)

    assert response.outcome is ContextOutcome.UNAVAILABLE
    assert response.reason_code is ContextReasonCode.RETRIEVAL_UNAVAILABLE
    assert response.context_bundle_ref is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source_object_id", "source_revision_id"),
    (
        (YANDEX_SOURCE_OBJECT_ID, YANDEX_SOURCE_REVISION_ID),
        (BITRIX24_SOURCE_OBJECT_ID, BITRIX24_SOURCE_REVISION_ID),
    ),
    ids=("yandex-disk", "bitrix24-disk"),
)
async def test_allowed_request_persists_exact_bundle_and_replays_without_second_read(
    caplog, source_object_id: UUID, source_revision_id: UUID
) -> None:
    from tests.memory.test_physical_read_plan import _config, _context, _publication

    physical_context, _ = _context(
        source=source_object_id,
        revision=source_revision_id,
    )
    base_publication = _publication(_config())
    findings: dict[str, object] = {}
    reconciliation = ProjectionReconciliationResult(
        reconciliation_result_id=UUID(int=100),
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        projection_kind="qa",
        projection_snapshot_id=base_publication.projection_snapshot.projection_snapshot_id,
        projection_activation_id=base_publication.projection_activation.projection_activation_id,
        activation_generation=base_publication.activation_generation,
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
    publication = base_publication.model_copy(
        update={"effective_reconciliation": reconciliation}
    )
    pin = PublicationPin.from_publication(publication)
    source = physical_context.authorized_scope.source_constraints[0]
    read_context = RetrievalReadContext(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        publication_pin_id=pin.pin_id,
        publication_pin_digest=pin.pin_digest,
        projection_snapshot_id=pin.projection_snapshot.projection_snapshot_id,
        projection_activation_id=pin.projection_activation.projection_activation_id,
        activation_generation=pin.activation_generation,
        canonical_boundary_id=pin.canonical_boundary.boundary_id,
        canonical_boundary_digest=pin.canonical_boundary.boundary_digest,
        projection_run_id=physical_context.projection_run.projection_run_id,
        projection_candidate_id=physical_context.projection_run.projection_candidate_id,
        projection_partition_id=physical_context.partitions[0].projection_partition_id,
        projection_receipt_id=physical_context.receipts[0].projection_receipt_id,
        physical_namespace="qa:active",
        physical_namespace_digest="e" * 64,
        source_object_ids=(source.source_object_id,),
        source_revision_ids=(source.source_revision_id,),
        provider_capability_reference="provider:qa",
        source_isolation_pre_retrieval_enforced=True,
    )
    evidence = EvidenceReference(
        evidence_id="evidence-1",
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        source_object_id=source.source_object_id,
        source_revision_id=source.source_revision_id,
        revision_digest="f" * 64,
        revision_number=1,
        processing_receipt_id=UUID(int=102),
        locator_contract_version="locator:v1",
        locator=StableLocator(kind="page", value="1"),
        evidence_digest="1" * 64,
    )
    item = ProjectionChunk(
        chunk_id="chunk-1",
        text="protected text",
        evidence=(evidence,),
        read_context=read_context,
        read_contexts=(read_context,),
    )
    retrieval_result = ProjectionRetrievalResult(
        outcome=ProjectionOutcome.SUCCEEDED,
        command_identity="retrieve-1",
        command_digest="2" * 64,
        chunks=(item,),
        evidence=(evidence,),
        retrieval_strategy="hybrid",
        read_context=read_context,
        read_contexts=(read_context,),
    )

    class TrustedResolved(Trusted):
        def __init__(self) -> None:
            super().__init__(
                TrustedAuthorizationContext(
                    mode=AuthorizationContextMode.HUMAN_DISCLOSURE,
                    workspace_id=WORKSPACE,
                    environment_id=ENVIRONMENT,
                    canonical_human_id=UUID(int=9),
                    purpose="answer_question",
                    operation="retrieve_context",
                    membership_generation=1,
                    authority_generation=1,
                    authority_snapshot_reference=UUID(int=10),
                    trace_id=UUID(int=11),
                    issued_at=NOW,
                )
            )

    profile = ContextProfileVersion.create(
        profile_id=UUID(int=20),
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
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

    class Scope:
        async def resolve(self, request: object) -> AuthorizedRetrievalScopeResolution:
            del request
            return AuthorizedRetrievalScopeResolution(
                outcome=AuthorizedRetrievalScopeOutcome.RESOLVED,
                reason=AuthorizedRetrievalScopeReason.RESOLVED,
                scope=physical_context.authorized_scope,
            )

    class ReadModel:
        async def load(self, **kwargs: object) -> object:
            del kwargs
            return __import__(
                "spine.application.authorized_context_broker",
                fromlist=["ContextBrokerReadModel"],
            ).ContextBrokerReadModel(
                projection_run=physical_context.projection_run,
                snapshot_members=physical_context.snapshot_members,
                partitions=physical_context.partitions,
                receipts=physical_context.receipts,
                source_references=physical_context.source_references,
                canonical_boundary=CanonicalBoundary(
                    boundary_id=publication.canonical_boundary_id,
                    workspace_id=WORKSPACE,
                    environment_id=ENVIRONMENT,
                    projection_kind="qa",
                    scope_version="r1",
                    accepted_through_generation=publication.accepted_through_generation,
                    boundary_time=publication.as_of.boundary_time,
                    manifest_digest=publication.canonical_boundary_manifest_digest,
                    idempotency_receipt_id=UUID(int=103),
                ),
                provider_capability_reference="provider:qa",
            )

    class Plans:
        async def resolve(self, context: object) -> object:
            del context
            return SimpleNamespace()

    class Retrieval:
        calls = 0
        expire_clock_after_read = False
        return_stale = False

        async def execute(self, plan: object) -> ProjectionRetrievalResult:
            del plan
            self.calls += 1
            if self.expire_clock_after_read:
                clock_now[0] = NOW.replace(hour=14)
            if self.return_stale:
                return retrieval_result.model_copy(
                    update={"outcome": ProjectionOutcome.STALE}
                )
            return retrieval_result

    class Evidence:
        async def verify(
            self,
            *,
            item: object,
            publication_pin: PublicationPin,
            authorization_context: object,
        ) -> VerifiedEvidenceItem:
            del authorization_context
            return VerifiedEvidenceItem(
                item=item,
                publication_pin_id=publication_pin.pin_id,
                authorization_decision_reference=UUID(int=31),
                decision_reference=UUID(int=32),
            )

    retrieval = Retrieval()
    clock_now = [NOW]
    current = request(profile_ref=profile.reference)
    bundles = InMemoryContextBundleStore()
    broker = AuthorizedContextBrokerApplicationService(
        trusted_context=TrustedResolved(),
        profile_resolver=ResolvedProfiles(profile),
        publication=type(
            "Publication",
            (),
            {
                "resolve_active_publication": lambda self, kind: _async_return(
                    publication
                )
            },
        )(),
        scope_resolver=Scope(),
        read_model=ReadModel(),
        plan_builder=Plans(),
        retrieval=retrieval,
        evidence_verifier=Evidence(),
        bundles=bundles,
        processing_authority=ProcessingAllowed(),
        clock=lambda: clock_now[0],
    )

    identity = AdmittedExecutionIdentity(
        command_id=UUID(int=700),
        run_id=current.run_id,
        session_id=UUID(int=701),
        message_id=UUID(int=702),
        workspace_id=current.workspace_id,
        environment_id=current.environment_id,
        requester=ActorRef(kind=ActorKind.HUMAN, id=UUID(int=9)),
        attempt_id=current.attempt_id,
        attempt_number=1,
    )
    question = AnswerQuestionInput(
        subject=QuestionSubject(text=current.query),
        scope=AnswerQuestionScope(
            workspace_id=current.workspace_id,
            environment_id=current.environment_id,
        ),
        correlation=RunAttemptCorrelation(
            run_id=current.run_id,
            attempt_id=current.attempt_id,
            trace_id=UUID(int=11),
        ),
    )
    capture_context = AuthorizedContextCaptureContext(
        identity=identity,
        subscription=CaptureSubscription(
            capture_id=UUID(int=705),
            subscription_id=UUID(int=706),
            expires_at=NOW.replace(hour=13),
        ),
        question=question,
    )

    class CaptureSink:
        values = []

        async def capture(self, value: object) -> None:
            self.values.append(value)

    sink = CaptureSink()
    captured_result = await broker.retrieve_with_capture(
        current,
        capture_context=capture_context,
        capture_sink=sink,
    )
    first = captured_result.response
    second = await broker.retrieve(current)

    assert captured_result.disposition is CaptureDisposition.CAPTURED
    assert len(sink.values) == 1
    capture = sink.values[0]
    assert capture.projection_retrieval is retrieval_result
    assert capture.retrieval.context == ("protected text",)
    assert (
        capture.retrieval.references[0].source_revision_id == source.source_revision_id
    )
    assert capture.context_bundle.reference == first.context_bundle_ref
    assert (
        capture.retrieval.references[0].source_revision_id == source.source_revision_id
    )
    assert capture.retrieval.outcome is first.outcome
    persisted_bundle = await bundles.resolve(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        reference=first.context_bundle_ref,
    )
    assert "protected text" not in first.model_dump_json()
    assert "protected text" not in persisted_bundle.model_dump_json()
    assert "protected text" not in caplog.text
    with pytest.raises(ValueError, match="does not match admitted execution"):
        AuthorizedContextCaptureContext(
            identity=identity.model_copy(update={"workspace_id": UUID(int=99)}),
            subscription=capture_context.subscription,
            question=question,
        )
    wrong_attempt = UUID(int=99)
    wrong_attempt_context = AuthorizedContextCaptureContext(
        identity=identity.model_copy(update={"attempt_id": wrong_attempt}),
        subscription=capture_context.subscription,
        question=question.model_copy(
            update={
                "correlation": question.correlation.model_copy(
                    update={"attempt_id": wrong_attempt}
                )
            }
        ),
    )
    with pytest.raises(
        ValueError, match="capture lineage does not match authorized execution"
    ):
        AuthorizedContextCapture(
            capture_context=wrong_attempt_context,
            response=first,
            projection_retrieval=retrieval_result,
            retrieval=capture.retrieval,
            context_bundle=capture.context_bundle,
            publication_pin=capture.publication_pin,
        )
    with pytest.raises(ValueError, match="capture references do not match"):
        AuthorizedContextCapture(
            capture_context=capture_context,
            response=first,
            projection_retrieval=retrieval_result.model_copy(
                update={
                    "evidence": (
                        retrieval_result.evidence[0].model_copy(
                            update={"source_revision_id": UUID(int=99)}
                        ),
                    )
                }
            ),
            retrieval=capture.retrieval,
            context_bundle=capture.context_bundle,
            publication_pin=capture.publication_pin,
        )
    assert first.outcome is ContextOutcome.ALLOWED
    assert capture.retrieval.references[0].source_revision_id == source_revision_id
    assert first.context_bundle_ref is not None
    assert second == first
    assert retrieval.calls == 1

    next_request = request(
        profile_ref=profile.reference,
        attempt_id=UUID(int=80),
        idempotency_key="attempt-2",
    )
    next_identity = identity.model_copy(update={"attempt_id": next_request.attempt_id})
    next_question = question.model_copy(
        update={
            "correlation": question.correlation.model_copy(
                update={"attempt_id": next_request.attempt_id}
            )
        }
    )
    next_capture_context = capture_context.model_copy(
        update={"identity": next_identity, "question": next_question}
    )

    class FailingCaptureSink:
        async def capture(self, value: object) -> None:
            del value
            raise RuntimeError("protected payload must not escape")

    failed_capture = await broker.retrieve_with_capture(
        next_request,
        capture_context=next_capture_context,
        capture_sink=FailingCaptureSink(),
    )
    assert failed_capture.response.outcome is ContextOutcome.ALLOWED
    assert failed_capture.disposition is CaptureDisposition.SINK_FAILED
    assert retrieval.calls == 2

    hanging_request = request(
        profile_ref=profile.reference,
        attempt_id=UUID(int=82),
        idempotency_key="attempt-3",
    )
    hanging_capture_context = capture_context.model_copy(
        update={
            "identity": identity.model_copy(
                update={"attempt_id": hanging_request.attempt_id}
            ),
            "question": question.model_copy(
                update={
                    "correlation": question.correlation.model_copy(
                        update={"attempt_id": hanging_request.attempt_id}
                    )
                }
            ),
        }
    )

    class HangingCaptureSink:
        async def capture(self, value: object) -> None:
            del value
            await asyncio.Event().wait()

    timed_out_capture = await broker.retrieve_with_capture(
        hanging_request,
        capture_context=hanging_capture_context,
        capture_sink=HangingCaptureSink(),
        capture_timeout_seconds=0.01,
    )
    assert timed_out_capture.response.outcome is ContextOutcome.ALLOWED
    assert timed_out_capture.disposition is CaptureDisposition.SINK_TIMEOUT
    assert retrieval.calls == 3

    stale_request = request(
        profile_ref=profile.reference,
        attempt_id=UUID(int=83),
        idempotency_key="attempt-stale",
    )
    stale_context = capture_context.model_copy(
        update={
            "identity": identity.model_copy(
                update={"attempt_id": stale_request.attempt_id}
            ),
            "question": question.model_copy(
                update={
                    "correlation": question.correlation.model_copy(
                        update={"attempt_id": stale_request.attempt_id}
                    )
                }
            ),
        }
    )
    retrieval.return_stale = True
    stale_capture = await broker.retrieve_with_capture(
        stale_request,
        capture_context=stale_context,
        capture_sink=sink,
    )
    retrieval.return_stale = False
    assert stale_capture.response.outcome is ContextOutcome.INSUFFICIENT
    assert stale_capture.disposition is CaptureDisposition.NOT_ELIGIBLE
    assert len(sink.values) == 1
    assert retrieval.calls == 4

    last_request = request(
        profile_ref=profile.reference,
        attempt_id=UUID(int=81),
        idempotency_key="attempt-5",
    )
    last_capture_context = capture_context.model_copy(
        update={
            "identity": identity.model_copy(
                update={"attempt_id": last_request.attempt_id}
            ),
            "question": question.model_copy(
                update={
                    "correlation": question.correlation.model_copy(
                        update={"attempt_id": last_request.attempt_id}
                    )
                }
            ),
        }
    )
    retrieval.expire_clock_after_read = True
    expired_capture = await broker.retrieve_with_capture(
        last_request,
        capture_context=last_capture_context,
        capture_sink=sink,
    )
    assert expired_capture.response.outcome is ContextOutcome.ALLOWED
    assert expired_capture.disposition is CaptureDisposition.EXPIRED
    assert len(sink.values) == 1
    assert retrieval.calls == 5
    with pytest.raises(ValueError, match="unavailable"):
        await bundles.resolve(
            workspace_id=UUID(int=99),
            environment_id=ENVIRONMENT,
            reference=first.context_bundle_ref,
        )


def test_bundle_lineage_indexes_canonical_evidence_not_physical_object_id() -> None:
    from tests.memory.test_physical_read_plan import _context

    physical_context, _ = _context()
    reference = physical_context.source_references[0].model_copy(
        update={
            "segment_id": "segment-canonical",
            "projected_object_id": "chunk-physical",
        }
    )

    indexed = _references_by_evidence_id((reference,))

    assert set(indexed) == {"segment-canonical"}
    assert indexed["segment-canonical"].projected_object_id == "chunk-physical"


@pytest.mark.asyncio
async def test_saved_bundle_access_keeps_load_and_revalidation_as_typed_ports() -> None:
    class Store:
        async def resolve(self, **kwargs: object) -> object:
            assert kwargs["workspace_id"] == WORKSPACE
            assert kwargs["environment_id"] == ENVIRONMENT
            return "bundle"

    class Revalidator:
        async def revalidate(self, request: object) -> object:
            assert request == "checkpoint"
            return "revalidated"

    access = SavedContextBundleAccess(store=Store(), revalidator=Revalidator())
    reference = ContextBundleRef(
        bundle_id=UUID(int=200), bundle_version=1, bundle_digest="a" * 64
    )

    assert (
        await access.load(
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            reference=reference,
        )
        == "bundle"
    )
    assert await access.revalidate("checkpoint") == "revalidated"


async def _async_return(value: object) -> object:
    return value
