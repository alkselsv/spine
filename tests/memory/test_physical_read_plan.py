from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from uuid import UUID

import pytest

from spine.application.diagnostics import DiagnosticContext
from spine.application.authorized_retrieval_scope import (
    AuthorizedRetrievalScope,
    AuthorizedSourceConstraint,
)
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
from spine.memory.contracts import (
    CanonicalBoundary,
    ProjectionCandidate,
    ProjectionConfigVersion as ProviderProjectionConfigVersion,
    ProjectionNamespaceState,
)
from spine.memory.namespace import (
    ProjectionNamespaceContext,
    ProjectionNamespaceRegistry,
)
from spine.memory.read_plan import (
    PhysicalReadContext,
    PhysicalReadIsolationRequirement,
    PhysicalReadPlanBuilder,
    PhysicalReadPlanOutcome,
)


NOW = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)
WORKSPACE = UUID(int=1)
ENVIRONMENT = UUID(int=2)
SNAPSHOT = UUID(int=3)
RUN = UUID(int=4)
CANDIDATE = UUID(int=5)
CONFIG = UUID(int=6)
BOUNDARY = UUID(int=7)
ACTIVATION = UUID(int=8)


def _config() -> ProjectionConfigVersion:
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


def _publication(config: ProjectionConfigVersion) -> PublishedActivePublication:
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


