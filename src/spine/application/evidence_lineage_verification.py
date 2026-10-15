"""Provider-neutral verification of structured retrieval evidence lineage.

The verifier accepts no provider-owned provenance as authority.  It resolves
the canonical source and projection records behind each evidence identifier,
proves the records against the publication pin, and then delegates current
content access to :class:`ContentAuthorizationDecisionService`.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import Enum
from typing import Literal, Protocol, TypeAlias
from uuid import UUID, uuid4

from pydantic import ConfigDict, Field, field_validator, model_validator

from spine.application.content_authorization import (
    AuthorizationContextMode,
    AuthorizationReasonCode,
    ContentAuthorizationDecision,
    ContentAuthorizationDecisionService,
    TrustedAuthorizationContext,
)
from spine.application.projection_locator import canonical_projection_locator_digest
from spine.domain.common import DefinitionModel
from spine.domain.ingestion import ProcessingCoverage, ProcessingReceipt
from spine.domain.projections import (
    ProjectionActivation,
    ProjectionConfigVersion,
    ProjectionPartition,
    ProjectionReceipt,
    ProjectionRunTransition,
    ProjectionSnapshot,
    ProjectionSnapshotMember,
    ProjectionSnapshotMemberDisposition,
    ProjectionSnapshotState,
    ProjectionValidation,
    SourceProjectionReference,
    projection_evidence_digest,
    projection_snapshot_manifest_digest,
)
from spine.domain.sources import (
    CanonicalBoundary,
    CanonicalBoundaryMember,
    RevisionKind,
    SourceObject,
    SourceRevision,
    assert_revision_digest,
    canonical_boundary_manifest_digest,
)
from spine.memory.contracts import (
    EvidenceReference,
    ProjectionChunk,
    ProjectionEntity,
    ProjectionNamespace,
    ProjectionNamespaceState,
    ProjectionRelation,
    PublicationPin,
    RetrievalReadContext,
    StableLocator,
)


EvidenceItem: TypeAlias = ProjectionChunk | ProjectionEntity | ProjectionRelation


class EvidenceVerificationReasonCode(str, Enum):
    """Bounded reasons safe to disclose without revealing protected sources."""

    VERIFIED = "verified"
    EVIDENCE_UNVERIFIED = "evidence_unverified"
    AUTHORIZATION_DENIED = "authorization_denied"
    AUTHORITY_STALE = "authority_stale"
    VERIFICATION_UNAVAILABLE = "verification_unavailable"


class EvidenceVerificationDiagnostic(DefinitionModel):
    """Content-free diagnostic safe for every rejected verification."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    retryable: bool = False
    opaque_reference: UUID | None = None

    @field_validator("opaque_reference")
    @classmethod
    def validate_reference(cls, value: UUID | None) -> UUID | None:
        if value is not None and value.int == 0:
            raise ValueError("opaque_reference must be a non-zero UUID")
        return value


