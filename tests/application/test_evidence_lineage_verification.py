from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
from uuid import UUID

import pytest

from spine.application.content_authorization import (
    AuthorizationContextMode,
    AuthorizationDecisionKind,
    AuthorizationEvidence,
    AuthorizationReasonCode,
    ContentAuthorizationDecision,
    ContentAuthorizationDecisionService,
    CurrentContentAuthorizationSnapshot,
    TrustedAuthorizationContext,
)
from spine.application.evidence_lineage_verification import (
    CanonicalEvidenceLineage,
    EvidenceAuthorizationPin,
    EvidenceLineageVerificationService,
    EvidenceVerificationReasonCode,
    RejectedEvidenceItem,
    VerifiedEvidenceItem,
)
from spine.application.projection_locator import canonical_projection_locator_digest
from spine.domain.ingestion import ProcessingCoverage, ProcessingReceipt
from spine.domain.projections import (
    CanonicalAsOf,
    ProjectionActivation,
    ProjectionActivationKind,
    ProjectionConfigVersion,
    ProjectionPartition,
    ProjectionReceipt,
    ProjectionSnapshot,
    ProjectionSnapshotMember,
    ProjectionSnapshotMemberDisposition,
    ProjectionSnapshotState,
    PublishedActivePublication,
    SourceProjectionReference,
    projection_evidence_digest,
    projection_snapshot_manifest_digest,
)
from spine.domain.sources import (
    AccessGrant,
    AccessPermission,
    AccessPolicyVersion,
    CanonicalBoundary,
    CanonicalBoundaryMember,
    CurrentAccessPolicy,
    IdentityMode,
    PolicyOriginKind,
    PrincipalKind,
    RevisionKind,
    RevisionMetadata,
    SourceObject,
    canonical_boundary_manifest_digest,
    content_revision,
)
from spine.domain.sources.original_reference import ObjectReference
from spine.evals.evidence_lineage_v1 import load_evidence_lineage_v1_dataset
from spine.evaluations.evidence_lineage import evaluate_evidence_lineage_outcomes
from spine.memory.contracts import (
    EvidenceReference,
    ProjectionChunk,
    ProjectionEntity,
    ProjectionNamespace,
    PublicationPin,
    RetrievalReadContext,
    StableLocator,
)


NOW = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)
WORKSPACE = UUID(int=1)
ENVIRONMENT = UUID(int=2)
SOURCE = UUID(int=3)
REVISION = UUID(int=4)
PROCESSING_RECEIPT = UUID(int=5)
RUN = UUID(int=6)
CANDIDATE = UUID(int=7)
PARTITION = UUID(int=8)
PROJECTION_RECEIPT = UUID(int=9)
SNAPSHOT = UUID(int=10)
BOUNDARY = UUID(int=11)
CONFIG = UUID(int=12)
ACTIVATION = UUID(int=13)
HUMAN = UUID(int=14)
TRACE = UUID(int=15)
SERVICE = UUID(int=113)


