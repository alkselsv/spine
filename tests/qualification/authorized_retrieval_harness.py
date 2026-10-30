"""Authorized retrieval composition for issue #677 qualification."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from uuid import UUID

from spine.application.authorized_context_broker import (
    AuthorizedContextBrokerApplicationService,
    AuthorizedContextRequest,
    ContextBrokerReadModel,
    InMemoryContextBundleStore,
)
from spine.application.authorized_retrieval_scope import (
    AuthorizedRetrievalScopeOutcome,
    AuthorizedRetrievalScopeReason,
    AuthorizedRetrievalScopeResolution,
)
from spine.application.content_authorization import (
    AuthorizationContextMode,
    TrustedAuthorizationContext,
)
from spine.application.context_profiles import (
    ContextProfileResolution,
    ContextProfileResolutionOutcome,
    ContextProfileResolutionReason,
    EffectiveContextRetrievalPolicy,
)
from spine.application.diagnostics import DiagnosticContext
from spine.application.evidence_lineage_verification import VerifiedEvidenceItem
from spine.domain.context_profiles import (
    CompletenessRequirement,
    ContextProfileVersion,
    PartialAnswerFallback,
    RetrievalStrategy,
)
from spine.domain.ingestion import ProcessingReceipt
from spine.domain.projections import (
    ProjectionReconciliationOutcome,
    ProjectionReconciliationResult,
    projection_reconciliation_findings_digest,
)
from spine.domain.sources import (
    CanonicalBoundary,
    SourceRevision,
    SourceRevisionProvenance,
)
from spine.memory.contracts import (
    ContextOutcome,
    EvidenceReference,
    ProjectionChunk,
    ProjectionOutcome,
    ProjectionRetrievalResult,
    PublicationPin,
    RetrievalReadContext,
    StableLocator,
)
from tests.qualification.provider_adapter_harness import (
    ENVIRONMENT_ID,
    NOW,
    WORKSPACE_ID,
    stable_id,
)
from tests.qualification.retrieval_fixture import (
    qualification_physical_context,
    qualification_projection_config,
    qualification_publication,
)


async def retrieve_canonical_revision(
    *,
    revision: SourceRevision,
    provenance: SourceRevisionProvenance,
    processing_receipt: ProcessingReceipt,
) -> tuple[object, ProjectionRetrievalResult]:
    physical_context = qualification_physical_context(
        source=revision.source_object_id,
        revision=revision.revision_id,
    )
    base_publication = qualification_publication(qualification_projection_config())
    findings: dict[str, object] = {}
    reconciliation = ProjectionReconciliationResult(
        reconciliation_result_id=stable_id("reconciliation"),
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        projection_kind="qa",
        projection_snapshot_id=base_publication.projection_snapshot.projection_snapshot_id,
        projection_activation_id=base_publication.projection_activation.projection_activation_id,
        activation_generation=base_publication.activation_generation,
        attempt_identity="issue677-reconciliation",
        attempt_digest="c" * 64,
        outcome=ProjectionReconciliationOutcome.HEALTHY,
        findings_schema_version="reconciliation-findings:v1",
        findings_payload=findings,
        findings_digest=projection_reconciliation_findings_digest(
            findings_schema_version="reconciliation-findings:v1",
            findings_payload=findings,
        ),
        artifact_evidence_reference="artifact:issue677",
        artifact_evidence_digest="d" * 64,
        idempotency_receipt_id=stable_id("reconciliation-receipt"),
        audit_event_reference="audit:issue677",
        checked_at=NOW,
    )
    publication = base_publication.model_copy(
        update={"effective_reconciliation": reconciliation}
    )
    pin = PublicationPin.from_publication(publication)
    source = physical_context.authorized_scope.source_constraints[0]
    read_context = RetrievalReadContext(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
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
        evidence_id="issue677-evidence",
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        source_object_id=source.source_object_id,
        source_revision_id=source.source_revision_id,
        revision_digest=revision.revision_digest,
        revision_number=1,
        processing_receipt_id=processing_receipt.processing_receipt_id,
        locator_contract_version=(
            provenance.origin_locator_schema
            or processing_receipt.locator_contract_version
        ),
        locator=StableLocator(
            kind="custom",
            value=(
                f"{provenance.origin_locator_kind}:"
                f"{provenance.origin_locator_value}"
                if provenance.origin_locator_kind and provenance.origin_locator_value
                else f"source-object:{revision.source_object_id}"
            ),
        ),
        evidence_digest=hashlib.sha256(
            f"{revision.revision_digest}:{provenance.provenance_id}".encode()
        ).hexdigest(),
    )
    retrieval_result = ProjectionRetrievalResult(
        outcome=ProjectionOutcome.SUCCEEDED,
        command_identity="issue677-retrieve",
        command_digest="2" * 64,
        chunks=(
            ProjectionChunk(
                chunk_id="issue677-chunk",
                text="synthetic protected text",
                evidence=(evidence,),
                read_context=read_context,
                read_contexts=(read_context,),
            ),
        ),
        evidence=(evidence,),
        retrieval_strategy="hybrid",
        read_context=read_context,
        read_contexts=(read_context,),
    )
    profile = ContextProfileVersion.create(
        profile_id=UUID(int=20),
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

    class Trusted:
        async def resolve(self, request: object) -> TrustedAuthorizationContext:
            del request
            return TrustedAuthorizationContext(
                mode=AuthorizationContextMode.HUMAN_DISCLOSURE,
                workspace_id=WORKSPACE_ID,
                environment_id=ENVIRONMENT_ID,
                canonical_human_id=stable_id("human"),
                purpose="answer_question",
                operation="retrieve_context",
                membership_generation=1,
                authority_generation=1,
                authority_snapshot_reference=stable_id("authority"),
                trace_id=stable_id("trace"),
                issued_at=NOW,
            )

    class Profiles:
        async def resolve(self, request: object) -> ContextProfileResolution:
            del request
            return ContextProfileResolution(
                outcome=ContextProfileResolutionOutcome.RESOLVED,
                reason=ContextProfileResolutionReason.RESOLVED,
                profile=profile,
                policy=EffectiveContextRetrievalPolicy(
                    workspace_id=WORKSPACE_ID,
                    environment_id=ENVIRONMENT_ID,
                    profile_ref=profile.reference,
                    retrieval_strategy=RetrievalStrategy.HYBRID,
                    max_results=10,
                    max_tokens=1000,
                    max_relation_hops=2,
                    freshness_max_age_seconds=60,
                    completeness_requirement=CompletenessRequirement.BEST_EFFORT,
                    partial_answer_fallback=PartialAnswerFallback.ALLOWED,
                ),
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
        async def load(self, **kwargs: object) -> ContextBrokerReadModel:
            del kwargs
            return ContextBrokerReadModel(
                projection_run=physical_context.projection_run,
                snapshot_members=physical_context.snapshot_members,
                partitions=physical_context.partitions,
                receipts=physical_context.receipts,
                source_references=physical_context.source_references,
                canonical_boundary=CanonicalBoundary(
                    boundary_id=publication.canonical_boundary_id,
                    workspace_id=WORKSPACE_ID,
                    environment_id=ENVIRONMENT_ID,
                    projection_kind="qa",
                    scope_version="r1",
                    accepted_through_generation=publication.accepted_through_generation,
                    boundary_time=publication.as_of.boundary_time,
                    manifest_digest=publication.canonical_boundary_manifest_digest,
                    idempotency_receipt_id=stable_id("boundary-receipt"),
                ),
                provider_capability_reference="provider:qa",
            )

    class Plans:
        async def resolve(self, context: object) -> SimpleNamespace:
            del context
            return SimpleNamespace()

    class Retrieval:
        calls = 0

        async def execute(self, plan: object) -> ProjectionRetrievalResult:
            del plan
            self.calls += 1
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
                authorization_decision_reference=stable_id("authorization-decision"),
                decision_reference=stable_id("evidence-decision"),
            )

    class Publication:
        async def resolve_active_publication(self, projection_kind: str) -> object:
            assert projection_kind == "qa"
            return publication

    retrieval = Retrieval()
    bundles = InMemoryContextBundleStore()
    broker = AuthorizedContextBrokerApplicationService(
        trusted_context=Trusted(),
        profile_resolver=Profiles(),
        publication=Publication(),
        scope_resolver=Scope(),
        read_model=ReadModel(),
        plan_builder=Plans(),
        retrieval=retrieval,
        evidence_verifier=Evidence(),
        bundles=bundles,
        clock=lambda: NOW,
    )
    request = AuthorizedContextRequest(
        request_id=stable_id(f"request:{revision.revision_id}"),
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        query="What supports the proposal?",
        profile_ref=profile.reference,
        trusted_request_context_ref=stable_id("trusted-request"),
        authorization_context_ref=stable_id("authorization-context"),
        run_id=stable_id(f"run:{revision.revision_id}"),
        attempt_id=stable_id(f"attempt:{revision.revision_id}"),
        idempotency_key=f"issue677:{revision.revision_id}",
        diagnostic_context=DiagnosticContext(trace_id=stable_id("diagnostic-trace")),
    )
    first = await broker.retrieve(request)
    replay = await broker.retrieve(request)
    assert first.outcome is ContextOutcome.ALLOWED
    assert first == replay
    assert retrieval.calls == 1
    return first, retrieval_result
