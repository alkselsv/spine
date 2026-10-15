from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from uuid import UUID

from spine.application.context_profiles import EffectiveContextRetrievalPolicy
from spine.application.retrieval_gates import (
    RetrievalGateDecision,
    RetrievalGateRequest,
    RetrievalGateService,
)
from spine.domain.context_profiles import (
    CompletenessRequirement,
    ContextProfileRef,
    PartialAnswerFallback,
    RetrievalStrategy,
)
from spine.domain.context_bundles import PolicyDecisionGeneration
from spine.domain.projections import (
    CanonicalAsOf,
    ProjectionActivation,
    ProjectionActivationKind,
    ProjectionConfigVersion,
    ProjectionFreshness,
    ProjectionHealth,
    ProjectionReconciliationOutcome,
    ProjectionReconciliationResult,
    ProjectionSnapshot,
    ProjectionSnapshotState,
    UnavailableActivePublication,
    UnpublishedActivePublication,
    PublishedActivePublication,
    projection_reconciliation_findings_digest,
)
from spine.memory.contracts import (
    ProjectionChunk,
    ProjectionRetrievalResult,
    ProjectionOutcome,
    ProjectionFailure,
    ProjectionFailureCode,
    ProjectionDiagnostic,
    PublicationPin,
    StableLocator,
    EvidenceReference,
    RetrievalReadContext,
)
from spine.application.evidence_lineage_verification import VerifiedEvidenceItem
from spine.application.evidence_lineage_verification import (
    RejectedEvidenceItem,
    EvidenceVerificationReasonCode,
    EvidenceVerificationDiagnostic,
)


NOW = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)
WORKSPACE = UUID(int=1)
ENVIRONMENT = UUID(int=2)
PROFILE_ID = UUID(int=3)
SNAPSHOT = UUID(int=4)
ACTIVATION = UUID(int=5)
BOUNDARY = UUID(int=6)
RUN = UUID(int=7)
CANDIDATE = UUID(int=8)
PARTITION = UUID(int=9)
RECEIPT = UUID(int=10)
SOURCE = UUID(int=11)
REVISION = UUID(int=12)
EVIDENCE = UUID(int=13)


def _profile(**overrides: object) -> EffectiveContextRetrievalPolicy:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE,
        "environment_id": ENVIRONMENT,
        "profile_ref": ContextProfileRef(
            profile_id=PROFILE_ID,
            profile_version=1,
            profile_digest="a" * 64,
        ),
        "retrieval_strategy": RetrievalStrategy.HYBRID,
        "max_results": 10,
        "max_tokens": 1000,
        "max_relation_hops": 2,
        "freshness_max_age_seconds": 3600,
        "completeness_requirement": CompletenessRequirement.COMPLETE,
        "partial_answer_fallback": PartialAnswerFallback.ALLOWED,
    }
    values.update(overrides)
    return EffectiveContextRetrievalPolicy(**values)