def _digest(schema: str, payload: object) -> str:
    return sha256(
        json.dumps(
            {"schema": schema, "payload": payload},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _locator_digest(locator: StableLocator) -> str:
    return canonical_projection_locator_digest(
        locator_contract_version="locator:v1",
        locator=locator,
    )


def _source() -> SourceObject:
    return SourceObject(
        source_object_id=SOURCE,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        source_kind="document",
        identity_mode=IdentityMode.UPLOAD,
        upload_identity="upload-1",
        created_at=NOW,
    )


def _revision():
    metadata = RevisionMetadata(embedded_title="Synthetic evidence")
    return content_revision(
        revision_id=REVISION,
        source_object_id=SOURCE,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        kind=RevisionKind.CONTENT,
        revision_schema_version="source-revision:v1",
        revision_metadata_schema="revision-metadata:r1-document-v1",
        revision_metadata=metadata,
        original_reference=ObjectReference(
            schema_version=1,
            object_id=UUID(int=16),
            storage_generation=UUID(int=17),
            digest_algorithm="sha256",
            digest_hex="a" * 64,
            byte_length=4,
        ),
        original_sha256="a" * 64,
        byte_length=4,
        media_type="text/plain",
        observed_at=NOW,
    )


def _config() -> ProjectionConfigVersion:
    payload: dict[str, object] = {}
    return ProjectionConfigVersion(
        projection_config_version_id=CONFIG,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        projection_kind="qa",
        version=1,
        ontology_version_id=UUID(int=18),
        parser_contract_version="parser:v1",
        locator_contract_version="locator:v1",
        extraction_contract_version="extraction:v1",
        embedding_profile_version="embedding:v1",
        adapter_contract_version="adapter:v1",
        config_payload=payload,
        config_digest=_digest("projection-config:r1", payload),
        created_at=NOW,
    )


def _lineage() -> tuple[CanonicalEvidenceLineage, ProjectionChunk, PublicationPin]:
    source = _source()
    revision = _revision()
    locator = StableLocator(kind="page", value="page:1:0-4")
    processing_receipt = ProcessingReceipt(
        processing_receipt_id=PROCESSING_RECEIPT,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        ingestion_run_source_id=UUID(int=19),
        binding_id=UUID(int=20),
        source_object_id=SOURCE,
        source_revision_id=REVISION,
        processor_kind="document_parser",
        parser_name="synthetic",
        parser_version="parser:v1",
        parser_config_reference="parser-config:v1",
        locator_contract_version="locator:v1",
        original_sha256="a" * 64,
        output_kind="parsed_document",
        output_reference=UUID(int=21),
        output_digest="b" * 64,
        coverage=ProcessingCoverage.COMPLETE,
        command_identity="process-1",
        command_digest="c" * 64,
        completed_at=NOW,
    )
    projection_receipt = ProjectionReceipt(
        projection_receipt_id=PROJECTION_RECEIPT,
        projection_run_id=RUN,
        projection_candidate_id=CANDIDATE,
        projection_partition_id=PARTITION,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        command_identity="partition-1",
        command_digest="d" * 64,
        artifact_kind="cognee",
        artifact_reference="artifact-1",
        artifact_integrity_digest="e" * 64,
        implementation_schema_version="adapter:v1",
        idempotency_receipt_id=UUID(int=22),
        completed_at=NOW,
    )
    projection_partition = ProjectionPartition(
        projection_partition_id=PARTITION,
        projection_run_id=RUN,
        projection_candidate_id=CANDIDATE,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        security_domain_reference="security-domain:default",
        security_domain_version="security-domain:v1",
        partition_identity="partition-1",
        expected_command_identity=projection_receipt.command_identity,
        expected_command_digest=projection_receipt.command_digest,
        created_at=NOW,
    )
    source_reference = SourceProjectionReference(
        source_projection_reference_id=UUID(int=23),
        projection_run_id=RUN,
        projection_candidate_id=CANDIDATE,
        projection_receipt_id=PROJECTION_RECEIPT,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        source_object_id=SOURCE,
        source_revision_id=REVISION,
        processing_receipt_id=PROCESSING_RECEIPT,
        segment_id="segment-1",
        locator_schema_version="locator:v1",
        locator_reference=locator.value,
        locator_digest=_locator_digest(locator),
        projected_kind="chunk",
        projected_object_id="chunk-1",
        projection_config_version_id=CONFIG,
        security_domain_reference="security-domain:default",
        created_at=NOW,
    )
    boundary_member = CanonicalBoundaryMember(
        boundary_id=BOUNDARY,
        source_object_id=SOURCE,
        source_revision_id=REVISION,
        acceptance_id=UUID(int=24),
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        revision_kind=RevisionKind.CONTENT,
        accepted_generation=1,
    )
    boundary = CanonicalBoundary(
        boundary_id=BOUNDARY,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        projection_kind="qa",
        scope_version="r1",
        accepted_through_generation=1,
        boundary_time=NOW,
        manifest_digest="0" * 64,
        idempotency_receipt_id=UUID(int=25),
    )
    boundary = boundary.model_copy(
        update={
            "manifest_digest": canonical_boundary_manifest_digest(
                boundary, (boundary_member,)
            )
        }
    )
    snapshot_member = ProjectionSnapshotMember(
        projection_snapshot_member_id=UUID(int=26),
        projection_snapshot_id=SNAPSHOT,
        canonical_boundary_id=BOUNDARY,
        canonical_boundary_member_id=SOURCE,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        source_object_id=SOURCE,
        source_revision_id=REVISION,
        disposition=ProjectionSnapshotMemberDisposition.INCLUDED,
        projection_partition_id=PARTITION,
        projection_receipt_id=PROJECTION_RECEIPT,
    )
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
        candidate_evidence_digest=projection_evidence_digest(
            partitions=(projection_partition,),
            receipts=(projection_receipt,),
            references=(source_reference,),
        ),
        manifest_digest="0" * 64,
        member_count=1,
        created_at=NOW,
    )
    snapshot = snapshot.model_copy(
        update={
            "manifest_digest": projection_snapshot_manifest_digest(
                snapshot, (snapshot_member,)
            )
        }
    )
    activation = ProjectionActivation(
        projection_activation_id=ACTIVATION,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        projection_kind="qa",
        projection_snapshot_id=SNAPSHOT,
        activation_generation=2,
        activation_kind=ProjectionActivationKind.PUBLISH,
        command_identity="activate-1",
        command_digest="1" * 64,
        idempotency_receipt_id=UUID(int=28),
        audit_event_id=UUID(int=29),
        activated_at=NOW,
    )
    config = _config()
    pin = PublicationPin.from_publication(
        PublishedActivePublication(
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            projection_kind="qa",
            projection_snapshot=snapshot,
            projection_activation=activation,
            activation_generation=2,
            active_pointer_cas_version=1,
            canonical_boundary_id=BOUNDARY,
            canonical_boundary_manifest_digest=boundary.manifest_digest,
            accepted_through_generation=1,
            canonical_boundary_membership_ref="canonical-boundary-members:r1",
            as_of=CanonicalAsOf(
                canonical_boundary_id=BOUNDARY,
                accepted_through_generation=1,
                boundary_time=NOW,
            ),
            projection_config_version=config,
            freshness="current",
            health="healthy",
        )
    )
    namespace = ProjectionNamespace(
        namespace="spine/qa/active",
        namespace_digest="2" * 64,
        projection_kind="qa",
        state="active",
    )
    read_context = RetrievalReadContext(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        publication_pin_id=pin.pin_id,
        publication_pin_digest=pin.pin_digest,
        projection_snapshot_id=SNAPSHOT,
        projection_activation_id=ACTIVATION,
        activation_generation=2,
        canonical_boundary_id=BOUNDARY,
        canonical_boundary_digest=boundary.manifest_digest,
        projection_run_id=RUN,
        projection_candidate_id=CANDIDATE,
        projection_partition_id=PARTITION,
        projection_receipt_id=PROJECTION_RECEIPT,
        physical_namespace=namespace.namespace,
        physical_namespace_digest=namespace.namespace_digest,
        source_object_ids=(SOURCE,),
        source_revision_ids=(REVISION,),
        provider_capability_reference="source-filter:v1",
        source_isolation_pre_retrieval_enforced=True,
    )
    evidence = EvidenceReference(
        evidence_id="evidence-1",
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        source_object_id=SOURCE,
        source_revision_id=REVISION,
        revision_digest=revision.revision_digest,
        processing_receipt_id=PROCESSING_RECEIPT,
        locator_contract_version="locator:v1",
        locator=locator,
        evidence_digest="3" * 64,
    )
    item = ProjectionChunk(
        chunk_id="chunk-1",
        text="Synthetic authorized evidence",
        evidence=(evidence,),
        read_context=read_context,
        read_contexts=(read_context,),
    )
    lineage = CanonicalEvidenceLineage(
        evidence_id=evidence.evidence_id,
        evidence_digest=evidence.evidence_digest,
        authorization_pin=EvidenceAuthorizationPin(
            source_object_id=SOURCE,
            read_context=read_context,
            policy_generation=1,
            membership_generation=1,
            authority_generation=1,
            purpose="answer_question",
            operation="retrieve_context",
            decision_reference=UUID(int=37),
        ),
        stable_locator=locator,
        source_object=source,
        source_revision=revision,
        processing_receipt=processing_receipt,
        source_projection_reference=source_reference,
        projection_receipt=projection_receipt,
        projection_partitions=(projection_partition,),
        projection_receipts=(projection_receipt,),
        source_projection_references=(source_reference,),
        projection_run_transitions=(),
        projection_validations=(),
        projection_snapshot=snapshot,
        projection_snapshot_members=(snapshot_member,),
        canonical_boundary=boundary,
        canonical_boundary_members=(boundary_member,),
        projection_activation=activation,
        projection_config_version=config,
        projection_namespace=namespace,
    )
    return lineage, item, pin


def _multi_source_lineage() -> tuple[
    tuple[CanonicalEvidenceLineage, CanonicalEvidenceLineage],
    ProjectionEntity,
    PublicationPin,
]:
    first, chunk, original_pin = _lineage()
    second_source_id = UUID(int=103)
    second_revision_id = UUID(int=104)
    second_processing_receipt_id = UUID(int=105)
    first_boundary_member = first.canonical_boundary_members[0]
    second_boundary_member = first_boundary_member.model_copy(
        update={
            "source_object_id": second_source_id,
            "source_revision_id": second_revision_id,
            "acceptance_id": UUID(int=106),
        }
    )
    boundary_members = (first_boundary_member, second_boundary_member)
    boundary = first.canonical_boundary.model_copy(update={"manifest_digest": "0" * 64})
    boundary = boundary.model_copy(
        update={
            "manifest_digest": canonical_boundary_manifest_digest(
                boundary, boundary_members
            )
        }
    )
    first_snapshot_member = first.projection_snapshot_members[0]
    second_snapshot_member = first_snapshot_member.model_copy(
        update={
            "projection_snapshot_member_id": UUID(int=107),
            "canonical_boundary_member_id": second_source_id,
            "source_object_id": second_source_id,
            "source_revision_id": second_revision_id,
        }
    )
    snapshot_members = (first_snapshot_member, second_snapshot_member)
    first_reference = first.source_projection_reference.model_copy(
        update={"projected_kind": "entity", "projected_object_id": "entity-1"}
    )
    second_reference = first_reference.model_copy(
        update={
            "source_projection_reference_id": UUID(int=109),
            "source_object_id": second_source_id,
            "source_revision_id": second_revision_id,
            "processing_receipt_id": second_processing_receipt_id,
        }
    )
    candidate_references = (first_reference, second_reference)
    snapshot = first.projection_snapshot.model_copy(
        update={
            "member_count": 2,
            "candidate_evidence_digest": projection_evidence_digest(
                partitions=first.projection_partitions,
                receipts=first.projection_receipts,
                references=candidate_references,
            ),
            "manifest_digest": "0" * 64,
        }
    )
    snapshot = snapshot.model_copy(
        update={
            "manifest_digest": projection_snapshot_manifest_digest(
                snapshot, snapshot_members
            )
        }
    )
    activation = first.projection_activation
    pin = PublicationPin.from_publication(
        PublishedActivePublication(
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            projection_kind="qa",
            projection_snapshot=snapshot,
            projection_activation=activation,
            activation_generation=activation.activation_generation,
            active_pointer_cas_version=original_pin.active_pointer_cas_version,
            canonical_boundary_id=BOUNDARY,
            canonical_boundary_manifest_digest=boundary.manifest_digest,
            accepted_through_generation=boundary.accepted_through_generation,
            canonical_boundary_membership_ref="canonical-boundary-members:r1",
            as_of=CanonicalAsOf(
                canonical_boundary_id=BOUNDARY,
                accepted_through_generation=boundary.accepted_through_generation,
                boundary_time=NOW,
            ),
            projection_config_version=first.projection_config_version,
            freshness="current",
            health="healthy",
        )
    )
    context = chunk.read_contexts[0].model_copy(
        update={
            "publication_pin_id": pin.pin_id,
            "publication_pin_digest": pin.pin_digest,
            "canonical_boundary_digest": boundary.manifest_digest,
            "source_object_ids": (SOURCE, second_source_id),
            "source_revision_ids": (REVISION, second_revision_id),
        }
    )
    first_evidence = chunk.evidence[0]
    second_revision = first.source_revision.model_copy(
        update={
            "revision_id": second_revision_id,
            "source_object_id": second_source_id,
        }
    )
    second_evidence = first_evidence.model_copy(
        update={
            "evidence_id": "evidence-2",
            "source_object_id": second_source_id,
            "source_revision_id": second_revision_id,
            "revision_digest": second_revision.revision_digest,
            "processing_receipt_id": second_processing_receipt_id,
            "evidence_digest": "6" * 64,
        }
    )
    entity = ProjectionEntity(
        entity_id="entity-1",
        kind="customer",
        name="Synthetic customer",
        evidence=(first_evidence, second_evidence),
        read_context=context,
        read_contexts=(context,),
    )
    shared_updates = {
        "projection_snapshot": snapshot,
        "projection_snapshot_members": snapshot_members,
        "source_projection_references": candidate_references,
        "canonical_boundary": boundary,
        "canonical_boundary_members": boundary_members,
    }
    first = first.model_copy(
        update={
            **shared_updates,
            "authorization_pin": first.authorization_pin.model_copy(
                update={"read_context": context}
            ),
            "source_projection_reference": first_reference,
        }
    )
    second = first.model_copy(
        update={
            **shared_updates,
            "evidence_id": second_evidence.evidence_id,
            "evidence_digest": second_evidence.evidence_digest,
            "authorization_pin": first.authorization_pin.model_copy(
                update={
                    "source_object_id": second_source_id,
                    "read_context": context,
                    "decision_reference": UUID(int=37),
                }
            ),
            "source_object": first.source_object.model_copy(
                update={
                    "source_object_id": second_source_id,
                    "upload_identity": "upload-2",
                }
            ),
            "source_revision": second_revision,
            "processing_receipt": first.processing_receipt.model_copy(
                update={
                    "processing_receipt_id": second_processing_receipt_id,
                    "source_object_id": second_source_id,
                    "source_revision_id": second_revision_id,
                }
            ),
            "source_projection_reference": second_reference,
        }
    )
    return (first, second), entity, pin


class _LineageRepository:
    def __init__(
        self,
        lineage: CanonicalEvidenceLineage | tuple[CanonicalEvidenceLineage, ...],
    ) -> None:
        self.lineages = lineage if isinstance(lineage, tuple) else (lineage,)
        unique_lineages = tuple(
            {
                value.source_object.source_object_id: value
                for value in self.lineages
            }.values()
        )
        if unique_lineages:
            first_pin = unique_lineages[0].authorization_pin
            decision_kind = (
                AuthorizationDecisionKind.INTERACTIVE_PROCESSING
                if first_pin.service_authority_generation is not None
                else AuthorizationDecisionKind.HUMAN_DISCLOSURE
            )
            self.authorization_decision = ContentAuthorizationDecision(
                allowed=True,
                decision_kind=decision_kind,
                workspace_id=WORKSPACE,
                environment_id=ENVIRONMENT,
                source_object_ids=tuple(
                    value.source_object.source_object_id for value in unique_lineages
                ),
                policy_evidence=tuple(
                    AuthorizationEvidence(
                        source_object_id=value.source_object.source_object_id,
                        source_revision_id=value.source_revision.revision_id,
                        policy_version_id=UUID(int=200 + index),
                        source_revision_observed_at=value.source_revision.observed_at,
                        retention_eligible=True,
                        policy_generation=value.authorization_pin.policy_generation,
                    )
                    for index, value in enumerate(unique_lineages)
                ),
                membership_generation=first_pin.membership_generation,
                authority_generation=first_pin.authority_generation,
                service_authority_generation=first_pin.service_authority_generation,
                authority_snapshot_reference=UUID(int=34),
                purpose=first_pin.purpose,
                operation=first_pin.operation,
                reason_code=AuthorizationReasonCode.ALLOWED,
                decision_reference=UUID(int=37),
                trace_id=TRACE,
                evaluated_at=NOW,
            )
        else:
            self.authorization_decision = None

    async def resolve_item(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        projection_snapshot_id: UUID,
        projected_kind: str,
        projected_object_id: str,
    ) -> tuple[CanonicalEvidenceLineage, ...]:
        if (workspace_id, environment_id) != (WORKSPACE, ENVIRONMENT):
            return ()
        return tuple(
            lineage
            for lineage in self.lineages
            if lineage.projection_snapshot.projection_snapshot_id
            == projection_snapshot_id
            and lineage.source_projection_reference.projected_kind == projected_kind
            and lineage.source_projection_reference.projected_object_id
            == projected_object_id
        )

    async def resolve_authorization_decision(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        decision_reference: UUID,
    ) -> ContentAuthorizationDecision | None:
        if (
            (workspace_id, environment_id) == (WORKSPACE, ENVIRONMENT)
            and decision_reference == UUID(int=37)
        ):
            return self.authorization_decision
        return None


class _FailingLineageRepository:
    async def resolve_item(self, **_: object) -> tuple[CanonicalEvidenceLineage, ...]:
        raise RuntimeError("protected-source-name must never escape")


class _FailureObserver:
    def __init__(self) -> None:
        self.observations: list[object] = []

    async def record_failure(self, observation: object) -> UUID:
        self.observations.append(observation)
        return UUID(int=123)


class _IdentityAuthority:
    def __init__(self, current: TrustedAuthorizationContext | None = None) -> None:
        self.current = current

    async def revalidate(
        self, context: TrustedAuthorizationContext
    ) -> TrustedAuthorizationContext:
        return self.current or context


class _AuthorizationRepository:
    def __init__(
        self,
        revision_id: UUID,
        *,
        policy_generation: int = 1,
        grant_content: bool = True,
        revision_kind: RevisionKind = RevisionKind.CONTENT,
        retention_eligible: bool = True,
    ) -> None:
        grants = (
            AccessGrant(
                access_grant_id=UUID(int=30),
                principal_kind=PrincipalKind.HUMAN,
                principal_id=HUMAN,
                permission=AccessPermission.READ_CONTENT,
            ),
        ) if grant_content else ()
        policy = AccessPolicyVersion(
            access_policy_version_id=UUID(int=31),
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            source_object_id=SOURCE,
            version=1,
            predecessor_policy_version_id=None,
            document_schema_version="access-policy.v1",
            document_hash="4" * 64,
            origin_kind=PolicyOriginKind.APPROVED_PROPOSAL,
            origin_reference=UUID(int=32),
            origin_version=1,
            origin_hash="5" * 64,
            created_by_canonical_human_id=HUMAN,
            created_at=NOW,
            grants=grants,
        )
        self.snapshot = CurrentContentAuthorizationSnapshot(
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            source_object_id=SOURCE,
            current_revision_id=revision_id,
            current_revision_kind=revision_kind,
            current_revision_observed_at=NOW,
            retention_eligible=retention_eligible,
            provenance_complete=True,
            current_policy=CurrentAccessPolicy(
                workspace_id=WORKSPACE,
                environment_id=ENVIRONMENT,
                source_object_id=SOURCE,
                access_policy_version_id=policy.access_policy_version_id,
                policy_activation_id=UUID(int=33),
                policy_generation=policy_generation,
                cas_version=1,
            ),
            policy=policy,
        )

    async def resolve_current_content_authorization(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        source_object_id: UUID,
    ) -> CurrentContentAuthorizationSnapshot | None:
        if (workspace_id, environment_id, source_object_id) != (
            WORKSPACE,
            ENVIRONMENT,
            SOURCE,
        ):
            return None
        return self.snapshot


class _MultiAuthorizationRepository:
    def __init__(self, *, grant_second_source: bool) -> None:
        first = _AuthorizationRepository(REVISION).snapshot
        assert first.policy is not None
        assert first.current_policy is not None
        second_source_id = UUID(int=103)
        second_policy_id = UUID(int=111)
        grants = first.policy.grants if grant_second_source else ()
        second_policy = first.policy.model_copy(
            update={
                "access_policy_version_id": second_policy_id,
                "source_object_id": second_source_id,
                "grants": grants,
            }
        )
        second = first.model_copy(
            update={
                "source_object_id": second_source_id,
                "current_revision_id": UUID(int=104),
                "current_policy": first.current_policy.model_copy(
                    update={
                        "source_object_id": second_source_id,
                        "access_policy_version_id": second_policy_id,
                        "policy_activation_id": UUID(int=112),
                    }
                ),
                "policy": second_policy,
            }
        )
        self.snapshots = {SOURCE: first, second_source_id: second}

    async def resolve_current_content_authorization(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        source_object_id: UUID,
    ) -> CurrentContentAuthorizationSnapshot | None:
        if (workspace_id, environment_id) != (WORKSPACE, ENVIRONMENT):
            return None
        return self.snapshots.get(source_object_id)


class _InteractiveAuthorizationRepository:
    def __init__(self, *, valid_until: datetime) -> None:
        base = _AuthorizationRepository(REVISION).snapshot
        assert base.policy is not None
        service_grant = AccessGrant(
            access_grant_id=UUID(int=114),
            principal_kind=PrincipalKind.SERVICE,
            principal_id=SERVICE,
            permission=AccessPermission.PROCESS_CONTENT,
            purpose="answer_question",
            operation="retrieve_context",
            valid_from=NOW - timedelta(minutes=1),
            valid_until=valid_until,
        )
        self.snapshot = base.model_copy(
            update={
                "policy": base.policy.model_copy(
                    update={"grants": (*base.policy.grants, service_grant)}
                )
            }
        )

    async def resolve_current_content_authorization(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        source_object_id: UUID,
    ) -> CurrentContentAuthorizationSnapshot | None:
        if (workspace_id, environment_id, source_object_id) != (
            WORKSPACE,
            ENVIRONMENT,
            SOURCE,
        ):
            return None
        return self.snapshot


def _authorization_context() -> TrustedAuthorizationContext:
    return TrustedAuthorizationContext(
        mode=AuthorizationContextMode.HUMAN_DISCLOSURE,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        canonical_human_id=HUMAN,
        purpose="answer_question",
        operation="retrieve_context",
        membership_generation=1,
        authority_generation=1,
        authority_snapshot_reference=UUID(int=34),
        trace_id=TRACE,
        issued_at=NOW,
    )


def _interactive_authorization_context() -> TrustedAuthorizationContext:
    return _authorization_context().model_copy(
        update={
            "mode": AuthorizationContextMode.INTERACTIVE_PROCESSING,
            "service_principal_id": SERVICE,
            "service_authority_generation": 1,
        }
    )


def _verifier(
    lineage: CanonicalEvidenceLineage | tuple[CanonicalEvidenceLineage, ...],
    authorization_repository: object,
    *,
    identity_authority: _IdentityAuthority | None = None,
) -> EvidenceLineageVerificationService:
    authorization = ContentAuthorizationDecisionService(
        repository=authorization_repository,
        identity_authority=identity_authority or _IdentityAuthority(),
        clock=lambda: NOW,
        decision_reference_factory=lambda: UUID(int=35),
    )
    return EvidenceLineageVerificationService(
        repository=_LineageRepository(lineage),
        authorization=authorization,
        failure_observer=_FailureObserver(),
        decision_reference_factory=lambda: UUID(int=36),
    )


@pytest.mark.asyncio
async def test_complete_authorized_lineage_matching_attempt_pin_is_verified() -> None:
    lineage, item, pin = _lineage()

    result = await _verifier(lineage, _AuthorizationRepository(REVISION)).verify(
        item=item,
        publication_pin=pin,
        authorization_context=_authorization_context(),
    )

    assert isinstance(result, VerifiedEvidenceItem)
    assert result.item == item
    assert result.publication_pin_id == pin.pin_id
    assert result.authorization_decision_reference == UUID(int=35)


@pytest.mark.asyncio
async def test_changed_policy_generation_after_pinned_read_is_rejected() -> None:
    lineage, item, pin = _lineage()

    result = await _verifier(
        lineage,
        _AuthorizationRepository(REVISION, policy_generation=2),
    ).verify(
        item=item,
        publication_pin=pin,
        authorization_context=_authorization_context(),
    )

    assert result.status == "rejected"
    assert result.reason_code.value == "authority_stale"


@pytest.mark.asyncio
@pytest.mark.parametrize("generation", ["membership_generation", "authority_generation"])
async def test_changed_identity_generation_is_rejected_before_source_resolution(
    generation: str,
) -> None:
    lineage, item, pin = _lineage()
    original_context = _authorization_context()
    current_context = original_context.model_copy(
        update={generation: getattr(original_context, generation) + 1}
    )
    verifier = _verifier(
        lineage,
        _AuthorizationRepository(REVISION),
        identity_authority=_IdentityAuthority(current_context),
    )

    result = await verifier.verify(
        item=item,
        publication_pin=pin,
        authorization_context=original_context,
    )

    assert isinstance(result, RejectedEvidenceItem)
    assert result.reason_code is EvidenceVerificationReasonCode.AUTHORITY_STALE


@pytest.mark.asyncio
async def test_human_with_admin_role_but_without_content_grant_is_rejected_safely() -> None:
    lineage, item, pin = _lineage()
    verifier = _verifier(
        lineage,
        _AuthorizationRepository(REVISION, grant_content=False),
    )

    result = await verifier.verify(
        item=item,
        publication_pin=pin,
        authorization_context=_authorization_context(),
    )

    assert isinstance(result, RejectedEvidenceItem)
    assert result.reason_code is EvidenceVerificationReasonCode.AUTHORIZATION_DENIED
    serialized = result.model_dump_json()
    assert item.text not in serialized
    assert str(SOURCE) not in serialized
    assert "source" not in serialized
    assert "count" not in serialized


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    ["missing", "stale", "tombstoned", "ambiguous", "incomplete"],
)
async def test_non_current_provider_evidence_status_is_rejected(status: str) -> None:
    lineage, item, pin = _lineage()
    evidence = item.evidence[0].model_copy(update={"status": status})
    unsafe_item = item.model_copy(update={"evidence": (evidence,)})

    result = await _verifier(
        lineage, _AuthorizationRepository(REVISION)
    ).verify(
        item=unsafe_item,
        publication_pin=pin,
        authorization_context=_authorization_context(),
    )

    assert isinstance(result, RejectedEvidenceItem)
    assert result.reason_code is EvidenceVerificationReasonCode.EVIDENCE_UNVERIFIED


