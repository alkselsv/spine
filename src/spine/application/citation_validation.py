"""Fail-closed citation validation at the protected disclosure boundary.

Identity is proven against persisted canonical records. Current lifecycle,
retention, and access are delegated to ``ContentAuthorizationDecisionService``.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import Enum
from typing import Literal, Protocol, TypeAlias
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import ConfigDict, Field, field_validator, model_validator

from spine.application.content_authorization import (
    AuthorizationContextMode,
    AuthorizationReasonCode,
    ContentAuthorizationDecision,
    ContentAuthorizationDecisionService,
    TrustedAuthorizationContext,
)
from spine.application.persistence.context_bundles import ContextBundleRepository
from spine.application.persistence.errors import (
    ContextBundleDigestMismatchError,
    ContextBundleEvidenceUnavailableError,
    ContextBundleNotFoundError,
)
from spine.application.projection_locator import canonical_projection_locator_digest
from spine.domain.capabilities.answer_question import Citation
from spine.domain.common import DefinitionModel
from spine.domain.context_bundles import (
    ContextBundle,
    ContextBundleEvidenceReference,
    ContextBundleLineageRef,
    PolicyDecisionGeneration,
)
from spine.domain.ingestion import ProcessingCoverage, ProcessingReceipt
from spine.domain.projections import (
    CanonicalAsOf,
    ProjectionActivation,
    ProjectionConfigVersion,
    ProjectionReceipt,
    ProjectionSnapshot,
    ProjectionSnapshotMember,
    ProjectionSnapshotMemberDisposition,
    SourceProjectionReference,
)
from spine.domain.sources import (
    CanonicalBoundary,
    RevisionKind,
    SourceObject,
    SourceRevision,
    assert_revision_digest,
)

_DIGEST = r"^[0-9a-f]{64}$"


class CitationValidationReasonCode(str, Enum):
    """Bounded non-content reasons safe for persisted disclosure decisions."""

    VALID = "valid"
    IDENTITY_INVALID = "identity_invalid"
    EVIDENCE_INVALIDATED = "evidence_invalidated"
    AUTHORIZATION_DENIED = "authorization_denied"
    AUTHORITY_STALE = "authority_stale"
    VALIDATION_UNAVAILABLE = "validation_unavailable"


class CanonicalCitationContributor(DefinitionModel):
    """Canonical source identity for one contributor to an evidence item."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reference: ContextBundleEvidenceReference
    source_object: SourceObject
    source_revision: SourceRevision
    processing_receipt: ProcessingReceipt
    source_projection_reference: SourceProjectionReference
    projection_receipt: ProjectionReceipt
    projection_snapshot_member: ProjectionSnapshotMember

    @model_validator(mode="after")
    def validate_scope(self) -> "CanonicalCitationContributor":
        reference, source, revision = self.reference, self.source_object, self.source_revision
        processing = self.processing_receipt
        projection = self.source_projection_reference
        receipt = self.projection_receipt
        member = self.projection_snapshot_member
        if (
            source.source_object_id != reference.source_object_id
            or revision.source_object_id != source.source_object_id
            or revision.revision_id != reference.source_revision_id
            or (source.workspace_id, source.environment_id)
            != (reference.workspace_id, reference.environment_id)
            or (revision.workspace_id, revision.environment_id)
            != (reference.workspace_id, reference.environment_id)
            or processing.processing_receipt_id != reference.processing_receipt_id
            or processing.source_object_id != source.source_object_id
            or processing.source_revision_id != revision.revision_id
            or (processing.workspace_id, processing.environment_id)
            != (reference.workspace_id, reference.environment_id)
            or projection.source_object_id != source.source_object_id
            or projection.source_revision_id != revision.revision_id
            or projection.processing_receipt_id != processing.processing_receipt_id
            or (projection.workspace_id, projection.environment_id)
            != (reference.workspace_id, reference.environment_id)
            or receipt.projection_receipt_id != projection.projection_receipt_id
            or (receipt.workspace_id, receipt.environment_id)
            != (reference.workspace_id, reference.environment_id)
            or member.source_object_id != source.source_object_id
            or member.source_revision_id != revision.revision_id
            or (member.workspace_id, member.environment_id)
            != (reference.workspace_id, reference.environment_id)
            or member.canonical_boundary_member_id != source.source_object_id
            or member.projection_receipt_id != receipt.projection_receipt_id
            or member.projection_partition_id != receipt.projection_partition_id
        ):
            raise ValueError("canonical citation contributor identity is inconsistent")
        return self