def _publication() -> PublishedActivePublication:
    config_payload: dict[str, object] = {}
    config_digest = sha256(
        json.dumps(
            {"schema": "projection-config:r1", "payload": config_payload},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    config = ProjectionConfigVersion(
        projection_config_version_id=UUID(int=20),
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        projection_kind="qa",
        version=1,
        ontology_version_id=UUID(int=21),
        parser_contract_version="parser:v1",
        locator_contract_version="locator:v1",
        extraction_contract_version="extractor:v1",
        embedding_profile_version="embedding:v1",
        adapter_contract_version="adapter:v1",
        config_payload=config_payload,
        config_digest=config_digest,
        created_at=NOW,
    )
    snapshot = ProjectionSnapshot(
        projection_snapshot_id=SNAPSHOT,
        projection_run_id=RUN,
        projection_candidate_id=CANDIDATE,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        snapshot_state=ProjectionSnapshotState.FINALIZED,
        projection_kind="qa",
        projection_config_version_id=config.projection_config_version_id,
        canonical_boundary_id=BOUNDARY,
        candidate_evidence_digest="b" * 64,
        manifest_digest="c" * 64,
        member_count=1,
        created_at=NOW,
    )
    activation = ProjectionActivation(
        projection_activation_id=ACTIVATION,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        projection_kind="qa",
        projection_snapshot_id=SNAPSHOT,
        activation_generation=1,
        activation_kind=ProjectionActivationKind.PUBLISH,
        command_identity="activation-1",
        command_digest="d" * 64,
        idempotency_receipt_id=UUID(int=22),
        audit_event_id=UUID(int=23),
        activated_at=NOW,
    )
    findings = {"healthy": True}
    reconciliation = ProjectionReconciliationResult(
        reconciliation_result_id=UUID(int=24),
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        projection_kind="qa",
        projection_snapshot_id=SNAPSHOT,
        projection_activation_id=ACTIVATION,
        activation_generation=1,
        attempt_identity="reconcile-1",
        attempt_digest="e" * 64,
        outcome=ProjectionReconciliationOutcome.HEALTHY,
        findings_schema_version="reconciliation-findings:v1",
        findings_payload=findings,
        findings_digest=projection_reconciliation_findings_digest(
            findings_schema_version="reconciliation-findings:v1",
            findings_payload=findings,
        ),
        artifact_evidence_reference="artifact:reconciliation-1",
        artifact_evidence_digest="f" * 64,
        idempotency_receipt_id=UUID(int=25),
        audit_event_reference="audit:reconciliation-1",
        checked_at=NOW,
    )
    return PublishedActivePublication(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        projection_kind="qa",
        projection_snapshot=snapshot,
        projection_activation=activation,
        activation_generation=1,
        active_pointer_cas_version=1,
        canonical_boundary_id=BOUNDARY,
        canonical_boundary_manifest_digest="1" * 64,
        accepted_through_generation=1,
        canonical_boundary_membership_ref="members:1",
        as_of=CanonicalAsOf(
            canonical_boundary_id=BOUNDARY,
            accepted_through_generation=1,
            boundary_time=NOW,
        ),
        projection_config_version=config,
        freshness=ProjectionFreshness.CURRENT,
        health=ProjectionHealth.HEALTHY,
        effective_reconciliation=reconciliation,
    )


def _retrieval(
    *, publication_pin_id: UUID | None = None
) -> tuple[ProjectionRetrievalResult, VerifiedEvidenceItem]:
    publication_pin_id = publication_pin_id or UUID("4f2ec0e4-1a1b-5a36-a1d1-1d4f8f5f6f4a")
    read_context = RetrievalReadContext(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        publication_pin_id=publication_pin_id,
        publication_pin_digest="2" * 64,
        projection_snapshot_id=SNAPSHOT,
        projection_activation_id=ACTIVATION,
        activation_generation=1,
        canonical_boundary_id=BOUNDARY,
        canonical_boundary_digest="3" * 64,
        projection_run_id=RUN,
        projection_candidate_id=CANDIDATE,
        projection_partition_id=PARTITION,
        projection_receipt_id=RECEIPT,
        physical_namespace="qa:active",
        physical_namespace_digest="4" * 64,
        source_object_ids=(SOURCE,),
        source_revision_ids=(REVISION,),
        provider_capability_reference="provider:qa",
        source_isolation_pre_retrieval_enforced=True,
    )
    evidence = EvidenceReference(
        evidence_id="evidence-1",
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        source_object_id=SOURCE,
        source_revision_id=REVISION,
        revision_digest="5" * 64,
        revision_number=1,
        processing_receipt_id=UUID(int=26),
        locator_contract_version="locator:v1",
        locator=StableLocator(kind="page", value="1"),
        evidence_digest="6" * 64,
    )
    item = ProjectionChunk(
        chunk_id="chunk-1",
        text="protected text",
        evidence=(evidence,),
        read_context=read_context,
        read_contexts=(read_context,),
    )
    result = ProjectionRetrievalResult(
        outcome=ProjectionOutcome.SUCCEEDED,
        command_identity="retrieve-1",
        command_digest="7" * 64,
        chunks=(item,),
        evidence=(evidence,),
        retrieval_strategy="hybrid",
        logical_projection_version="1",
        physical_projection_version="physical-1",
        read_context=read_context,
        read_contexts=(read_context,),
    )
    verified = VerifiedEvidenceItem(
        item=item,
        publication_pin_id=publication_pin_id,
        authorization_decision_reference=UUID(int=27),
        decision_reference=UUID(int=28),
    )
    return result, verified


def test_gate_allows_current_healthy_verified_evidence_and_preserves_pin() -> None:
    publication = _publication()
    retrieval, verified = _retrieval(
        publication_pin_id=PublicationPin.from_publication(publication).pin_id
    )
    decision = RetrievalGateService().evaluate(
        RetrievalGateRequest(
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            profile=_profile(),
            publication=publication,
            retrieval=retrieval,
            evidence_verifications=(verified,),
            policy_decision_generations=(_policy_generation(),),
        )
    )

    assert decision.outcome.value == "allowed"
    assert decision.publication_pin is not None
    assert decision.publication_pin.projection_snapshot.projection_snapshot_id == SNAPSHOT
    assert tuple(item.evidence_id for item in decision.evidence_references) == ("evidence-1",)
    assert decision.authorization_decision_references == (UUID(int=27),)
    assert decision.policy_decision_generations[0].policy_generation == 2
    assert RetrievalGateDecision.model_validate_json(decision.model_dump_json()) == decision


def _policy_generation() -> PolicyDecisionGeneration:
    return PolicyDecisionGeneration(
        source_object_id=SOURCE,
        policy_version_id=UUID(int=30),
        policy_generation=2,
        membership_generation=3,
        authority_generation=4,
        decision_reference=UUID(int=27),
    )


def test_gate_returns_stale_without_issuing_evidence() -> None:
    publication = _publication().model_copy(
        update={"freshness": ProjectionFreshness.STALE}
    )
    retrieval, verified = _retrieval(
        publication_pin_id=PublicationPin.from_publication(publication).pin_id
    )

    decision = RetrievalGateService().evaluate(
        RetrievalGateRequest(
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            profile=_profile(),
            publication=publication,
            retrieval=retrieval,
            evidence_verifications=(verified,),
        )
    )

    assert decision.outcome.value == "stale"
    assert decision.evidence_references == ()
    assert not decision.bundle_eligible


def test_gate_returns_degraded_for_unhealthy_publication_or_reconciliation() -> None:
    publication = _publication().model_copy(
        update={"health": ProjectionHealth.DEGRADED}
    )
    retrieval, verified = _retrieval(
        publication_pin_id=PublicationPin.from_publication(publication).pin_id
    )

    decision = RetrievalGateService().evaluate(
        RetrievalGateRequest(
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            profile=_profile(),
            publication=publication,
            retrieval=retrieval,
            evidence_verifications=(verified,),
        )
    )

    assert decision.outcome.value == "degraded"
    assert decision.publication_pin is not None
    assert decision.evidence_references == ()


def test_gate_rejects_unpublished_or_missing_reconciliation_as_unavailable() -> None:
    publication = _publication().model_copy(update={"effective_reconciliation": None})
    retrieval, verified = _retrieval(
        publication_pin_id=PublicationPin.from_publication(publication).pin_id
    )

    decision = RetrievalGateService().evaluate(
        RetrievalGateRequest(
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            profile=_profile(),
            publication=publication,
            retrieval=retrieval,
            evidence_verifications=(verified,),
        )
    )

    assert decision.outcome.value == "unavailable"
    assert decision.reason_code.value == "publication_unavailable"


def test_gate_rejects_unpublished_publication_before_provider_evidence() -> None:
    publication = UnpublishedActivePublication(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        projection_kind="qa",
    )
    retrieval, verified = _retrieval()

    decision = RetrievalGateService().evaluate(
        RetrievalGateRequest(
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            profile=_profile(),
            publication=publication,
            retrieval=retrieval,
            evidence_verifications=(verified,),
        )
    )

    assert decision.outcome.value == "unavailable"
    assert decision.publication_pin is None
    assert decision.evidence_references == ()


def test_gate_preserves_safe_partial_provider_failure_only_with_verified_items() -> None:
    publication = _publication()
    retrieval, verified = _retrieval(
        publication_pin_id=PublicationPin.from_publication(publication).pin_id
    )
    failure = ProjectionFailure(
        code=ProjectionFailureCode.PHYSICAL_FAILURE,
        retryable=True,
        safe_detail=ProjectionDiagnostic(),
    )
    partial = retrieval.model_copy(
        update={
            "outcome": ProjectionOutcome.PARTIAL,
            "partial_failures": (failure,),
        }
    )

    decision = RetrievalGateService().evaluate(
        RetrievalGateRequest(
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            profile=_profile(),
            publication=publication,
            retrieval=partial,
            evidence_verifications=(verified,),
            policy_decision_generations=(_policy_generation(),),
        )
    )

    assert decision.outcome.value == "partial"
    assert decision.bundle_eligible


def test_gate_rejects_stale_provenance_even_when_verifier_claims_verified() -> None:
    publication = _publication()
    retrieval, verified = _retrieval(
        publication_pin_id=PublicationPin.from_publication(publication).pin_id
    )
    stale_reference = retrieval.evidence[0].model_copy(update={"status": "stale"})
    stale_item = retrieval.chunks[0].model_copy(update={"evidence": (stale_reference,)})
    stale_retrieval = retrieval.model_copy(
        update={"chunks": (stale_item,), "evidence": (stale_reference,)}
    )
    claimed_verified = verified.model_copy(update={"item": stale_item})

    decision = RetrievalGateService().evaluate(
        RetrievalGateRequest(
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            profile=_profile(),
            publication=publication,
            retrieval=stale_retrieval,
            evidence_verifications=(claimed_verified,),
            policy_decision_generations=(_policy_generation(),),
        )
    )

    assert decision.outcome.value == "abstained"
    assert decision.evidence_references == ()


def test_gate_rejects_provider_strategy_that_does_not_match_selected_profile() -> None:
    publication = _publication()
    retrieval, verified = _retrieval(
        publication_pin_id=PublicationPin.from_publication(publication).pin_id
    )
    mismatched = retrieval.model_copy(update={"retrieval_strategy": "chunks"})

    decision = RetrievalGateService().evaluate(
        RetrievalGateRequest(
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            profile=_profile(),
            publication=publication,
            retrieval=mismatched,
            evidence_verifications=(verified,),
            policy_decision_generations=(_policy_generation(),),
        )
    )

    assert decision.outcome.value == "abstained"
    assert decision.evidence_references == ()


def test_gate_abstains_when_retrieval_has_no_evidence() -> None:
    publication = _publication()
    retrieval, _ = _retrieval(
        publication_pin_id=PublicationPin.from_publication(publication).pin_id
    )
    empty = retrieval.model_copy(update={"chunks": (), "evidence": ()})

    decision = RetrievalGateService().evaluate(
        RetrievalGateRequest(
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            profile=_profile(),
            publication=publication,
            retrieval=empty,
            evidence_verifications=(),
        )
    )

    assert decision.outcome.value == "insufficient"
    assert "protected text" not in decision.model_dump_json()


def test_gate_accepts_provider_partial_only_when_profile_allows_safe_fallback() -> None:
    publication = _publication()
    retrieval, verified = _retrieval(
        publication_pin_id=PublicationPin.from_publication(publication).pin_id
    )
    partial = retrieval.model_copy(update={"outcome": ProjectionOutcome.PARTIAL})

    allowed = RetrievalGateService().evaluate(
        RetrievalGateRequest(
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            profile=_profile(),
            publication=publication,
            retrieval=partial,
            evidence_verifications=(verified,),
            policy_decision_generations=(_policy_generation(),),
        )
    )
    denied = RetrievalGateService().evaluate(
        RetrievalGateRequest(
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            profile=_profile(
                partial_answer_fallback=PartialAnswerFallback.DISALLOWED
            ),
            publication=publication,
            retrieval=partial,
            evidence_verifications=(verified,),
            policy_decision_generations=(_policy_generation(),),
        )
    )

    assert allowed.outcome.value == "partial"
    assert allowed.publication_pin is not None
    assert denied.outcome.value == "insufficient"
    assert denied.evidence_references == ()


def test_gate_maps_cancelled_and_authorization_failure_without_details() -> None:
    publication = _publication()
    retrieval, verified = _retrieval(
        publication_pin_id=PublicationPin.from_publication(publication).pin_id
    )
    cancelled = RetrievalGateService().evaluate(
        RetrievalGateRequest(
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            profile=_profile(),
            publication=publication,
            retrieval=retrieval,
            evidence_verifications=(verified,),
            cancelled=True,
        )
    )
    rejected = RejectedEvidenceItem(
        reason_code=EvidenceVerificationReasonCode.AUTHORIZATION_DENIED,
        decision_reference=UUID(int=29),
        diagnostic=EvidenceVerificationDiagnostic(),
    )
    denied = RetrievalGateService().evaluate(
        RetrievalGateRequest(
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            profile=_profile(),
            publication=publication,
            retrieval=retrieval,
            evidence_verifications=(rejected,),
        )
    )

    assert cancelled.outcome.value == "cancelled"
    assert denied.outcome.value == "denied"
    assert denied.evidence_references == ()
    assert "protected text" not in denied.model_dump_json()