@pytest.mark.asyncio
@pytest.mark.parametrize("candidate_count", [0, 2])
async def test_missing_or_ambiguous_canonical_lineage_is_rejected(
    candidate_count: int,
) -> None:
    lineage, item, pin = _lineage()
    candidates = tuple(lineage for _ in range(candidate_count))

    result = await _verifier(
        candidates, _AuthorizationRepository(REVISION)
    ).verify(
        item=item,
        publication_pin=pin,
        authorization_context=_authorization_context(),
    )

    assert isinstance(result, RejectedEvidenceItem)
    assert result.reason_code is EvidenceVerificationReasonCode.EVIDENCE_UNVERIFIED


@pytest.mark.asyncio
@pytest.mark.parametrize("scope_field", ["workspace_id", "environment_id"])
async def test_evidence_from_another_tenant_scope_is_rejected(
    scope_field: str,
) -> None:
    lineage, item, pin = _lineage()
    foreign_evidence = item.evidence[0].model_copy(
        update={scope_field: UUID(int=100)}
    )
    unsafe_item = item.model_copy(update={"evidence": (foreign_evidence,)})

    result = await _verifier(
        lineage, _AuthorizationRepository(REVISION)
    ).verify(
        item=unsafe_item,
        publication_pin=pin,
        authorization_context=_authorization_context(),
    )

    assert isinstance(result, RejectedEvidenceItem)
    assert result.reason_code is EvidenceVerificationReasonCode.EVIDENCE_UNVERIFIED


