"""Read-only exact source matching for independently mapped CRM FILES items."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from enum import Enum
from hashlib import sha256
from typing import Literal, Protocol
from uuid import UUID

from pydantic import Field, model_validator

from spine.application.persistence.canonical import CanonicalAcceptanceReadResult
from spine.application.persistence.context import TrustedPersistenceContext
from spine.application.persistence.unit_of_work import UnitOfWork, UnitOfWorkFactory
from spine.application.source_usage_guard import (
    CurrentSourceUsageAuthority,
    CurrentSourceUsageGuard,
    SourceUsageArtifact,
    SourceUsageDependency,
    SourceUsageKind,
    SourceUsageReason,
    SourceUsageRequest,
)
from spine.domain.common import DefinitionModel
from spine.domain.sources import (
    CurrentLifecycleEligibility,
    CurrentSourceHead,
    IdentityMode,
    LifecycleEligibilityStatus,
    ProcessingAdmissionMode,
    RetentionArtifactClass,
    RevisionKind,
    SourceObject,
    SourceProcessingAdmission,
    SourceRevision,
    SourceRevisionProvenance,
    SourceRevisionReceiptAssociation,
)
from spine.domain.sources.crm_relationship_v2 import CrmEligibilityOutcome
from spine.domain.sources.bitrix24_disk_identity import (
    BITRIX24_DISK_OBJECT_NAMESPACE,
    BITRIX24_DISK_SOURCE_GENERATION,
    BITRIX24_DISK_SOURCE_NAMESPACE,
    canonical_bitrix24_disk_external_object_id,
)
from spine.domain.sources.trusted_content import assert_exact_revision_receipt_binding


class CrmFilesItemIdentity(DefinitionModel):
    """One exact namespaced FILES item from the relationship identity."""

    namespace: str = Field(min_length=1, max_length=128)
    value: str = Field(min_length=1, max_length=256)


class DiskRevisionIdentity(DefinitionModel):
    """Provider-native Disk object and revision identity; never timestamp-derived."""

    namespace: str = Field(min_length=1, max_length=128)
    object_id: str = Field(min_length=1, max_length=256)
    provider_revision: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_namespace(self) -> "DiskRevisionIdentity":
        if self.namespace != BITRIX24_DISK_OBJECT_NAMESPACE:
            raise ValueError("Disk revision namespace is unsupported")
        return self


class CrmMappedDiskRevisionEvidence(DefinitionModel):
    """Stable application handoff produced from either T3 or T4 output."""

    workspace_id: UUID
    environment_id: UUID
    connection_id: UUID
    connection_version_id: UUID
    mapping_strategy: Literal["direct_disk_object", "attached_object"]
    mapping_profile_id: UUID
    mapping_profile_version: int = Field(ge=1)
    files_item: CrmFilesItemIdentity
    disk_revision: DiskRevisionIdentity
    ownership_proof_reference: str | None = Field(default=None, max_length=255)
    ownership_proof_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_mapping(self) -> "CrmMappedDiskRevisionEvidence":
        identifiers = (
            self.workspace_id,
            self.environment_id,
            self.connection_id,
            self.connection_version_id,
            self.mapping_profile_id,
        )
        if any(value.int == 0 for value in identifiers):
            raise ValueError("mapped Disk evidence identifiers must be non-zero")
        proof = (self.ownership_proof_reference, self.ownership_proof_digest)
        if self.mapping_strategy == "attached_object" and any(
            value is None for value in proof
        ):
            raise ValueError("attached mapping requires an exact ownership proof")
        if self.mapping_strategy == "direct_disk_object" and any(
            value is not None for value in proof
        ):
            raise ValueError("direct mapping cannot carry attached ownership proof")
        return self


class CrmRelationshipV2SourceMatchRequest(DefinitionModel):
    """One relationship FILES item and its independently produced Disk evidence."""

    workspace_id: UUID
    environment_id: UUID
    connection_id: UUID
    connection_version_id: UUID
    relationship_files_item: CrmFilesItemIdentity
    mapping_evidence: CrmMappedDiskRevisionEvidence

    @model_validator(mode="after")
    def validate_evidence_scope(self) -> "CrmRelationshipV2SourceMatchRequest":
        scope = (
            self.workspace_id,
            self.environment_id,
            self.connection_id,
            self.connection_version_id,
        )
        if any(value.int == 0 for value in scope):
            raise ValueError("source match scope identifiers must be non-zero")
        evidence = self.mapping_evidence
        if (
            evidence.workspace_id,
            evidence.environment_id,
            evidence.connection_id,
            evidence.connection_version_id,
        ) != scope:
            raise ValueError("mapped Disk evidence does not match relationship scope")
        if evidence.files_item != self.relationship_files_item:
            raise ValueError(
                "mapped Disk evidence must bind the exact relationship FILES item"
            )
        return self


class CanonicalDiskSourceQuery(DefinitionModel):
    """Exact lookup key passed to the existing canonical-source transaction."""

    workspace_id: UUID
    environment_id: UUID
    connection_id: UUID
    connection_version_id: UUID
    relationship_files_item: CrmFilesItemIdentity
    mapping_profile_id: UUID
    mapping_profile_version: int = Field(ge=1)
    disk_revision: DiskRevisionIdentity


class CanonicalDiskSourceCandidate(DefinitionModel):
    """Existing S2 records for one possible current Disk revision."""

    source: SourceObject
    revision: SourceRevision
    provenance: SourceRevisionProvenance
    receipt: SourceRevisionReceiptAssociation
    lifecycle: CurrentLifecycleEligibility


class CanonicalDiskSourceFinder(Protocol):
    """Find existing S2 records inside the caller-owned read transaction."""

    async def find_current(
        self, query: CanonicalDiskSourceQuery, transaction: UnitOfWork
    ) -> tuple[CanonicalDiskSourceCandidate, ...]: ...


class CanonicalSourceAuthorityFence(DefinitionModel):
    """Exact S2/S3 tokens that T6 must revalidate before persistence."""

    source_head: CurrentSourceHead
    processing_admission: SourceProcessingAdmission
    lifecycle: CurrentLifecycleEligibility

    @model_validator(mode="after")
    def validate_exact_authority(self) -> "CanonicalSourceAuthorityFence":
        head = self.source_head
        admission = self.processing_admission
        lifecycle = self.lifecycle
        if head.current_revision_kind is not RevisionKind.CONTENT:
            raise ValueError("source authority requires a current content revision")
        scope = (
            head.workspace_id,
            head.environment_id,
            head.source_object_id,
            head.current_revision_id,
        )
        if (
            admission.workspace_id,
            admission.environment_id,
            admission.source_object_id,
            admission.source_revision_id,
        ) != scope:
            raise ValueError(
                "processing admission must bind the current source revision"
            )
        if (
            lifecycle.workspace_id,
            lifecycle.environment_id,
            lifecycle.source_object_id,
            lifecycle.current_decision_id,
            lifecycle.generation,
        ) != (
            head.workspace_id,
            head.environment_id,
            head.source_object_id,
            admission.lifecycle_eligibility_reference,
            admission.lifecycle_eligibility_generation,
        ):
            raise ValueError("lifecycle authority must bind the processing admission")
        return self


class CanonicalSourceFenceStatus(str, Enum):
    CURRENT = "current"
    STALE_CONTEXT = "stale_context"
    GOVERNANCE_WITHDRAWN = "governance_withdrawn"
    SOURCE_INELIGIBLE = "source_ineligible"
    AUTHORITY_UNAVAILABLE = "authority_unavailable"


class CanonicalDiskSourceResolution(DefinitionModel):
    status: CanonicalSourceFenceStatus
    source_object_id: UUID | None = None
    source_revision_id: UUID | None = None
    authority: CanonicalSourceAuthorityFence | None = None

    @model_validator(mode="after")
    def validate_resolution(self) -> "CanonicalDiskSourceResolution":
        handoff = (self.source_object_id, self.source_revision_id, self.authority)
        if self.status is CanonicalSourceFenceStatus.CURRENT:
            if any(value is None for value in handoff):
                raise ValueError(
                    "current source resolution requires a complete handoff"
                )
        elif any(value is not None for value in handoff):
            raise ValueError("denied source resolution cannot disclose source identity")
        return self


class ExistingCanonicalDiskSourceAuthority:
    """Compose existing source/canonical/S3 ports in one fenced transaction.

    The injected current-source authority must use the same transaction. The
    PostgreSQL implementation takes shared lifecycle/retention locks and a row
    lock on ``CurrentSourceHead`` before it issues an allowed decision. The
    returned head and processing-admission tokens remain a point-in-time handoff;
    T6 must revalidate them in its write transaction.
    """

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        source_finder: CanonicalDiskSourceFinder,
        usage_authority_factory: Callable[[UnitOfWork], CurrentSourceUsageAuthority],
        clock: Callable[[], datetime],
    ) -> None:
        self._uow_factory = uow_factory
        self._source_finder = source_finder
        self._usage_authority_factory = usage_authority_factory
        self._clock = clock

    async def resolve_current(
        self,
        query: CanonicalDiskSourceQuery,
        context: TrustedPersistenceContext,
    ) -> CanonicalDiskSourceResolution:
        try:
            async with self._uow_factory(context) as transaction:
                candidates = await self._source_finder.find_current(query, transaction)
                if not candidates:
                    return CanonicalDiskSourceResolution(
                        status=CanonicalSourceFenceStatus.STALE_CONTEXT
                    )
                if len(candidates) != 1:
                    return CanonicalDiskSourceResolution(
                        status=CanonicalSourceFenceStatus.AUTHORITY_UNAVAILABLE
                    )
                candidate = candidates[0]
                if not _candidate_matches(candidate, query):
                    return CanonicalDiskSourceResolution(
                        status=CanonicalSourceFenceStatus.STALE_CONTEXT
                    )
                current = await transaction.canonical.resolve_current(
                    candidate.source.source_object_id
                )
                if not _head_matches(current, candidate):
                    return CanonicalDiskSourceResolution(
                        status=CanonicalSourceFenceStatus.STALE_CONTEXT
                    )
                assert current is not None
                acceptance = await transaction.canonical.resolve_acceptance_read(
                    current.acceptance_id
                )
                admission = _matching_admission(acceptance, candidate)
                if admission is None:
                    return CanonicalDiskSourceResolution(
                        status=CanonicalSourceFenceStatus.SOURCE_INELIGIBLE
                    )
                usage = await CurrentSourceUsageGuard(
                    self._usage_authority_factory(transaction)
                ).evaluate(_usage_request(query, candidate, self._clock()))
                if not usage.allowed:
                    return CanonicalDiskSourceResolution(
                        status=_status_for_usage(usage.reason, candidate.lifecycle)
                    )
                authority = CanonicalSourceAuthorityFence(
                    source_head=current,
                    processing_admission=admission,
                    lifecycle=candidate.lifecycle,
                )
                return CanonicalDiskSourceResolution(
                    status=CanonicalSourceFenceStatus.CURRENT,
                    source_object_id=candidate.source.source_object_id,
                    source_revision_id=candidate.revision.revision_id,
                    authority=authority,
                )
        except Exception:
            return CanonicalDiskSourceResolution(
                status=CanonicalSourceFenceStatus.AUTHORITY_UNAVAILABLE
            )


class CrmRelationshipV2SourceMatchResult(DefinitionModel):
    outcome: CrmEligibilityOutcome
    source_object_id: UUID | None = None
    source_revision_id: UUID | None = None
    authority: CanonicalSourceAuthorityFence | None = None
    mapping_evidence: CrmMappedDiskRevisionEvidence | None = None

    @property
    def eligible(self) -> bool:
        return self.outcome is CrmEligibilityOutcome.ELIGIBLE_VERIFIED

    @model_validator(mode="after")
    def validate_handoff(self) -> "CrmRelationshipV2SourceMatchResult":
        handoff = (
            self.source_object_id,
            self.source_revision_id,
            self.authority,
            self.mapping_evidence,
        )
        if self.eligible:
            if any(value is None for value in handoff):
                raise ValueError("positive source match requires a complete handoff")
            assert self.authority is not None
            if (
                self.authority.source_head.source_object_id != self.source_object_id
                or self.authority.source_head.current_revision_id
                != self.source_revision_id
            ):
                raise ValueError(
                    "positive source match requires its exact current head"
                )
        elif any(value is not None for value in handoff):
            raise ValueError("denied source match cannot disclose source identity")
        return self


class CrmRelationshipV2SourceMatcher:
    """Match one exact mapped Disk revision without canonical-source writes."""

    def __init__(self, authority: ExistingCanonicalDiskSourceAuthority) -> None:
        self._authority = authority

    async def match(
        self,
        requested: CrmRelationshipV2SourceMatchRequest,
        context: TrustedPersistenceContext,
    ) -> CrmRelationshipV2SourceMatchResult:
        resolution = await self._authority.resolve_current(_query(requested), context)
        outcome = _outcome_for_status(resolution.status)
        if outcome is not CrmEligibilityOutcome.ELIGIBLE_VERIFIED:
            return CrmRelationshipV2SourceMatchResult(outcome=outcome)
        return CrmRelationshipV2SourceMatchResult(
            outcome=outcome,
            source_object_id=resolution.source_object_id,
            source_revision_id=resolution.source_revision_id,
            authority=resolution.authority,
            mapping_evidence=requested.mapping_evidence,
        )


def _query(requested: CrmRelationshipV2SourceMatchRequest) -> CanonicalDiskSourceQuery:
    evidence = requested.mapping_evidence
    return CanonicalDiskSourceQuery(
        workspace_id=requested.workspace_id,
        environment_id=requested.environment_id,
        connection_id=requested.connection_id,
        connection_version_id=requested.connection_version_id,
        relationship_files_item=requested.relationship_files_item,
        mapping_profile_id=evidence.mapping_profile_id,
        mapping_profile_version=evidence.mapping_profile_version,
        disk_revision=evidence.disk_revision,
    )


def _candidate_matches(
    candidate: CanonicalDiskSourceCandidate, query: CanonicalDiskSourceQuery
) -> bool:
    source = candidate.source
    revision = candidate.revision
    provenance = candidate.provenance
    receipt = candidate.receipt
    membership = provenance.bitrix24_disk_membership
    metadata = revision.revision_metadata
    external = None if metadata is None else metadata.external_file_revision
    if (
        source.identity_mode is not IdentityMode.CONNECTOR
        or source.external_namespace != BITRIX24_DISK_SOURCE_NAMESPACE
        or source.external_generation != BITRIX24_DISK_SOURCE_GENERATION
        or revision.kind is not RevisionKind.CONTENT
        or membership is None
        or external is None
    ):
        return False
    scope = (query.workspace_id, query.environment_id)
    if any(
        value != scope
        for value in (
            (source.workspace_id, source.environment_id),
            (revision.workspace_id, revision.environment_id),
            (provenance.workspace_id, provenance.environment_id),
            (receipt.workspace_id, receipt.environment_id),
            (membership.workspace_id, membership.environment_id),
            (candidate.lifecycle.workspace_id, candidate.lifecycle.environment_id),
        )
    ):
        return False
    lifecycle = candidate.lifecycle
    if (
        source.connection_id != query.connection_id
        or source.external_object_id
        != canonical_bitrix24_disk_external_object_id(
            query.connection_id, query.disk_revision.object_id
        )
        or revision.source_object_id != source.source_object_id
        or provenance.source_object_id != source.source_object_id
        or provenance.revision_id != revision.revision_id
        or provenance.connection_id != query.connection_id
        or receipt.provenance_id != provenance.provenance_id
        or membership.connection_id != query.connection_id
        or membership.connection_version_id != query.connection_version_id
        or membership.object_id != query.disk_revision.object_id
        or membership.provider_revision != query.disk_revision.provider_revision
        or external.provider_kind != "bitrix24"
        or external.provider_resource_id != query.disk_revision.object_id
        or external.provider_revision != query.disk_revision.provider_revision
        or lifecycle.source_object_id != source.source_object_id
        or lifecycle.connection_id != query.connection_id
        or lifecycle.connection_version_id != query.connection_version_id
    ):
        return False
    try:
        assert_exact_revision_receipt_binding(revision=revision, association=receipt)
    except ValueError:
        return False
    return True


def _head_matches(
    current: CurrentSourceHead | None, candidate: CanonicalDiskSourceCandidate
) -> bool:
    return bool(
        current is not None
        and current.workspace_id == candidate.source.workspace_id
        and current.environment_id == candidate.source.environment_id
        and current.source_object_id == candidate.source.source_object_id
        and current.current_revision_id == candidate.revision.revision_id
        and current.current_revision_kind is RevisionKind.CONTENT
    )


def _matching_admission(
    read: CanonicalAcceptanceReadResult | None,
    candidate: CanonicalDiskSourceCandidate,
) -> SourceProcessingAdmission | None:
    membership = candidate.provenance.bitrix24_disk_membership
    if read is None or not read.is_current or membership is None:
        return None
    admission = read.processing_admission
    if (
        admission is None
        or admission.mode is not ProcessingAdmissionMode.PROCESSOR_REQUIRED
    ):
        return None
    receipt = candidate.receipt
    lifecycle = candidate.lifecycle
    if (
        admission.workspace_id,
        admission.environment_id,
        admission.source_object_id,
        admission.source_revision_id,
        admission.provider_connection_id,
        admission.provider_connection_version_id,
        admission.receipt_association_id,
        admission.root_policy_id,
        admission.root_policy_version,
        admission.root_policy_digest,
        admission.content_policy_id,
        admission.content_policy_version,
        admission.content_policy_digest,
        admission.lifecycle_eligibility_reference,
        admission.lifecycle_eligibility_generation,
    ) != (
        candidate.source.workspace_id,
        candidate.source.environment_id,
        candidate.source.source_object_id,
        candidate.revision.revision_id,
        membership.connection_id,
        membership.connection_version_id,
        receipt.association_id,
        membership.root_policy_id,
        membership.root_policy_version,
        membership.root_policy_digest,
        membership.content_policy_id,
        membership.content_policy_version,
        membership.content_policy_digest,
        lifecycle.current_decision_id,
        lifecycle.generation,
    ):
        return None
    return admission


def _usage_request(
    query: CanonicalDiskSourceQuery,
    candidate: CanonicalDiskSourceCandidate,
    evaluated_at: datetime,
) -> SourceUsageRequest:
    digest = sha256(query.model_dump_json().encode("utf-8")).hexdigest()
    return SourceUsageRequest(
        workspace_id=query.workspace_id,
        environment_id=query.environment_id,
        usage=SourceUsageKind.CRM_RELATIONSHIP_MATCH,
        artifact=SourceUsageArtifact(
            artifact_class=RetentionArtifactClass.CRM_RELATIONSHIP_EVIDENCE,
            artifact_id=query.mapping_profile_id,
            artifact_version=query.mapping_profile_version,
            artifact_digest=digest,
        ),
        dependencies=(
            SourceUsageDependency(
                source_object_id=candidate.source.source_object_id,
                source_revision_id=candidate.revision.revision_id,
                dependency_id=candidate.receipt.association_id,
                artifact_member_id=query.mapping_profile_id,
            ),
        ),
        evaluated_at=evaluated_at,
    )


def _status_for_usage(
    reason: SourceUsageReason, lifecycle: CurrentLifecycleEligibility
) -> CanonicalSourceFenceStatus:
    if reason is SourceUsageReason.AUTHORITY_UNAVAILABLE:
        return CanonicalSourceFenceStatus.AUTHORITY_UNAVAILABLE
    if reason is SourceUsageReason.PROCESSING_INELIGIBLE:
        return CanonicalSourceFenceStatus.STALE_CONTEXT
    if lifecycle.status is LifecycleEligibilityStatus.READ_WITHDRAWN:
        return CanonicalSourceFenceStatus.GOVERNANCE_WITHDRAWN
    return CanonicalSourceFenceStatus.SOURCE_INELIGIBLE


def _outcome_for_status(status: CanonicalSourceFenceStatus) -> CrmEligibilityOutcome:
    return {
        CanonicalSourceFenceStatus.CURRENT: CrmEligibilityOutcome.ELIGIBLE_VERIFIED,
        CanonicalSourceFenceStatus.STALE_CONTEXT: CrmEligibilityOutcome.STALE_CONTEXT,
        CanonicalSourceFenceStatus.GOVERNANCE_WITHDRAWN: (
            CrmEligibilityOutcome.GOVERNANCE_WITHDRAWN
        ),
        CanonicalSourceFenceStatus.SOURCE_INELIGIBLE: (
            CrmEligibilityOutcome.SOURCE_INELIGIBLE
        ),
        CanonicalSourceFenceStatus.AUTHORITY_UNAVAILABLE: (
            CrmEligibilityOutcome.AUTHORITY_UNAVAILABLE
        ),
    }[status]


__all__ = [
    "BITRIX24_DISK_OBJECT_NAMESPACE",
    "BITRIX24_DISK_SOURCE_GENERATION",
    "BITRIX24_DISK_SOURCE_NAMESPACE",
    "CanonicalDiskSourceCandidate",
    "CanonicalDiskSourceFinder",
    "CanonicalDiskSourceQuery",
    "CanonicalSourceAuthorityFence",
    "CanonicalSourceFenceStatus",
    "CrmFilesItemIdentity",
    "CrmMappedDiskRevisionEvidence",
    "CrmRelationshipV2SourceMatchRequest",
    "CrmRelationshipV2SourceMatchResult",
    "CrmRelationshipV2SourceMatcher",
    "DiskRevisionIdentity",
    "ExistingCanonicalDiskSourceAuthority",
]