class EvidenceAuthorizationPin(DefinitionModel):
    """Authorization generations used when the pinned physical read was issued."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_object_id: UUID
    read_context: RetrievalReadContext
    policy_generation: int = Field(ge=1)
    membership_generation: int = Field(ge=1)
    authority_generation: int = Field(ge=1)
    service_authority_generation: int | None = Field(default=None, ge=1)
    purpose: str
    operation: str
    decision_reference: UUID

    @field_validator("source_object_id", "decision_reference")
    @classmethod
    def validate_ids(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("authorization pin references must be non-zero UUIDs")
        return value

    @model_validator(mode="after")
    def validate_operations(self) -> "EvidenceAuthorizationPin":
        for name, value in (("purpose", self.purpose), ("operation", self.operation)):
            if not value or len(value) > 64:
                raise ValueError(f"{name} must be a bounded identifier")
        return self


class CanonicalEvidenceLineage(DefinitionModel):
    """Canonical records proving one provider-returned evidence identifier."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_id: str
    evidence_digest: str
    authorization_pin: EvidenceAuthorizationPin
    stable_locator: StableLocator
    source_object: SourceObject
    source_revision: SourceRevision
    processing_receipt: ProcessingReceipt
    source_projection_reference: SourceProjectionReference
    projection_receipt: ProjectionReceipt
    projection_partitions: tuple[ProjectionPartition, ...]
    projection_receipts: tuple[ProjectionReceipt, ...]
    source_projection_references: tuple[SourceProjectionReference, ...]
    projection_run_transitions: tuple[ProjectionRunTransition, ...]
    projection_validations: tuple[ProjectionValidation, ...]
    projection_snapshot: ProjectionSnapshot
    projection_snapshot_members: tuple[ProjectionSnapshotMember, ...]
    canonical_boundary: CanonicalBoundary
    canonical_boundary_members: tuple[CanonicalBoundaryMember, ...]
    projection_activation: ProjectionActivation
    projection_config_version: ProjectionConfigVersion
    projection_namespace: ProjectionNamespace

    @model_validator(mode="after")
    def validate_bounded_fields(self) -> "CanonicalEvidenceLineage":
        if not self.evidence_id or len(self.evidence_id) > 128:
            raise ValueError("evidence_id must be a bounded identifier")
        if len(self.evidence_digest) != 64 or any(
            character not in "0123456789abcdef" for character in self.evidence_digest
        ):
            raise ValueError("evidence_digest must be a lowercase SHA-256 digest")
        return self