@pytest.mark.asyncio
async def test_wrong_activation_generation_is_rejected() -> None:
    lineage, item, pin = _lineage()
    context = item.read_contexts[0].model_copy(update={"activation_generation": 3})
    unsafe_item = item.model_copy(
        update={"read_context": context, "read_contexts": (context,)}
    )

    result = await _verifier(
        lineage, _AuthorizationRepository(REVISION)
    ).verify(
        item=unsafe_item,
        publication_pin=pin,
        authorization_context=_authorization_context(),
    )

    assert isinstance(result, RejectedEvidenceItem)
    assert result.reason_code is EvidenceVerificationReasonCode.EVIDENCE_UNVERIFIED


@pytest.mark.asyncio
async def test_shadow_namespace_is_rejected() -> None:
    lineage, item, pin = _lineage()
    shadow = lineage.model_copy(
        update={
            "projection_namespace": lineage.projection_namespace.model_copy(
                update={"state": "shadow"}
            )
        }
    )

    result = await _verifier(
        shadow, _AuthorizationRepository(REVISION)
    ).verify(
        item=item,
        publication_pin=pin,
        authorization_context=_authorization_context(),
    )

    assert isinstance(result, RejectedEvidenceItem)
    assert result.reason_code is EvidenceVerificationReasonCode.EVIDENCE_UNVERIFIED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("current_revision_id", "revision_kind", "retention_eligible"),
    [
        pytest.param(UUID(int=101), RevisionKind.CONTENT, True, id="superseded"),
        pytest.param(REVISION, RevisionKind.TOMBSTONE, True, id="tombstoned"),
        pytest.param(REVISION, RevisionKind.CONTENT, False, id="retention-ineligible"),
    ],
)
async def test_ineligible_revision_is_rejected(
    current_revision_id: UUID,
    revision_kind: RevisionKind,
    retention_eligible: bool,
) -> None:
    lineage, item, pin = _lineage()
    repository = _AuthorizationRepository(
        current_revision_id,
        revision_kind=revision_kind,
        retention_eligible=retention_eligible,
    )

    result = await _verifier(lineage, repository).verify(
        item=item,
        publication_pin=pin,
        authorization_context=_authorization_context(),
    )

    assert isinstance(result, RejectedEvidenceItem)
    assert result.reason_code is EvidenceVerificationReasonCode.EVIDENCE_UNVERIFIED