def _scope(source: UUID, revision: UUID, member: UUID, partition: UUID, receipt: UUID) -> AuthorizedRetrievalScope:
    return AuthorizedRetrievalScope(
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
        canonical_as_of=CanonicalAsOf(
            canonical_boundary_id=BOUNDARY,
            accepted_through_generation=4,
            boundary_time=NOW,
        ),
        projection_config_version_id=CONFIG,
        publication_freshness="current",
        publication_health="healthy",
        source_constraints=(
            AuthorizedSourceConstraint(
                projection_snapshot_member_id=member,
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


def _context() -> tuple[PhysicalReadContext, ProjectionNamespaceRegistry]:
    config = _config()
    publication = _publication(config)
    pin = PublicationPin.from_publication(publication)
    partition_id = UUID(int=40)
    receipt_id = UUID(int=41)
    member_id = UUID(int=42)
    source = UUID(int=43)
    revision = UUID(int=44)
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
    scope = _scope(source, revision, member_id, partition_id, receipt_id)
    isolation = PhysicalReadIsolationRequirement(
        provider_capability_reference="cognee-source-filter:v1",
        source_object_ids=(source,),
        source_revision_ids=(revision,),
        pre_retrieval_enforced=True,
    )
    context = PhysicalReadContext(
        query="find the answer",
        diagnostic_context=DiagnosticContext(trace_id=UUID(int=1000)),
        publication_pin=pin,
        authorized_scope=scope,
        projection_run=run,
        snapshot_members=(member,),
        partitions=(partition,),
        receipts=(receipt,),
        source_references=(reference,),
        source_isolation=isolation,
    )
    registry = ProjectionNamespaceRegistry()
    _register_namespace(registry, partition_id, config)
    return context, registry


def _register_namespace(
    registry: ProjectionNamespaceRegistry,
    partition_id: UUID,
    config: ProjectionConfigVersion,
    *,
    state: ProjectionNamespaceState = ProjectionNamespaceState.ACTIVE,
) -> None:
    registry.register(
        ProjectionNamespaceContext(
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            projection_kind="qa",
            projection_candidate=ProjectionCandidate(
                candidate_id=CANDIDATE,
                run_id=RUN,
                partition_id=partition_id,
                snapshot_id=SNAPSHOT,
            ),
            projection_config=ProviderProjectionConfigVersion(
                config_id=CONFIG,
                version="1",
                config_digest=config.config_digest,
                ontology_version=str(config.ontology_version_id),
                adapter_contract_version=config.adapter_contract_version,
            ),
            canonical_boundary=CanonicalBoundary(
                boundary_id=BOUNDARY,
                boundary_digest="d" * 64,
                accepted_through_generation=4,
            ),
            state=state,
        )
    )


def test_valid_context_builds_one_typed_partition_read() -> None:
    context, registry = _context()

    result = PhysicalReadPlanBuilder(namespace_registry=registry).build(context)

    assert result.outcome is PhysicalReadPlanOutcome.READY
    assert result.plan is not None
    assert result.plan.context.publication_pin.pin_id == context.publication_pin.pin_id
    assert len(result.plan.operations) == 1
    operation = result.plan.operations[0]
    assert operation.projection_partition.projection_partition_id == UUID(int=40)
    assert operation.source_revision_ids == (UUID(int=44),)
    assert operation.physical_namespace.state.value == "active"


def test_one_snapshot_fans_out_to_all_eligible_partitions_in_stable_order() -> None:
    context, registry = _context()
    config = _config()
    partition = ProjectionPartition(
        projection_partition_id=UUID(int=50),
        projection_run_id=RUN,
        projection_candidate_id=CANDIDATE,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        security_domain_reference="security-domain:default",
        security_domain_version="r1",
        partition_identity="secondary",
        expected_command_identity="partition-2",
        expected_command_digest="4" * 64,
        created_at=NOW,
    )
    receipt = ProjectionReceipt(
        projection_receipt_id=UUID(int=51),
        projection_run_id=RUN,
        projection_candidate_id=CANDIDATE,
        projection_partition_id=partition.projection_partition_id,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        command_identity="partition-2",
        command_digest="4" * 64,
        artifact_kind="cognee",
        artifact_reference="artifact-2",
        artifact_integrity_digest="5" * 64,
        implementation_schema_version="adapter:v1",
        idempotency_receipt_id=UUID(int=52),
        completed_at=NOW,
    )
    member = ProjectionSnapshotMember(
        projection_snapshot_member_id=UUID(int=53),
        projection_snapshot_id=SNAPSHOT,
        canonical_boundary_id=BOUNDARY,
        canonical_boundary_member_id=UUID(int=54),
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        source_object_id=UUID(int=55),
        source_revision_id=UUID(int=56),
        disposition=ProjectionSnapshotMemberDisposition.INCLUDED,
        projection_partition_id=partition.projection_partition_id,
        projection_receipt_id=receipt.projection_receipt_id,
    )
    reference = SourceProjectionReference(
        source_projection_reference_id=UUID(int=57),
        projection_run_id=RUN,
        projection_candidate_id=CANDIDATE,
        projection_receipt_id=receipt.projection_receipt_id,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        source_object_id=member.source_object_id,
        source_revision_id=member.source_revision_id,
        segment_id="chunk-2",
        locator_schema_version="locator:v1",
        locator_reference="page:2",
        locator_digest="6" * 64,
        projected_kind="chunk",
        projected_object_id="chunk-2",
        projection_config_version_id=CONFIG,
        security_domain_reference=partition.security_domain_reference,
        created_at=NOW,
    )
    constraint = AuthorizedSourceConstraint(
        projection_snapshot_member_id=member.projection_snapshot_member_id,
        source_object_id=member.source_object_id,
        source_revision_id=member.source_revision_id,
        policy_version_id=UUID(int=58),
        policy_generation=1,
        membership_generation=1,
        authority_generation=1,
        decision_reference=UUID(int=59),
    )
    isolation = context.source_isolation.model_copy(
        update={
            "source_object_ids": (UUID(int=43), member.source_object_id),
            "source_revision_ids": (UUID(int=44), member.source_revision_id),
        }
    )
    updated_scope = context.authorized_scope.model_copy(
        update={
            "source_constraints": context.authorized_scope.source_constraints + (constraint,)
        }
    )
    _register_namespace(registry, partition.projection_partition_id, config)
    updated = context.model_copy(
        update={
            "authorized_scope": updated_scope,
            "partitions": context.partitions + (partition,),
            "receipts": context.receipts + (receipt,),
            "snapshot_members": context.snapshot_members + (member,),
            "source_references": context.source_references + (reference,),
            "source_isolation": isolation,
        }
    )

    limited = PhysicalReadPlanBuilder(namespace_registry=registry).build(
        updated.model_copy(update={"max_partitions": 1})
    )
    assert limited.outcome is PhysicalReadPlanOutcome.UNSAFE
    assert limited.reason.value == "fanout_limit"

    result = PhysicalReadPlanBuilder(namespace_registry=registry).build(updated)

    assert result.outcome is PhysicalReadPlanOutcome.READY
    assert result.plan is not None
    assert [
        operation.projection_partition.partition_identity
        for operation in result.plan.operations
    ] == ["default", "secondary"]
    assert [
        operation.source_revision_ids for operation in result.plan.operations
    ] == [(UUID(int=44),), (UUID(int=56),)]
    assert len({
        operation.physical_namespace.namespace_digest
        for operation in result.plan.operations
    }) == 2


def test_isolation_failure_is_typed_and_never_produces_a_plan() -> None:
    context, registry = _context()
    unsafe = context.source_isolation.model_copy(update={"pre_retrieval_enforced": False})

    result = PhysicalReadPlanBuilder(namespace_registry=registry).build(
        context.model_copy(update={"source_isolation": unsafe})
    )

    assert result.outcome is PhysicalReadPlanOutcome.UNSAFE
    assert result.plan is None


@pytest.mark.parametrize(
    "state",
    (
        ProjectionNamespaceState.SHADOW,
        ProjectionNamespaceState.REBUILDING,
        ProjectionNamespaceState.FAILED,
        ProjectionNamespaceState.RETIRED,
    ),
)
def test_non_active_namespace_is_excluded_before_provider_boundary(
    state: ProjectionNamespaceState,
) -> None:
    context, _ = _context()
    registry = ProjectionNamespaceRegistry()
    _register_namespace(
        registry,
        UUID(int=40),
        _config(),
        state=state,
    )

    result = PhysicalReadPlanBuilder(namespace_registry=registry).build(context)

    assert result.outcome is PhysicalReadPlanOutcome.UNSAFE
    assert result.reason.value == "ineligible_namespace"
    assert result.plan is None


def test_missing_receipt_lineage_fails_closed() -> None:
    context, registry = _context()
    missing = context.receipts[0].model_copy(
        update={"projection_receipt_id": UUID(int=49)}
    )

    result = PhysicalReadPlanBuilder(namespace_registry=registry).build(
        context.model_copy(update={"receipts": (missing,)})
    )

    assert result.outcome is PhysicalReadPlanOutcome.INCOMPLETE
    assert result.plan is None


def test_cross_workspace_reference_fails_closed_without_repairing_the_reference() -> None:
    context, registry = _context()
    foreign = context.source_references[0].model_copy(
        update={"workspace_id": UUID(int=99)}
    )

    result = PhysicalReadPlanBuilder(namespace_registry=registry).build(
        context.model_copy(update={"source_references": (foreign,)})
    )

    assert result.outcome is PhysicalReadPlanOutcome.INCOMPLETE
    assert result.reason.value == "fabricated_lineage"
    assert result.plan is None


def test_changed_publication_pin_cannot_mix_with_the_authorized_scope() -> None:
    context, registry = _context()
    changed_pin = context.publication_pin.model_copy(
        update={"pin_digest": "9" * 64}
    )

    result = PhysicalReadPlanBuilder(namespace_registry=registry).build(
        context.model_copy(update={"publication_pin": changed_pin})
    )

    assert result.outcome is PhysicalReadPlanOutcome.CONFLICT
    assert result.reason.value == "invalid_publication_pin"
    assert result.plan is None


def test_forged_publication_pin_identity_fails_closed() -> None:
    context, registry = _context()
    forged_pin = context.publication_pin.model_copy(update={"pin_id": UUID(int=96)})

    result = PhysicalReadPlanBuilder(namespace_registry=registry).build(
        context.model_copy(update={"publication_pin": forged_pin})
    )

    assert result.outcome is PhysicalReadPlanOutcome.CONFLICT
    assert result.reason.value == "invalid_publication_pin"
    assert result.plan is None


def test_fabricated_projection_config_reference_fails_closed() -> None:
    context, registry = _context()
    foreign = context.source_references[0].model_copy(
        update={"projection_config_version_id": UUID(int=98)}
    )

    result = PhysicalReadPlanBuilder(namespace_registry=registry).build(
        context.model_copy(update={"source_references": (foreign,)})
    )

    assert result.outcome is PhysicalReadPlanOutcome.INCOMPLETE
    assert result.plan is None


@pytest.mark.parametrize(
    "bad_member_update",
    (
        {"disposition": ProjectionSnapshotMemberDisposition.TOMBSTONED, "projection_partition_id": None, "projection_receipt_id": None},
        {"source_revision_id": UUID(int=97)},
    ),
)
def test_member_binding_must_remain_included_and_pinned(
    bad_member_update: dict[str, object],
) -> None:
    context, registry = _context()
    member = context.snapshot_members[0].model_copy(update=bad_member_update)

    result = PhysicalReadPlanBuilder(namespace_registry=registry).build(
        context.model_copy(update={"snapshot_members": (member,)})
    )

    assert result.outcome is PhysicalReadPlanOutcome.INCOMPLETE
    assert result.plan is None