class VerifiedEvidenceItem(DefinitionModel):
    """An item whose complete lineage and every contributor are authorized."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["verified"] = "verified"
    reason_code: Literal[EvidenceVerificationReasonCode.VERIFIED] = (
        EvidenceVerificationReasonCode.VERIFIED
    )
    item: EvidenceItem
    publication_pin_id: UUID
    authorization_decision_reference: UUID
    decision_reference: UUID

    @field_validator(
        "publication_pin_id", "authorization_decision_reference", "decision_reference"
    )
    @classmethod
    def validate_ids(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("verification references must be non-zero UUIDs")
        return value


class RejectedEvidenceItem(DefinitionModel):
    """A fail-closed result that contains no evidence or source identifiers."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["rejected"] = "rejected"
    reason_code: Literal[
        EvidenceVerificationReasonCode.EVIDENCE_UNVERIFIED,
        EvidenceVerificationReasonCode.AUTHORIZATION_DENIED,
        EvidenceVerificationReasonCode.AUTHORITY_STALE,
        EvidenceVerificationReasonCode.VERIFICATION_UNAVAILABLE,
    ]
    decision_reference: UUID
    diagnostic: EvidenceVerificationDiagnostic

    @field_validator("decision_reference")
    @classmethod
    def validate_decision_reference(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("decision_reference must be a non-zero UUID")
        return value


EvidenceVerificationOutcome: TypeAlias = VerifiedEvidenceItem | RejectedEvidenceItem


class EvidenceLineageRepository(Protocol):
    """Enumerate every canonical contributor for one projected item."""

    async def resolve_item(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        projection_snapshot_id: UUID,
        projected_kind: str,
        projected_object_id: str,
    ) -> tuple[CanonicalEvidenceLineage, ...]: ...

    async def resolve_authorization_decision(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        decision_reference: UUID,
    ) -> ContentAuthorizationDecision | None: ...


class EvidenceLineageVerifier(Protocol):
    """Public verification seam for one structured retrieval item."""

    async def verify(
        self,
        *,
        item: EvidenceItem,
        publication_pin: PublicationPin,
        authorization_context: TrustedAuthorizationContext,
    ) -> EvidenceVerificationOutcome: ...


class EvidenceVerificationFailureObservation(DefinitionModel):
    """Safe operational failure handed to the diagnostic-event adapter."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision_reference: UUID
    workspace_id: UUID
    environment_id: UUID
    trace_id: UUID
    reason_code: Literal[EvidenceVerificationReasonCode.VERIFICATION_UNAVAILABLE] = (
        EvidenceVerificationReasonCode.VERIFICATION_UNAVAILABLE
    )


class EvidenceVerificationFailureObserver(Protocol):
    """Record an opaque diagnostic event without receiving protected content."""

    async def record_failure(
        self, observation: EvidenceVerificationFailureObservation
    ) -> UUID: ...


class EvidenceLineageVerificationService:
    """Prove canonical lineage and current access for every item contributor."""

    def __init__(
        self,
        *,
        repository: EvidenceLineageRepository,
        authorization: ContentAuthorizationDecisionService,
        failure_observer: EvidenceVerificationFailureObserver,
        decision_reference_factory: Callable[[], UUID] | None = None,
    ) -> None:
        self._repository = repository
        self._authorization = authorization
        self._failure_observer = failure_observer
        self._decision_reference_factory = decision_reference_factory or uuid4

    async def verify(
        self,
        *,
        item: EvidenceItem,
        publication_pin: PublicationPin,
        authorization_context: TrustedAuthorizationContext,
    ) -> EvidenceVerificationOutcome:
        try:
            item_kind, item_id = _item_identity(item)
            contexts = _read_contexts(item)
            if not _pin_is_eligible(publication_pin, authorization_context, contexts):
                return self._reject(EvidenceVerificationReasonCode.EVIDENCE_UNVERIFIED)
            if len({reference.evidence_id for reference in item.evidence}) != len(
                item.evidence
            ):
                return self._reject(EvidenceVerificationReasonCode.EVIDENCE_UNVERIFIED)

            candidates = await self._repository.resolve_item(
                workspace_id=authorization_context.workspace_id,
                environment_id=authorization_context.environment_id,
                projection_snapshot_id=publication_pin.projection_snapshot.projection_snapshot_id,
                projected_kind=item_kind,
                projected_object_id=item_id,
            )
            candidates_by_evidence: dict[str, list[CanonicalEvidenceLineage]] = {}
            for candidate in candidates:
                candidates_by_evidence.setdefault(candidate.evidence_id, []).append(
                    candidate
                )
            claimed_evidence_ids = {reference.evidence_id for reference in item.evidence}
            if (
                not candidates
                or set(candidates_by_evidence) != claimed_evidence_ids
                or any(len(values) != 1 for values in candidates_by_evidence.values())
            ):
                return self._reject(EvidenceVerificationReasonCode.EVIDENCE_UNVERIFIED)

            lineages: list[CanonicalEvidenceLineage] = []
            for evidence in item.evidence:
                matching_contexts = tuple(
                    context
                    for context in contexts
                    if _context_contains_evidence(context, evidence)
                )
                if len(matching_contexts) != 1 or not _evidence_claim_is_complete(
                    evidence, authorization_context
                ):
                    return self._reject(
                        EvidenceVerificationReasonCode.EVIDENCE_UNVERIFIED
                    )
                lineage = candidates_by_evidence[evidence.evidence_id][0]
                if not _lineage_matches(
                    lineage=lineage,
                    evidence=evidence,
                    context=matching_contexts[0],
                    publication_pin=publication_pin,
                    item_kind=item_kind,
                    item_id=item_id,
                ):
                    return self._reject(
                        EvidenceVerificationReasonCode.EVIDENCE_UNVERIFIED
                    )
                lineages.append(lineage)

            for lineage in lineages:
                pinned_decision = await self._repository.resolve_authorization_decision(
                    workspace_id=authorization_context.workspace_id,
                    environment_id=authorization_context.environment_id,
                    decision_reference=lineage.authorization_pin.decision_reference,
                )
                if not _read_authorization_is_proven(
                    pinned_decision,
                    lineage,
                    authorization_context,
                ):
                    return self._reject(
                        EvidenceVerificationReasonCode.EVIDENCE_UNVERIFIED
                    )

            source_ids = tuple(
                dict.fromkeys(lineage.source_object.source_object_id for lineage in lineages)
            )
            authorization = await _authorize(
                self._authorization, authorization_context, source_ids
            )
            rejected_reason = _authorization_rejection_reason(authorization)
            if rejected_reason is not None:
                return self._reject(rejected_reason)
            lineage_rejection = _authorization_lineage_rejection_reason(
                authorization, lineages
            )
            if lineage_rejection is not None:
                return self._reject(lineage_rejection)
            return VerifiedEvidenceItem(
                item=item,
                publication_pin_id=publication_pin.pin_id,
                authorization_decision_reference=authorization.decision_reference,
                decision_reference=self._decision_reference_factory(),
            )
        except Exception:
            decision_reference = self._decision_reference_factory()
            opaque_reference: UUID | None = None
            try:
                recorded_reference = await self._failure_observer.record_failure(
                    EvidenceVerificationFailureObservation(
                        decision_reference=decision_reference,
                        workspace_id=authorization_context.workspace_id,
                        environment_id=authorization_context.environment_id,
                        trace_id=authorization_context.trace_id,
                    )
                )
                if isinstance(recorded_reference, UUID) and recorded_reference.int != 0:
                    opaque_reference = recorded_reference
            except Exception:
                pass
            return self._reject(
                EvidenceVerificationReasonCode.VERIFICATION_UNAVAILABLE,
                retryable=True,
                decision_reference=decision_reference,
                opaque_reference=opaque_reference,
            )

    def _reject(
        self,
        reason: EvidenceVerificationReasonCode,
        *,
        retryable: bool = False,
        decision_reference: UUID | None = None,
        opaque_reference: UUID | None = None,
    ) -> RejectedEvidenceItem:
        return RejectedEvidenceItem(
            reason_code=reason,
            decision_reference=decision_reference or self._decision_reference_factory(),
            diagnostic=EvidenceVerificationDiagnostic(
                retryable=retryable,
                opaque_reference=opaque_reference,
            ),
        )


def _item_identity(item: EvidenceItem) -> tuple[str, str]:
    if isinstance(item, ProjectionChunk):
        return "chunk", item.chunk_id
    if isinstance(item, ProjectionEntity):
        return "entity", item.entity_id
    if isinstance(item, ProjectionRelation):
        return "relation", item.relation_id
    raise TypeError("unsupported structured evidence item")


def _read_contexts(item: EvidenceItem) -> tuple[RetrievalReadContext, ...]:
    contexts = item.read_contexts
    if not contexts and item.read_context is not None:
        contexts = (item.read_context,)
    if not contexts:
        return ()
    if len(set(contexts)) != len(contexts):
        return ()
    if item.read_context is not None and item.read_context not in contexts:
        return ()
    return contexts


def _pin_is_eligible(
    pin: PublicationPin,
    authorization_context: TrustedAuthorizationContext,
    contexts: tuple[RetrievalReadContext, ...],
) -> bool:
    if (
        not contexts
        or not pin.has_valid_digest()
        or pin.workspace_id != authorization_context.workspace_id
        or pin.environment_id != authorization_context.environment_id
    ):
        return False
    return all(
        context.workspace_id == pin.workspace_id
        and context.environment_id == pin.environment_id
        and context.publication_pin_id == pin.pin_id
        and context.publication_pin_digest == pin.pin_digest
        and context.projection_snapshot_id
        == pin.projection_snapshot.projection_snapshot_id
        and context.projection_activation_id
        == pin.projection_activation.projection_activation_id
        and context.activation_generation == pin.activation_generation
        and context.canonical_boundary_id == pin.canonical_boundary.boundary_id
        and context.canonical_boundary_digest == pin.canonical_boundary.boundary_digest
        and context.projection_run_id == pin.projection_snapshot.projection_run_id
        and context.projection_candidate_id
        == pin.projection_snapshot.projection_candidate_id
        and context.source_isolation_pre_retrieval_enforced
        for context in contexts
    )


def _context_contains_evidence(
    context: RetrievalReadContext, evidence: EvidenceReference
) -> bool:
    return (evidence.source_object_id, evidence.source_revision_id) in tuple(
        zip(context.source_object_ids, context.source_revision_ids, strict=True)
    )


def _evidence_claim_is_complete(
    evidence: EvidenceReference, context: TrustedAuthorizationContext
) -> bool:
    return bool(
        evidence.status == "valid"
        and evidence.workspace_id == context.workspace_id
        and evidence.environment_id == context.environment_id
        and evidence.revision_digest is not None
        and evidence.processing_receipt_id is not None
        and evidence.locator_contract_version is not None
    )


def _lineage_matches(
    *,
    lineage: CanonicalEvidenceLineage,
    evidence: EvidenceReference,
    context: RetrievalReadContext,
    publication_pin: PublicationPin,
    item_kind: str,
    item_id: str,
) -> bool:
    source = lineage.source_object
    revision = lineage.source_revision
    processing = lineage.processing_receipt
    reference = lineage.source_projection_reference
    receipt = lineage.projection_receipt
    snapshot = lineage.projection_snapshot
    boundary = lineage.canonical_boundary
    activation = lineage.projection_activation
    config = lineage.projection_config_version
    namespace = lineage.projection_namespace
    scope = (publication_pin.workspace_id, publication_pin.environment_id)

    try:
        assert_revision_digest(revision)
        boundary_digest = canonical_boundary_manifest_digest(
            boundary, lineage.canonical_boundary_members
        )
        snapshot_digest = projection_snapshot_manifest_digest(
            snapshot, lineage.projection_snapshot_members
        )
        candidate_evidence_digest = projection_evidence_digest(
            partitions=lineage.projection_partitions,
            receipts=lineage.projection_receipts,
            references=lineage.source_projection_references,
            transitions=lineage.projection_run_transitions,
            validations=lineage.projection_validations,
        )
        locator_digest = canonical_projection_locator_digest(
            locator_contract_version=evidence.locator_contract_version,
            locator=evidence.locator,
        )
    except Exception:
        return False

    boundary_matches = tuple(
        member
        for member in lineage.canonical_boundary_members
        if (member.source_object_id, member.source_revision_id)
        == (source.source_object_id, revision.revision_id)
    )
    snapshot_matches = tuple(
        member
        for member in lineage.projection_snapshot_members
        if (member.source_object_id, member.source_revision_id)
        == (source.source_object_id, revision.revision_id)
    )
    if len(boundary_matches) != 1 or len(snapshot_matches) != 1:
        return False
    boundary_member = boundary_matches[0]
    snapshot_member = snapshot_matches[0]

    return bool(
        lineage.evidence_id == evidence.evidence_id
        and lineage.evidence_digest == evidence.evidence_digest
        and lineage.authorization_pin.read_context == context
        and lineage.stable_locator == evidence.locator
        and (source.workspace_id, source.environment_id) == scope
        and source.source_object_id == evidence.source_object_id
        and revision.kind is RevisionKind.CONTENT
        and revision.source_object_id == source.source_object_id
        and revision.revision_id == evidence.source_revision_id
        and revision.revision_digest == evidence.revision_digest
        and (revision.workspace_id, revision.environment_id) == scope
        and processing.processing_receipt_id == evidence.processing_receipt_id
        and processing.coverage is not ProcessingCoverage.FAILED
        and processing.original_sha256 == revision.original_sha256
        and processing.source_object_id == source.source_object_id
        and processing.source_revision_id == revision.revision_id
        and processing.locator_contract_version
        == evidence.locator_contract_version
        and processing.locator_contract_version == config.locator_contract_version
        and (processing.workspace_id, processing.environment_id) == scope
        and reference.workspace_id == scope[0]
        and reference.environment_id == scope[1]
        and reference.source_object_id == source.source_object_id
        and reference.source_revision_id == revision.revision_id
        and reference.processing_receipt_id == processing.processing_receipt_id
        and reference.projection_run_id == context.projection_run_id
        and reference.projection_candidate_id == context.projection_candidate_id
        and reference.projection_receipt_id == context.projection_receipt_id
        and reference.locator_schema_version == evidence.locator_contract_version
        and reference.locator_reference == evidence.locator.value
        and reference.locator_digest == locator_digest
        and reference.projected_kind == item_kind
        and reference.projected_object_id == item_id
        and reference.projection_config_version_id
        == publication_pin.projection_config_version.projection_config_version_id
        and receipt.projection_receipt_id == reference.projection_receipt_id
        and receipt in lineage.projection_receipts
        and reference in lineage.source_projection_references
        and any(
            partition.projection_partition_id == receipt.projection_partition_id
            for partition in lineage.projection_partitions
        )
        and receipt.projection_run_id == context.projection_run_id
        and receipt.projection_candidate_id == context.projection_candidate_id
        and receipt.projection_partition_id == context.projection_partition_id
        and (receipt.workspace_id, receipt.environment_id) == scope
        and snapshot.snapshot_state is ProjectionSnapshotState.FINALIZED
        and snapshot.projection_snapshot_id == context.projection_snapshot_id
        and snapshot.projection_run_id == context.projection_run_id
        and snapshot.projection_candidate_id == context.projection_candidate_id
        and snapshot.canonical_boundary_id == context.canonical_boundary_id
        and snapshot.projection_config_version_id
        == publication_pin.projection_config_version.projection_config_version_id
        and snapshot.member_count == len(lineage.projection_snapshot_members)
        and snapshot.manifest_digest == snapshot_digest
        and snapshot.candidate_evidence_digest == candidate_evidence_digest
        and (snapshot.workspace_id, snapshot.environment_id) == scope
        and snapshot_member.disposition
        is ProjectionSnapshotMemberDisposition.INCLUDED
        and (snapshot_member.workspace_id, snapshot_member.environment_id) == scope
        and snapshot_member.projection_snapshot_id == snapshot.projection_snapshot_id
        and snapshot_member.canonical_boundary_id == boundary.boundary_id
        and snapshot_member.canonical_boundary_member_id
        == boundary_member.source_object_id
        and snapshot_member.projection_partition_id == context.projection_partition_id
        and snapshot_member.projection_receipt_id == receipt.projection_receipt_id
        and boundary.boundary_id == context.canonical_boundary_id
        and boundary.manifest_digest == boundary_digest
        and boundary.manifest_digest == context.canonical_boundary_digest
        and boundary.projection_kind == publication_pin.projection_kind
        and boundary.boundary_time == publication_pin.as_of.boundary_time
        and boundary.accepted_through_generation
        == publication_pin.canonical_boundary.accepted_through_generation
        and (boundary.workspace_id, boundary.environment_id) == scope
        and boundary_member.boundary_id == boundary.boundary_id
        and (boundary_member.workspace_id, boundary_member.environment_id) == scope
        and boundary_member.revision_kind is RevisionKind.CONTENT
        and boundary_member.accepted_generation
        <= boundary.accepted_through_generation
        and activation.projection_activation_id == context.projection_activation_id
        and activation.projection_snapshot_id == snapshot.projection_snapshot_id
        and activation.activation_generation == context.activation_generation
        and (activation.workspace_id, activation.environment_id) == scope
        and config == publication_pin.projection_config_version
        and namespace.state is ProjectionNamespaceState.ACTIVE
        and namespace.projection_kind == publication_pin.projection_kind
        and namespace.namespace == context.physical_namespace
        and namespace.namespace_digest == context.physical_namespace_digest
        and snapshot == publication_pin.projection_snapshot
        and activation == publication_pin.projection_activation
    )


def _read_authorization_is_proven(
    decision: ContentAuthorizationDecision | None,
    lineage: CanonicalEvidenceLineage,
    context: TrustedAuthorizationContext,
) -> bool:
    if decision is None or not decision.allowed:
        return False
    pin = lineage.authorization_pin
    source_id = lineage.source_object.source_object_id
    evidence = tuple(
        value for value in decision.policy_evidence if value.source_object_id == source_id
    )
    return bool(
        decision.decision_reference == pin.decision_reference
        and decision.workspace_id == context.workspace_id
        and decision.environment_id == context.environment_id
        and decision.decision_kind.value == context.mode.value
        and source_id in decision.source_object_ids
        and len(evidence) == 1
        and evidence[0].source_revision_id == lineage.source_revision.revision_id
        and evidence[0].policy_generation == pin.policy_generation
        and evidence[0].retention_eligible is True
        and decision.membership_generation == pin.membership_generation
        and decision.authority_generation == pin.authority_generation
        and decision.service_authority_generation
        == pin.service_authority_generation
        and decision.purpose == pin.purpose
        and decision.operation == pin.operation
    )


async def _authorize(
    service: ContentAuthorizationDecisionService,
    context: TrustedAuthorizationContext,
    source_ids: tuple[UUID, ...],
) -> ContentAuthorizationDecision:
    if context.mode is AuthorizationContextMode.HUMAN_DISCLOSURE:
        return await service.decide_human_disclosure(context, source_ids)
    if context.mode is AuthorizationContextMode.INTERACTIVE_PROCESSING:
        return await service.decide_interactive_processing(context, source_ids)
    return await service.decide_noninteractive_processing(context, source_ids)


def _authorization_rejection_reason(
    decision: ContentAuthorizationDecision,
) -> EvidenceVerificationReasonCode | None:
    if decision.allowed:
        return None
    if decision.reason_code is AuthorizationReasonCode.AUTHORITY_STALE:
        return EvidenceVerificationReasonCode.AUTHORITY_STALE
    if decision.reason_code in {
        AuthorizationReasonCode.DECISION_UNAVAILABLE,
        AuthorizationReasonCode.AUDIT_UNAVAILABLE,
    }:
        return EvidenceVerificationReasonCode.VERIFICATION_UNAVAILABLE
    if decision.reason_code is AuthorizationReasonCode.SOURCE_UNAVAILABLE:
        return EvidenceVerificationReasonCode.EVIDENCE_UNVERIFIED
    return EvidenceVerificationReasonCode.AUTHORIZATION_DENIED


def _authorization_lineage_rejection_reason(
    decision: ContentAuthorizationDecision,
    lineages: list[CanonicalEvidenceLineage],
) -> EvidenceVerificationReasonCode | None:
    evidence_by_source = {
        evidence.source_object_id: evidence for evidence in decision.policy_evidence
    }
    expected_by_source = {
        lineage.source_object.source_object_id: lineage.source_revision.revision_id
        for lineage in lineages
    }
    if (
        not decision.allowed
        or set(decision.source_object_ids) != set(expected_by_source)
        or set(evidence_by_source) != set(expected_by_source)
        or any(
            evidence_by_source[source_id].source_revision_id != revision_id
            or evidence_by_source[source_id].retention_eligible is not True
            for source_id, revision_id in expected_by_source.items()
        )
    ):
        return EvidenceVerificationReasonCode.EVIDENCE_UNVERIFIED
    for lineage in lineages:
        source_id = lineage.source_object.source_object_id
        pin = lineage.authorization_pin
        evidence = evidence_by_source[source_id]
        if (
            pin.source_object_id != source_id
            or pin.policy_generation != evidence.policy_generation
            or pin.membership_generation != decision.membership_generation
            or pin.authority_generation != decision.authority_generation
            or pin.service_authority_generation
            != decision.service_authority_generation
            or pin.purpose != decision.purpose
            or pin.operation != decision.operation
        ):
            return EvidenceVerificationReasonCode.AUTHORITY_STALE
    return None


__all__ = [
    "CanonicalEvidenceLineage",
    "EvidenceAuthorizationPin",
    "EvidenceItem",
    "EvidenceLineageRepository",
    "EvidenceLineageVerificationService",
    "EvidenceLineageVerifier",
    "EvidenceVerificationDiagnostic",
    "EvidenceVerificationFailureObservation",
    "EvidenceVerificationFailureObserver",
    "EvidenceVerificationOutcome",
    "EvidenceVerificationReasonCode",
    "RejectedEvidenceItem",
    "VerifiedEvidenceItem",
]