@pytest.mark.asyncio
async def test_fabricated_evidence_digest_is_rejected() -> None:
    lineage, item, pin = _lineage()
    forged = item.evidence[0].model_copy(update={"evidence_digest": "9" * 64})
    unsafe_item = item.model_copy(update={"evidence": (forged,)})

    result = await _verifier(
        lineage, _AuthorizationRepository(REVISION)
    ).verify(
        item=unsafe_item,
        publication_pin=pin,
        authorization_context=_authorization_context(),
    )

    assert isinstance(result, RejectedEvidenceItem)
    assert result.reason_code is EvidenceVerificationReasonCode.EVIDENCE_UNVERIFIED


@pytest.mark.asyncio
async def test_mixed_access_multi_source_entity_is_withheld_as_a_whole() -> None:
    lineages, entity, pin = _multi_source_lineage()
    verifier = _verifier(
        lineages,
        _MultiAuthorizationRepository(grant_second_source=False),
    )

    result = await verifier.verify(
        item=entity,
        publication_pin=pin,
        authorization_context=_authorization_context(),
    )

    assert isinstance(result, RejectedEvidenceItem)
    assert result.reason_code is EvidenceVerificationReasonCode.AUTHORIZATION_DENIED
    serialized = result.model_dump_json()
    assert entity.name not in serialized
    assert entity.evidence[0].evidence_id not in serialized
    assert entity.evidence[1].evidence_id not in serialized