class CanonicalCitationEvidence(DefinitionModel):
    """Canonical source/projection identity for one derived evidence item."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_id: str = Field(min_length=1, max_length=128)
    evidence_digest: str = Field(pattern=_DIGEST)
    canonical_boundary: CanonicalBoundary
    canonical_as_of: CanonicalAsOf
    projection_snapshot: ProjectionSnapshot
    projection_activation: ProjectionActivation
    projection_config_version: ProjectionConfigVersion
    contributors: tuple[CanonicalCitationContributor, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_identity(self) -> "CanonicalCitationEvidence":
        if len({item.reference.evidence_id for item in self.contributors}) != len(self.contributors):
            raise ValueError("canonical citation contributors must be unique")
        return self


class CitationCanonicalEvidenceResolver(Protocol):
    """Resolve canonical lineage without treating projections as authority."""

    async def resolve_citation_evidence(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        projection_snapshot_id: UUID,
        evidence_id: str,
    ) -> tuple[CanonicalCitationEvidence, ...]: ...


class CitationValidationFacts(DefinitionModel):
    """Immutable content-free facts retained for an allowed disclosure."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["citation-validation-facts:v1"] = "citation-validation-facts:v1"
    citation_id: UUID
    context_bundle_id: UUID
    context_bundle_version: int = Field(ge=1)
    context_bundle_digest: str = Field(pattern=_DIGEST)
    policy_decision_generations: tuple[PolicyDecisionGeneration, ...] = Field(
        min_length=1
    )

    @field_validator("citation_id", "context_bundle_id")
    @classmethod
    def validate_ids(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("citation validation references must be non-zero")
        return value


class ValidCitation(DefinitionModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["citation-validation-result:v1"] = "citation-validation-result:v1"
    status: Literal["valid"] = "valid"
    reason_code: Literal[CitationValidationReasonCode.VALID] = CitationValidationReasonCode.VALID
    validation_reference: UUID
    facts: CitationValidationFacts

    @field_validator("validation_reference")
    @classmethod
    def validate_reference(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("validation_reference must be non-zero")
        return value


class RejectedCitation(DefinitionModel):
    """Safe rejection: no source identity, existence, count, or provider detail."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["citation-validation-result:v1"] = "citation-validation-result:v1"
    status: Literal["rejected"] = "rejected"
    reason_code: Literal[
        CitationValidationReasonCode.IDENTITY_INVALID,
        CitationValidationReasonCode.EVIDENCE_INVALIDATED,
        CitationValidationReasonCode.AUTHORIZATION_DENIED,
        CitationValidationReasonCode.AUTHORITY_STALE,
        CitationValidationReasonCode.VALIDATION_UNAVAILABLE,
    ]
    validation_reference: UUID
    retryable: bool = False

    @field_validator("validation_reference")
    @classmethod
    def validate_reference(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("validation_reference must be non-zero")
        return value


CitationValidationOutcome: TypeAlias = ValidCitation | RejectedCitation


class CitationValidationService:
    """Validate one citation from exact bundle identity through current access."""

    def __init__(
        self,
        *,
        context_bundles: ContextBundleRepository,
        canonical_evidence: CitationCanonicalEvidenceResolver,
        authorization: ContentAuthorizationDecisionService,
        validation_reference_factory: Callable[[], UUID] | None = None,
    ) -> None:
        self._context_bundles = context_bundles
        self._canonical_evidence = canonical_evidence
        self._authorization = authorization
        self._validation_reference_factory = validation_reference_factory

    async def validate(
        self,
        *,
        citation: Citation,
        expected_context_bundle: ContextBundleLineageRef,
        authorization_context: TrustedAuthorizationContext,
    ) -> CitationValidationOutcome:
        try:
            if (
                citation.context_bundle != expected_context_bundle
                or not _citation_scope_matches_context(citation, authorization_context)
            ):
                return self._reject(citation, CitationValidationReasonCode.IDENTITY_INVALID)
            bundle = await self._context_bundles.resolve(
                bundle_id=citation.context_bundle.bundle.bundle_id,
                bundle_version=citation.context_bundle.bundle.bundle_version,
                bundle_digest=citation.context_bundle.bundle.bundle_digest,
            )
            if not _citation_matches_bundle(citation, bundle):
                return self._reject(citation, CitationValidationReasonCode.IDENTITY_INVALID)
            records = await self._canonical_evidence.resolve_citation_evidence(
                workspace_id=bundle.workspace_id,
                environment_id=bundle.environment_id,
                projection_snapshot_id=bundle.projection_snapshot.projection_snapshot_id,
                evidence_id=citation.evidence_id,
            )
            if len(records) != 1 or not _canonical_record_matches_bundle(records[0], bundle):
                return self._reject(citation, CitationValidationReasonCode.IDENTITY_INVALID)
            record = records[0]
            if citation.evidence_reference not in tuple(
                contributor.reference for contributor in record.contributors
            ):
                return self._reject(citation, CitationValidationReasonCode.IDENTITY_INVALID)

            decision = await _authorize(
                self._authorization,
                authorization_context,
                tuple(
                    dict.fromkeys(
                        contributor.source_object.source_object_id
                        for contributor in record.contributors
                    )
                ),
            )
            reason = _authorization_rejection(decision, record, bundle)
            if reason is not None:
                return self._reject(
                    citation,
                    reason,
                    retryable=reason is CitationValidationReasonCode.VALIDATION_UNAVAILABLE,
                )
            return ValidCitation(
                validation_reference=self._validation_reference(
                    citation, CitationValidationReasonCode.VALID
                ),
                facts=CitationValidationFacts(
                    citation_id=citation.citation_id,
                    context_bundle_id=bundle.bundle_id,
                    context_bundle_version=bundle.bundle_version,
                    context_bundle_digest=bundle.bundle_digest,
                    policy_decision_generations=tuple(
                        generation
                        for generation in bundle.policy_decision_generations
                        if generation.source_object_id
                        in {
                            contributor.source_object.source_object_id
                            for contributor in record.contributors
                        }
                    ),
                ),
            )
        except (ContextBundleNotFoundError, ContextBundleDigestMismatchError):
            return self._reject(citation, CitationValidationReasonCode.IDENTITY_INVALID)
        except ContextBundleEvidenceUnavailableError:
            return self._reject(citation, CitationValidationReasonCode.EVIDENCE_INVALIDATED)
        except Exception:
            return self._reject(
                citation,
                CitationValidationReasonCode.VALIDATION_UNAVAILABLE,
                retryable=True,
            )

    def _reject(
        self,
        citation: Citation,
        reason: CitationValidationReasonCode,
        *,
        retryable: bool = False,
    ) -> RejectedCitation:
        return RejectedCitation(
            reason_code=reason,
            validation_reference=self._validation_reference(citation, reason),
            retryable=retryable,
        )

    def _validation_reference(
        self, citation: Citation, reason: CitationValidationReasonCode
    ) -> UUID:
        if self._validation_reference_factory is not None:
            return self._validation_reference_factory()
        identity = (
            f"spine:citation-validation-result:v1:{citation.citation_id}:"
            f"{citation.context_bundle.bundle.bundle_digest}:{reason.value}"
        )
        return uuid5(NAMESPACE_URL, identity)


def _citation_scope_matches_context(citation: Citation, context: TrustedAuthorizationContext) -> bool:
    return citation.workspace_id == context.workspace_id and citation.environment_id == context.environment_id


def _citation_matches_bundle(citation: Citation, bundle: ContextBundle) -> bool:
    matches = tuple(
        reference for reference in bundle.evidence_references if reference.evidence_id == citation.evidence_id
    )
    return bool(
        ContextBundle.calculate_digest(bundle) == bundle.bundle_digest
        and citation.context_bundle == bundle.lineage_reference
        and len(matches) == 1
        and matches[0] == citation.evidence_reference
        and citation.evidence_reference.projection_snapshot_id == bundle.projection_snapshot.projection_snapshot_id
        and citation.evidence_reference.activation_generation == bundle.activation_generation
    )


def _canonical_record_matches_bundle(record: CanonicalCitationEvidence, bundle: ContextBundle) -> bool:
    bundle_references = set(bundle.evidence_references)
    if (
        record.canonical_boundary != bundle.canonical_boundary
        or record.canonical_as_of != bundle.canonical_as_of
        or record.projection_snapshot != bundle.projection_snapshot
        or record.projection_activation != bundle.projection_activation
        or record.projection_config_version != bundle.projection_config_version
    ):
        return False
    for contributor in record.contributors:
        reference, revision = contributor.reference, contributor.source_revision
        processing = contributor.processing_receipt
        projection = contributor.source_projection_reference
        receipt = contributor.projection_receipt
        member = contributor.projection_snapshot_member
        try:
            assert_revision_digest(revision)
            locator_digest = canonical_projection_locator_digest(
                locator_contract_version=reference.locator_contract_version or "",
                locator=reference.locator,
            )
        except Exception:
            return False
        if (
            reference not in bundle_references
            or reference.workspace_id != bundle.workspace_id
            or reference.environment_id != bundle.environment_id
            or reference.projection_snapshot_id != bundle.projection_snapshot.projection_snapshot_id
            or reference.activation_generation != bundle.activation_generation
            or reference.status.value != "valid"
            or revision.kind is not RevisionKind.CONTENT
            or revision.revision_digest != reference.revision_digest
            or revision.original_sha256 != reference.original_sha256
            or processing.coverage is ProcessingCoverage.FAILED
            or processing.original_sha256 != revision.original_sha256
            or processing.locator_contract_version != reference.locator_contract_version
            or projection.projection_run_id != bundle.projection_snapshot.projection_run_id
            or projection.projection_candidate_id
            != bundle.projection_snapshot.projection_candidate_id
            or projection.projection_config_version_id
            != bundle.projection_config_version.projection_config_version_id
            or projection.projected_kind != reference.projected_kind
            or projection.projected_object_id != reference.evidence_id
            or projection.locator_schema_version != reference.locator_contract_version
            or projection.locator_reference != reference.locator.value
            or projection.locator_digest != locator_digest
            or receipt.projection_run_id != bundle.projection_snapshot.projection_run_id
            or receipt.projection_candidate_id
            != bundle.projection_snapshot.projection_candidate_id
            or member.disposition is not ProjectionSnapshotMemberDisposition.INCLUDED
            or member.projection_snapshot_id
            != bundle.projection_snapshot.projection_snapshot_id
            or member.canonical_boundary_id != bundle.canonical_boundary.boundary_id
            or (member.workspace_id, member.environment_id)
            != (bundle.workspace_id, bundle.environment_id)
            or member.canonical_boundary_member_id != reference.source_object_id
            or member.projection_receipt_id != receipt.projection_receipt_id
            or member.projection_partition_id != receipt.projection_partition_id
            or reference.locator_contract_version != bundle.projection_config_version.locator_contract_version
            or reference.locator_digest != locator_digest
        ):
            return False
    primary = tuple(
        contributor.reference
        for contributor in record.contributors
        if contributor.reference.evidence_id == record.evidence_id
    )
    return len(primary) == 1 and primary[0].evidence_digest == record.evidence_digest


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


def _authorization_rejection(
    decision: ContentAuthorizationDecision,
    record: CanonicalCitationEvidence,
    bundle: ContextBundle,
) -> CitationValidationReasonCode | None:
    if not decision.allowed:
        if decision.reason_code is AuthorizationReasonCode.AUTHORITY_STALE:
            return CitationValidationReasonCode.AUTHORITY_STALE
        if decision.reason_code in {AuthorizationReasonCode.DECISION_UNAVAILABLE, AuthorizationReasonCode.AUDIT_UNAVAILABLE}:
            return CitationValidationReasonCode.VALIDATION_UNAVAILABLE
        if decision.reason_code is AuthorizationReasonCode.AUTHORIZATION_DENIED:
            return CitationValidationReasonCode.AUTHORIZATION_DENIED
        return CitationValidationReasonCode.EVIDENCE_INVALIDATED
    expected = {
        contributor.source_object.source_object_id: contributor.source_revision.revision_id
        for contributor in record.contributors
    }
    actual = {item.source_object_id: item for item in decision.policy_evidence}
    pinned = {
        item.source_object_id: item for item in bundle.policy_decision_generations
    }
    if (
        set(decision.source_object_ids) != set(expected)
        or set(actual) != set(expected)
        or not set(expected) <= set(pinned)
        or any(
            actual[source_id].source_revision_id != revision_id
            or actual[source_id].retention_eligible is not True
            for source_id, revision_id in expected.items()
        )
    ):
        return CitationValidationReasonCode.EVIDENCE_INVALIDATED
    if any(
        pinned[source_id].policy_version_id != actual[source_id].policy_version_id
        or pinned[source_id].policy_generation != actual[source_id].policy_generation
        or pinned[source_id].membership_generation != decision.membership_generation
        or pinned[source_id].authority_generation != decision.authority_generation
        or pinned[source_id].service_authority_generation
        != decision.service_authority_generation
        for source_id in expected
    ):
        return CitationValidationReasonCode.AUTHORITY_STALE
    return None


__all__ = [
    "CanonicalCitationContributor",
    "CanonicalCitationEvidence",
    "CitationCanonicalEvidenceResolver",
    "CitationValidationFacts",
    "CitationValidationOutcome",
    "CitationValidationReasonCode",
    "CitationValidationService",
    "RejectedCitation",
    "ValidCitation",
]
