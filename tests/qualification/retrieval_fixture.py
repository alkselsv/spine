"""Projection/read-model fixture owned by the issue #677 qualification."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from uuid import UUID

from spine.application.authorized_retrieval_scope import (
    AuthorizedRetrievalScope,
    AuthorizedSourceConstraint,
)
from spine.application.diagnostics import DiagnosticContext
from spine.domain.context_profiles import (
    CompletenessRequirement,
    ContextProfileRef,
    PartialAnswerFallback,
    RetrievalStrategy,
)
from spine.domain.projections import (
    CanonicalAsOf,
    ProjectionActivation,
    ProjectionActivationKind,
    ProjectionCandidateState,
    ProjectionConfigVersion,
    ProjectionOperation,
    ProjectionPartition,
    ProjectionReceipt,
    ProjectionRun,
    ProjectionSnapshot,
    ProjectionSnapshotMember,
    ProjectionSnapshotMemberDisposition,
    ProjectionSnapshotState,
    PublishedActivePublication,
    SourceProjectionReference,
)
from spine.memory.contracts import PublicationPin
from spine.memory.read_plan import PhysicalReadContext, PhysicalReadIsolationRequirement

NOW = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)
WORKSPACE = UUID(int=1)
ENVIRONMENT = UUID(int=2)
SNAPSHOT = UUID(int=3)
RUN = UUID(int=4)
CANDIDATE = UUID(int=5)
CONFIG = UUID(int=6)
BOUNDARY = UUID(int=7)
ACTIVATION = UUID(int=8)


def qualification_projection_config() -> ProjectionConfigVersion:
    payload: dict[str, object] = {}
    return ProjectionConfigVersion(
        projection_config_version_id=CONFIG,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        projection_kind="qa",
        version=1,
        ontology_version_id=UUID(int=9),
        parser_contract_version="parser:v1",
        locator_contract_version="locator:v1",
        extraction_contract_version="extractor:v1",
        embedding_profile_version="embedding:v1",
        adapter_contract_version="adapter:v1",
        config_payload=payload,
        config_digest=sha256(
            b'{"payload":{},"schema":"projection-config:r1"}'
        ).hexdigest(),
        created_at=NOW,
    )


def qualification_publication(
    config: ProjectionConfigVersion,
) -> PublishedActivePublication:
    snapshot = ProjectionSnapshot(
        projection_snapshot_id=SNAPSHOT,
        projection_run_id=RUN,
        projection_candidate_id=CANDIDATE,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        snapshot_state=ProjectionSnapshotState.FINALIZED,
        projection_kind="qa",
        projection_config_version_id=CONFIG,
        canonical_boundary_id=BOUNDARY,
        candidate_evidence_digest="a" * 64,
        manifest_digest="b" * 64,
        member_count=1,
        created_at=NOW,
    )
    activation = ProjectionActivation(
        projection_activation_id=ACTIVATION,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        projection_kind="qa",
        projection_snapshot_id=SNAPSHOT,
        activation_generation=2,
        activation_kind=ProjectionActivationKind.PUBLISH,
        command_identity="activation-1",
        command_digest="c" * 64,
        idempotency_receipt_id=UUID(int=10),
        audit_event_id=UUID(int=11),
        activated_at=NOW,
    )
    return PublishedActivePublication(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        projection_kind="qa",
        projection_snapshot=snapshot,
        projection_activation=activation,
        activation_generation=2,
        active_pointer_cas_version=3,
        canonical_boundary_id=BOUNDARY,
        canonical_boundary_manifest_digest="d" * 64,
        accepted_through_generation=4,
        canonical_boundary_membership_ref="members",
        as_of=CanonicalAsOf(
            canonical_boundary_id=BOUNDARY,
            accepted_through_generation=4,
            boundary_time=NOW,
        ),
        projection_config_version=config,
        freshness="current",
        health="healthy",
    )


def qualification_physical_context(
    *, source: UUID, revision: UUID
) -> PhysicalReadContext:
    publication = qualification_publication(qualification_projection_config())
    partition_id = UUID(int=40)
    receipt_id = UUID(int=41)
    member_id = UUID(int=42)
    run = ProjectionRun(
        projection_run_id=RUN,
        projection_candidate_id=CANDIDATE,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        projection_kind="qa",
        operation=ProjectionOperation.BUILD,
        projection_config_version_id=CONFIG,
        canonical_boundary_id=BOUNDARY,
        command_identity="run-1",
        command_digest="f" * 64,
        idempotency_receipt_id=UUID(int=45),
        candidate_state=ProjectionCandidateState.SEALED,
        candidate_evidence_digest="a" * 64,
        candidate_sealed_at=NOW,
        started_at=NOW,
    )
    partition = ProjectionPartition(
        projection_partition_id=partition_id,
        projection_run_id=RUN,
        projection_candidate_id=CANDIDATE,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        security_domain_reference="security-domain:default",
        security_domain_version="r1",
        partition_identity="default",
        expected_command_identity="partition-1",
        expected_command_digest="1" * 64,
        created_at=NOW,
    )
    receipt = ProjectionReceipt(
        projection_receipt_id=receipt_id,
        projection_run_id=RUN,
        projection_candidate_id=CANDIDATE,
        projection_partition_id=partition_id,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        command_identity="partition-1",
        command_digest="1" * 64,
        artifact_kind="cognee",
        artifact_reference="artifact-1",
        artifact_integrity_digest="2" * 64,
        implementation_schema_version="adapter:v1",
        idempotency_receipt_id=UUID(int=46),
        completed_at=NOW,
    )
    member = ProjectionSnapshotMember(
        projection_snapshot_member_id=member_id,
        projection_snapshot_id=SNAPSHOT,
        canonical_boundary_id=BOUNDARY,
        canonical_boundary_member_id=UUID(int=47),
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        source_object_id=source,
        source_revision_id=revision,
        disposition=ProjectionSnapshotMemberDisposition.INCLUDED,
        projection_partition_id=partition_id,
        projection_receipt_id=receipt_id,
    )
    reference = SourceProjectionReference(
        source_projection_reference_id=UUID(int=48),
        projection_run_id=RUN,
        projection_candidate_id=CANDIDATE,
        projection_receipt_id=receipt_id,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        source_object_id=source,
        source_revision_id=revision,
        segment_id="chunk-1",
        locator_schema_version="locator:v1",
        locator_reference="page:1",
        locator_digest="3" * 64,
        projected_kind="chunk",
        projected_object_id="chunk-1",
        projection_config_version_id=CONFIG,
        security_domain_reference=partition.security_domain_reference,
        created_at=NOW,
    )
    scope = AuthorizedRetrievalScope(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        profile_ref=ContextProfileRef(
            profile_id=UUID(int=20), profile_version=1, profile_digest="e" * 64
        ),
        projection_kind="qa",
        projection_snapshot_id=SNAPSHOT,
        projection_activation_id=ACTIVATION,
        activation_generation=2,
        active_pointer_cas_version=3,
        canonical_boundary_id=BOUNDARY,
        canonical_boundary_manifest_digest="d" * 64,
        canonical_as_of=publication.as_of,
        projection_config_version_id=CONFIG,
        publication_freshness="current",
        publication_health="healthy",
        source_constraints=(
            AuthorizedSourceConstraint(
                projection_snapshot_member_id=member_id,
                source_object_id=source,
                source_revision_id=revision,
                policy_version_id=UUID(int=30),
                policy_generation=1,
                membership_generation=1,
                authority_generation=1,
                decision_reference=UUID(int=31),
            ),
        ),
        retrieval_strategy=RetrievalStrategy.HYBRID,
        max_results=20,
        max_tokens=1000,
        max_relation_hops=2,
        freshness_max_age_seconds=60,
        completeness_requirement=CompletenessRequirement.BEST_EFFORT,
        partial_answer_fallback=PartialAnswerFallback.ALLOWED,
    )
    return PhysicalReadContext(
        query="find the answer",
        diagnostic_context=DiagnosticContext(trace_id=UUID(int=1000)),
        publication_pin=PublicationPin.from_publication(publication),
        authorized_scope=scope,
        projection_run=run,
        snapshot_members=(member,),
        partitions=(partition,),
        receipts=(receipt,),
        source_references=(reference,),
        source_isolation=PhysicalReadIsolationRequirement(
            provider_capability_reference="cognee-source-filter:v1",
            source_object_ids=(source,),
            source_revision_ids=(revision,),
            pre_retrieval_enforced=True,
        ),
    )