@pytest.mark.asyncio
async def test_all_authorized_multi_source_entity_is_verified_without_dropping_contributors() -> None:
    lineages, entity, pin = _multi_source_lineage()
    verifier = _verifier(
        lineages,
        _MultiAuthorizationRepository(grant_second_source=True),
    )

    result = await verifier.verify(
        item=entity,
        publication_pin=pin,
        authorization_context=_authorization_context(),
    )

    assert isinstance(result, VerifiedEvidenceItem)
    assert result.item.evidence == entity.evidence


@pytest.mark.asyncio
async def test_provider_omitting_one_canonical_contributor_is_rejected() -> None:
    lineages, entity, pin = _multi_source_lineage()
    incomplete_entity = entity.model_copy(update={"evidence": (entity.evidence[0],)})
    verifier = _verifier(
        lineages,
        _MultiAuthorizationRepository(grant_second_source=True),
    )

    result = await verifier.verify(
        item=incomplete_entity,
        publication_pin=pin,
        authorization_context=_authorization_context(),
    )

    assert isinstance(result, RejectedEvidenceItem)
    assert result.reason_code is EvidenceVerificationReasonCode.EVIDENCE_UNVERIFIED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("valid_until", "expected_status"),
    [
        (NOW + timedelta(minutes=1), "verified"),
        (NOW, "rejected"),
    ],
)
async def test_interactive_processing_rechecks_service_grant_validity(
    valid_until: datetime,
    expected_status: str,
) -> None:
    lineage, item, pin = _lineage()
    context = _interactive_authorization_context()
    lineage = lineage.model_copy(
        update={
            "authorization_pin": lineage.authorization_pin.model_copy(
                update={"service_authority_generation": 1}
            )
        }
    )
    verifier = _verifier(
        lineage,
        _InteractiveAuthorizationRepository(valid_until=valid_until),
    )

    result = await verifier.verify(
        item=item,
        publication_pin=pin,
        authorization_context=context,
    )

    assert result.status == expected_status


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["purpose", "operation"])
async def test_purpose_or_operation_change_after_pinned_read_is_rejected(
    field: str,
) -> None:
    lineage, item, pin = _lineage()
    stale = lineage.model_copy(
        update={
            "authorization_pin": lineage.authorization_pin.model_copy(
                update={field: "different_authority"}
            )
        }
    )

    result = await _verifier(stale, _AuthorizationRepository(REVISION)).verify(
        item=item,
        publication_pin=pin,
        authorization_context=_authorization_context(),
    )

    assert isinstance(result, RejectedEvidenceItem)
    assert result.reason_code is EvidenceVerificationReasonCode.AUTHORITY_STALE


@pytest.mark.asyncio
async def test_missing_pinned_read_context_is_rejected() -> None:
    lineage, item, pin = _lineage()
    unsafe_item = item.model_copy(update={"read_context": None, "read_contexts": ()})

    result = await _verifier(
        lineage, _AuthorizationRepository(REVISION)
    ).verify(
        item=unsafe_item,
        publication_pin=pin,
        authorization_context=_authorization_context(),
    )

    assert isinstance(result, RejectedEvidenceItem)
    assert result.reason_code is EvidenceVerificationReasonCode.EVIDENCE_UNVERIFIED


@pytest.mark.asyncio
async def test_repository_failure_returns_retryable_safe_diagnostic() -> None:
    _, item, pin = _lineage()
    authorization = ContentAuthorizationDecisionService(
        repository=_AuthorizationRepository(REVISION),
        identity_authority=_IdentityAuthority(),
        clock=lambda: NOW,
        decision_reference_factory=lambda: UUID(int=35),
    )
    observer = _FailureObserver()
    verifier = EvidenceLineageVerificationService(
        repository=_FailingLineageRepository(),
        authorization=authorization,
        failure_observer=observer,
        decision_reference_factory=lambda: UUID(int=36),
    )

    result = await verifier.verify(
        item=item,
        publication_pin=pin,
        authorization_context=_authorization_context(),
    )

    assert isinstance(result, RejectedEvidenceItem)
    assert result.reason_code is EvidenceVerificationReasonCode.VERIFICATION_UNAVAILABLE
    assert result.diagnostic.retryable is True
    assert result.diagnostic.opaque_reference == UUID(int=123)
    assert len(observer.observations) == 1
    assert "protected-source-name" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_verification_is_deterministic_with_injected_decision_identity() -> None:
    lineage, item, pin = _lineage()
    verifier = _verifier(lineage, _AuthorizationRepository(REVISION))

    first = await verifier.verify(
        item=item,
        publication_pin=pin,
        authorization_context=_authorization_context(),
    )
    second = await verifier.verify(
        item=item,
        publication_pin=pin,
        authorization_context=_authorization_context(),
    )

    assert first == second


@pytest.mark.asyncio
@pytest.mark.parametrize("member_kind", ["boundary", "snapshot"])
@pytest.mark.parametrize("scope_field", ["workspace_id", "environment_id"])
async def test_foreign_scoped_canonical_member_is_rejected(
    member_kind: str,
    scope_field: str,
) -> None:
    lineage, item, pin = _lineage()
    if member_kind == "boundary":
        members = (
            lineage.canonical_boundary_members[0].model_copy(
                update={scope_field: UUID(int=120)}
            ),
        )
        unsafe = lineage.model_copy(update={"canonical_boundary_members": members})
    else:
        members = (
            lineage.projection_snapshot_members[0].model_copy(
                update={scope_field: UUID(int=120)}
            ),
        )
        unsafe = lineage.model_copy(update={"projection_snapshot_members": members})

    result = await _verifier(unsafe, _AuthorizationRepository(REVISION)).verify(
        item=item,
        publication_pin=pin,
        authorization_context=_authorization_context(),
    )

    assert isinstance(result, RejectedEvidenceItem)
    assert result.reason_code is EvidenceVerificationReasonCode.EVIDENCE_UNVERIFIED


@pytest.mark.asyncio
async def test_snapshot_member_must_link_to_exact_boundary_member() -> None:
    lineage, item, pin = _lineage()
    member = lineage.projection_snapshot_members[0].model_copy(
        update={"canonical_boundary_member_id": UUID(int=121)}
    )
    snapshot = lineage.projection_snapshot.model_copy(update={"manifest_digest": "0" * 64})
    snapshot = snapshot.model_copy(
        update={
            "manifest_digest": projection_snapshot_manifest_digest(snapshot, (member,))
        }
    )
    unsafe = lineage.model_copy(
        update={"projection_snapshot": snapshot, "projection_snapshot_members": (member,)}
    )
    publication = PublishedActivePublication(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        projection_kind="qa",
        projection_snapshot=snapshot,
        projection_activation=lineage.projection_activation,
        activation_generation=lineage.projection_activation.activation_generation,
        active_pointer_cas_version=pin.active_pointer_cas_version,
        canonical_boundary_id=BOUNDARY,
        canonical_boundary_manifest_digest=lineage.canonical_boundary.manifest_digest,
        accepted_through_generation=lineage.canonical_boundary.accepted_through_generation,
        canonical_boundary_membership_ref="canonical-boundary-members:r1",
        as_of=pin.as_of,
        projection_config_version=lineage.projection_config_version,
        freshness="current",
        health="healthy",
    )
    changed_pin = PublicationPin.from_publication(publication)
    context = item.read_contexts[0].model_copy(
        update={
            "publication_pin_id": changed_pin.pin_id,
            "publication_pin_digest": changed_pin.pin_digest,
        }
    )
    unsafe = unsafe.model_copy(
        update={
            "authorization_pin": unsafe.authorization_pin.model_copy(
                update={"read_context": context}
            )
        }
    )
    unsafe_item = item.model_copy(
        update={"read_context": context, "read_contexts": (context,)}
    )

    result = await _verifier(unsafe, _AuthorizationRepository(REVISION)).verify(
        item=unsafe_item,
        publication_pin=changed_pin,
        authorization_context=_authorization_context(),
    )

    assert isinstance(result, RejectedEvidenceItem)


@pytest.mark.asyncio
async def test_processing_receipt_for_different_original_bytes_is_rejected() -> None:
    lineage, item, pin = _lineage()
    unsafe = lineage.model_copy(
        update={
            "processing_receipt": lineage.processing_receipt.model_copy(
                update={"original_sha256": "7" * 64}
            )
        }
    )

    result = await _verifier(unsafe, _AuthorizationRepository(REVISION)).verify(
        item=item,
        publication_pin=pin,
        authorization_context=_authorization_context(),
    )

    assert isinstance(result, RejectedEvidenceItem)


@pytest.mark.asyncio
async def test_format_specific_locator_contract_is_verified_against_source_lineage() -> None:
    lineage, item, pin = _lineage()
    config = lineage.projection_config_version.model_copy(
        update={"locator_contract_version": "locator:v2"}
    )
    publication = PublishedActivePublication(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        projection_kind="qa",
        projection_snapshot=lineage.projection_snapshot,
        projection_activation=lineage.projection_activation,
        activation_generation=lineage.projection_activation.activation_generation,
        active_pointer_cas_version=pin.active_pointer_cas_version,
        canonical_boundary_id=BOUNDARY,
        canonical_boundary_manifest_digest=lineage.canonical_boundary.manifest_digest,
        accepted_through_generation=lineage.canonical_boundary.accepted_through_generation,
        canonical_boundary_membership_ref="canonical-boundary-members:r1",
        as_of=pin.as_of,
        projection_config_version=config,
        freshness="current",
        health="healthy",
    )
    changed_pin = PublicationPin.from_publication(publication)
    context = item.read_contexts[0].model_copy(
        update={
            "publication_pin_id": changed_pin.pin_id,
            "publication_pin_digest": changed_pin.pin_digest,
        }
    )
    format_specific_lineage = lineage.model_copy(
        update={
            "projection_config_version": config,
            "authorization_pin": lineage.authorization_pin.model_copy(
                update={"read_context": context}
            ),
        }
    )
    format_specific_item = item.model_copy(
        update={"read_context": context, "read_contexts": (context,)}
    )

    result = await _verifier(
        format_specific_lineage, _AuthorizationRepository(REVISION)
    ).verify(
        item=format_specific_item,
        publication_pin=changed_pin,
        authorization_context=_authorization_context(),
    )

    assert isinstance(result, VerifiedEvidenceItem)


@pytest.mark.asyncio
async def test_locator_contract_forged_outside_source_lineage_is_rejected() -> None:
    lineage, item, pin = _lineage()
    forged = lineage.model_copy(
        update={
            "processing_receipt": lineage.processing_receipt.model_copy(
                update={"locator_contract_version": "locator:forged-v1"}
            )
        }
    )

    result = await _verifier(forged, _AuthorizationRepository(REVISION)).verify(
        item=item,
        publication_pin=pin,
        authorization_context=_authorization_context(),
    )

    assert isinstance(result, RejectedEvidenceItem)
    assert result.reason_code is EvidenceVerificationReasonCode.EVIDENCE_UNVERIFIED


@pytest.mark.asyncio
async def test_fabricated_read_authorization_reference_is_rejected() -> None:
    lineage, item, pin = _lineage()
    unsafe = lineage.model_copy(
        update={
            "authorization_pin": lineage.authorization_pin.model_copy(
                update={"decision_reference": UUID(int=122)}
            )
        }
    )

    result = await _verifier(unsafe, _AuthorizationRepository(REVISION)).verify(
        item=item,
        publication_pin=pin,
        authorization_context=_authorization_context(),
    )

    assert isinstance(result, RejectedEvidenceItem)


@pytest.mark.asyncio
async def test_projection_receipt_contents_must_match_snapshot_candidate_digest() -> None:
    lineage, item, pin = _lineage()
    changed_receipt = lineage.projection_receipt.model_copy(
        update={"artifact_integrity_digest": "8" * 64}
    )
    unsafe = lineage.model_copy(
        update={
            "projection_receipt": changed_receipt,
            "projection_receipts": (changed_receipt,),
        }
    )

    result = await _verifier(unsafe, _AuthorizationRepository(REVISION)).verify(
        item=item,
        publication_pin=pin,
        authorization_context=_authorization_context(),
    )

    assert isinstance(result, RejectedEvidenceItem)
    assert result.reason_code is EvidenceVerificationReasonCode.EVIDENCE_UNVERIFIED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field_name", "field_value"),
    [
        pytest.param("projection_kind", "monitoring", id="projection-kind"),
        pytest.param("boundary_time", NOW + timedelta(seconds=1), id="boundary-time"),
    ],
)
async def test_boundary_semantics_must_match_publication_pin(
    field_name: str,
    field_value: object,
) -> None:
    lineage, item, original_pin = _lineage()
    boundary = lineage.canonical_boundary.model_copy(
        update={field_name: field_value, "manifest_digest": "0" * 64}
    )
    boundary = boundary.model_copy(
        update={
            "manifest_digest": canonical_boundary_manifest_digest(
                boundary,
                lineage.canonical_boundary_members,
            )
        }
    )
    publication = PublishedActivePublication(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        projection_kind="qa",
        projection_snapshot=lineage.projection_snapshot,
        projection_activation=lineage.projection_activation,
        activation_generation=lineage.projection_activation.activation_generation,
        active_pointer_cas_version=original_pin.active_pointer_cas_version,
        canonical_boundary_id=boundary.boundary_id,
        canonical_boundary_manifest_digest=boundary.manifest_digest,
        accepted_through_generation=boundary.accepted_through_generation,
        canonical_boundary_membership_ref="canonical-boundary-members:r1",
        as_of=original_pin.as_of,
        projection_config_version=lineage.projection_config_version,
        freshness="current",
        health="healthy",
    )
    pin = PublicationPin.from_publication(publication)
    context = item.read_contexts[0].model_copy(
        update={
            "publication_pin_id": pin.pin_id,
            "publication_pin_digest": pin.pin_digest,
            "canonical_boundary_digest": boundary.manifest_digest,
        }
    )
    unsafe = lineage.model_copy(
        update={
            "canonical_boundary": boundary,
            "authorization_pin": lineage.authorization_pin.model_copy(
                update={"read_context": context}
            ),
        }
    )
    unsafe_item = item.model_copy(
        update={"read_context": context, "read_contexts": (context,)}
    )

    result = await _verifier(unsafe, _AuthorizationRepository(REVISION)).verify(
        item=unsafe_item,
        publication_pin=pin,
        authorization_context=_authorization_context(),
    )

    assert isinstance(result, RejectedEvidenceItem)
    assert result.reason_code is EvidenceVerificationReasonCode.EVIDENCE_UNVERIFIED


@pytest.mark.asyncio
async def test_versioned_lineage_evaluation_preserves_score_with_mixed_format_cases() -> None:
    lineage, item, pin = _lineage()
    verified = await _verifier(lineage, _AuthorizationRepository(REVISION)).verify(
        item=item,
        publication_pin=pin,
        authorization_context=_authorization_context(),
    )
    forged_evidence = item.evidence[0].model_copy(
        update={"evidence_digest": "9" * 64}
    )
    forged = await _verifier(lineage, _AuthorizationRepository(REVISION)).verify(
        item=item.model_copy(update={"evidence": (forged_evidence,)}),
        publication_pin=pin,
        authorization_context=_authorization_context(),
    )
    lineages, entity, entity_pin = _multi_source_lineage()
    mixed_access = await _verifier(
        lineages,
        _MultiAuthorizationRepository(grant_second_source=False),
    ).verify(
        item=entity,
        publication_pin=entity_pin,
        authorization_context=_authorization_context(),
    )
    config = lineage.projection_config_version.model_copy(
        update={"locator_contract_version": "locator:v2"}
    )
    publication = PublishedActivePublication(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        projection_kind="qa",
        projection_snapshot=lineage.projection_snapshot,
        projection_activation=lineage.projection_activation,
        activation_generation=lineage.projection_activation.activation_generation,
        active_pointer_cas_version=pin.active_pointer_cas_version,
        canonical_boundary_id=BOUNDARY,
        canonical_boundary_manifest_digest=lineage.canonical_boundary.manifest_digest,
        accepted_through_generation=lineage.canonical_boundary.accepted_through_generation,
        canonical_boundary_membership_ref="canonical-boundary-members:r1",
        as_of=pin.as_of,
        projection_config_version=config,
        freshness="current",
        health="healthy",
    )
    format_pin = PublicationPin.from_publication(publication)
    format_context = item.read_contexts[0].model_copy(
        update={
            "publication_pin_id": format_pin.pin_id,
            "publication_pin_digest": format_pin.pin_digest,
        }
    )
    format_lineage = lineage.model_copy(
        update={
            "projection_config_version": config,
            "authorization_pin": lineage.authorization_pin.model_copy(
                update={"read_context": format_context}
            ),
        }
    )
    format_item = item.model_copy(
        update={
            "read_context": format_context,
            "read_contexts": (format_context,),
        }
    )
    format_specific = await _verifier(
        format_lineage, _AuthorizationRepository(REVISION)
    ).verify(
        item=format_item,
        publication_pin=format_pin,
        authorization_context=_authorization_context(),
    )
    forged_locator = await _verifier(
        lineage.model_copy(
            update={
                "processing_receipt": lineage.processing_receipt.model_copy(
                    update={"locator_contract_version": "locator:forged-v1"}
                )
            }
        ),
        _AuthorizationRepository(REVISION),
    ).verify(
        item=item,
        publication_pin=pin,
        authorization_context=_authorization_context(),
    )

    report = evaluate_evidence_lineage_outcomes(
        {
            "authorized-single-source": verified,
            "forged-evidence-digest": forged,
            "mixed-access-derived-entity": mixed_access,
            "format-specific-locator-lineage": format_specific,
            "forged-cross-lineage-locator": forged_locator,
        },
        dataset=load_evidence_lineage_v1_dataset(),
        baseline_exact_decision_rate=1.0,
    )
    artifact_path = (
        Path(__file__).resolve().parents[2]
        / "docs/qualification/issue-265-evidence-lineage-evaluation.json"
    )

    assert report.passed is True
    assert report.exact_decision_rate == 1.0
    assert report.safe_rejection_rate == 1.0
    assert report.comparison_status == "compared"
    assert report.exact_decision_delta == 0.0
    assert json.loads(artifact_path.read_text(encoding="utf-8")) == report.model_dump(
        mode="json"
    )
