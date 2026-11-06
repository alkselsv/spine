"""Offline private ASR corpus admission, storage ports and report boundary."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from datetime import datetime
import hashlib
import json
from typing import Literal, Protocol
from uuid import UUID

from pydantic import ConfigDict, Field, StrictBool, StrictInt, model_validator

from spine.application.persistence.context import (
    EnvironmentScope,
    TrustedContextVerifier,
    TrustedPersistenceContext,
)
from spine.domain.common import DefinitionModel, VersionIdentifier
from spine.domain.evaluations.private_asr_corpus import (
    AggregateCategory,
    AggregateMetric,
    ApprovedPrivacyPolicy,
    AudioDigestVerification,
    CorpusSplit,
    PrivateReference,
    PrivacyPolicy,
    QualificationSufficiency,
    SanitizedAggregate,
    SanitizedCorpusReport,
)
from spine.domain.sources.access_policy import AccessPolicyVersion
from spine.domain.sources.models import SourceObject, SourceRevision
from spine.domain.sources.retention import RetentionArtifactClass


class PrivateASRApplicationContract(DefinitionModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    contract_version: Literal["r2-private-asr-admission-contract:v1"] = "r2-private-asr-admission-contract:v1"


class CorpusAdmissionDenied(Exception):
    """Generic fail-closed admission error; does not disclose private details."""


class CorpusVerificationError(Exception):
    """Source or authority changed during a permitted content verification."""


class CorpusRecheckUnavailable(Exception):
    """A current authority check could not be completed safely."""


class PrivacySanitizationError(Exception):
    """A candidate aggregate is not safe for the requested report boundary."""


class MetadataInventoryEntry(PrivateASRApplicationContract):
    """A candidate containing metadata only; it has no bytes or path field."""

    schema_version: Literal["private-asr-metadata-inventory-entry:v1"] = "private-asr-metadata-inventory-entry:v1"
    source_object: SourceObject
    source_revision: SourceRevision
    access_policy_version: UUID
    policy_version: VersionIdentifier
    media_type: str
    byte_length: StrictInt = Field(ge=0)
    duration_ms: StrictInt = Field(ge=0)
    previously_verified_audio_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )


class PurposeAdmissionRequest(PrivateASRApplicationContract):
    schema_version: Literal["private-asr-purpose-admission-request:v1"] = "private-asr-purpose-admission-request:v1"
    source_object: SourceObject
    source_revision: SourceRevision
    workspace_id: UUID
    environment_id: UUID
    purpose_approval_ref: PrivateReference
    legal_data_evidence_ref: PrivateReference
    retention_profile_ref: PrivateReference
    authorized_actor_id: UUID
    access_policy_version: UUID
    policy_version: VersionIdentifier
    allowed_environment_id: UUID


class VerifiedAuthority(PrivateASRApplicationContract):
    schema_version: Literal["private-asr-verified-authority:v1"] = "private-asr-verified-authority:v1"
    purpose: str
    process_granted: bool
    human_read_granted: bool
    legal_data_evidence_confirmed: bool
    purpose_approved: bool
    actor_authorized: bool
    retention_current: bool
    audit_permitted: bool
    environment_allowed: bool
    current: bool
    revoked: bool
    expires_at: datetime
    revocation_epoch: StrictInt = Field(ge=0)
    attestation_ref: PrivateReference
    read_grant_ref: PrivateReference
    process_grant_ref: PrivateReference

    @model_validator(mode="after")
    def validate_authority_expiry(self) -> "VerifiedAuthority":
        if self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None:
            raise ValueError("authority expiry must be timezone-aware")
        return self


class AdmittedSourceRevision(PrivateASRApplicationContract):
    schema_version: Literal["private-asr-admitted-source-revision:v1"] = "private-asr-admitted-source-revision:v1"
    source_object: SourceObject
    source_revision: SourceRevision
    source_revision_number: StrictInt = Field(gt=0)
    workspace_id: UUID
    environment_id: UUID
    purpose_approval_ref: PrivateReference
    legal_data_evidence_ref: PrivateReference
    retention_profile_ref: PrivateReference
    actor_id: UUID
    audit_receipt_ref: PrivateReference
    access_policy_version: UUID
    policy_version: VersionIdentifier
    authority: VerifiedAuthority
    admitted_at: datetime

    @model_validator(mode="after")
    def validate_exact_binding(self) -> "AdmittedSourceRevision":
        source, revision = self.source_object, self.source_revision
        if (source.source_object_id, source.workspace_id, source.environment_id) != (
            revision.source_object_id, revision.workspace_id, revision.environment_id
        ):
            raise ValueError("admitted source revision binding is inconsistent")
        if (self.workspace_id, self.environment_id) != (
            source.workspace_id, source.environment_id
        ):
            raise ValueError("admitted source scope is inconsistent")
        if self.admitted_at.tzinfo is None or self.admitted_at.utcoffset() is None:
            raise ValueError("admitted_at must be timezone-aware")
        return self


class ContentVerificationResult(PrivateASRApplicationContract):
    schema_version: Literal["private-asr-content-verification:v1"] = "private-asr-content-verification:v1"
    admitted: AdmittedSourceRevision
    digest_verification: AudioDigestVerification
    byte_length: StrictInt = Field(ge=0)
    staged_ref: PrivateReference
    original_ref: PrivateReference


class PrivateReportReceipt(PrivateASRApplicationContract):
    """Restricted store receipt for private detailed evidence."""

    schema_version: Literal["private-asr-report-receipt:v1"] = "private-asr-report-receipt:v1"
    report_ref: PrivateReference
    manifest_ref: PrivateReference
    audit_receipt_ref: PrivateReference
    retention_profile_ref: PrivateReference


class PrivateReportSourceBinding(PrivateASRApplicationContract):
    """Exact private report case to immutable source-revision lineage."""

    record_id: UUID
    dataset_id: UUID
    source_object_id: UUID
    source_revision_id: UUID
    source_revision_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    split: CorpusSplit


class PrivateArtifactSourceLineage(PrivateASRApplicationContract):
    """Source lineage resolved from persisted artifact/source rows for deletion."""

    source_object_id: UUID
    source_revision_id: UUID
    source_revision_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    record_id: UUID | None = None
    dataset_id: UUID | None = None
    split: CorpusSplit | None = None

    @model_validator(mode="after")
    def validate_manifest_binding(self) -> "PrivateArtifactSourceLineage":
        manifest_fields = (self.record_id, self.dataset_id, self.split)
        if any(value is not None for value in manifest_fields) and any(
            value is None for value in manifest_fields
        ):
            raise ValueError("artifact source manifest lineage must be complete")
        if self.source_object_id.int == 0 or self.source_revision_id.int == 0:
            raise ValueError("artifact source lineage identifiers must be non-zero")
        return self


class PrivateArtifactDeletionRequest(PrivateASRApplicationContract):
    """Transaction-scoped request to expire or discard one persisted artifact.

    The storage adapter constructs this only after resolving exact scoped rows.
    The authority must resolve current retention/deletion rules and holds using
    the supplied transaction; caller references alone never authorize deletion.
    """

    schema_version: Literal["private-asr-artifact-deletion-request:v1"] = (
        "private-asr-artifact-deletion-request:v1"
    )
    artifact_id: UUID
    workspace_id: UUID
    environment_id: UUID
    artifact_kind: Literal["staging", "original", "annotation", "report", "aggregate_export"]
    action: Literal["retention_expiration", "staging_cleanup"]
    expected_state_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    retention_profile_ref: PrivateReference
    retention_deadline: datetime
    requested_at: datetime
    source_lineage: tuple[PrivateArtifactSourceLineage, ...]
    manifest_version: str | None = None
    manifest_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_request(self) -> "PrivateArtifactDeletionRequest":
        if any(
            value.int == 0
            for value in (self.artifact_id, self.workspace_id, self.environment_id)
        ) or not self.source_lineage:
            raise ValueError("artifact deletion request requires exact lineage")
        if any(
            value.tzinfo is None or value.utcoffset() is None
            for value in (self.retention_deadline, self.requested_at)
        ):
            raise ValueError("artifact deletion timestamps must be timezone-aware")
        if self.action == "staging_cleanup" and self.artifact_kind != "staging":
            raise ValueError("staging cleanup applies only to staging artifacts")
        if self.artifact_kind == "aggregate_export" and self.action != "retention_expiration":
            raise ValueError("aggregate exports may only be deleted on retention expiry")
        if (self.manifest_version is None) != (self.manifest_sha256 is None):
            raise ValueError("artifact manifest lineage must be complete")
        return self

    def calculate_digest(self) -> str:
        payload = self.model_dump(mode="json")
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


class PrivateArtifactDeletionSourceBinding(PrivateASRApplicationContract):
    """One exact source revision bound to current approved retention evidence."""

    workspace_id: UUID
    environment_id: UUID
    source_object_id: UUID
    source_revision_id: UUID
    revision_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    applicability_id: UUID
    applicability_generation: StrictInt = Field(ge=1)
    expected_current_applicability_id: UUID
    expected_current_generation: StrictInt = Field(ge=0)
    artifact_class: RetentionArtifactClass
    retention_policy_id: UUID
    retention_policy_version_id: UUID
    retention_policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    approving_authority_reference: UUID
    approving_authority_version_id: UUID
    approving_authority_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    applicability_evidence_reference: UUID
    applicability_evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    approval_evidence_reference: UUID
    terms_version: str = Field(pattern=r"^[a-z][a-z0-9_.:-]{0,63}$")
    terms_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    deletion_obligation_reference: UUID
    backup_obligation_reference: UUID
    resolved_exception_references: tuple[UUID, ...] = ()
    resolved_legal_hold_references: tuple[UUID, ...] = ()
    active_legal_hold_references: tuple[UUID, ...] = ()

    @model_validator(mode="after")
    def validate_binding(self) -> "PrivateArtifactDeletionSourceBinding":
        refs = (self.workspace_id, self.environment_id, self.source_object_id,
                self.source_revision_id, self.applicability_id,
                self.retention_policy_id, self.retention_policy_version_id,
                self.approving_authority_reference, self.approving_authority_version_id,
                self.applicability_evidence_reference,
                self.approval_evidence_reference, self.deletion_obligation_reference,
                self.backup_obligation_reference, *self.resolved_exception_references,
                *self.resolved_legal_hold_references, *self.active_legal_hold_references)
        if any(ref.int == 0 for ref in refs):
            raise ValueError("deletion source binding references must be non-zero")
        if (self.expected_current_applicability_id != self.applicability_id
                or self.expected_current_generation != self.applicability_generation):
            raise ValueError("deletion source binding is not the current applicability head")
        for values in (self.resolved_exception_references,
                       self.resolved_legal_hold_references,
                       self.active_legal_hold_references):
            if len(values) != len(set(values)):
                raise ValueError("deletion source binding references must be unique")
        if not set(self.active_legal_hold_references).issubset(self.resolved_legal_hold_references):
            raise ValueError("active legal holds require resolved authority")
        return self

    def calculate_digest(self) -> str:
        payload = self.model_dump(mode="json")
        return hashlib.sha256(
            json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()


class PrivateArtifactDeletionRequestV2(PrivateASRApplicationContract):
    """Versioned whole-artifact deletion request with exact per-source bindings.

    V1 remains immutable for persisted compatibility. Callers must explicitly
    construct v2; no conversion or inferred binding from v1 is provided.
    """

    schema_version: Literal["private-asr-artifact-deletion-request:v2"] = (
        "private-asr-artifact-deletion-request:v2"
    )
    artifact_id: UUID
    workspace_id: UUID
    environment_id: UUID
    purpose: str = Field(pattern=r"^[a-z][a-z0-9_.:-]{0,63}$")
    artifact_kind: Literal["staging", "original", "annotation", "report", "aggregate_export"]
    action: Literal["retention_expiration", "staging_cleanup"]
    expected_state_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    retention_profile_ref: PrivateReference
    retention_deadline: datetime
    requested_at: datetime
    expected_source_lineage: tuple[PrivateArtifactSourceLineage, ...] = Field(min_length=1)
    source_bindings: tuple[PrivateArtifactDeletionSourceBinding, ...] = Field(min_length=1)
    authority_version: str = Field(pattern=r"^[a-z][a-z0-9_.:-]{0,63}$")
    valid_until: datetime

    @model_validator(mode="after")
    def validate_request(self) -> "PrivateArtifactDeletionRequestV2":
        if any(value.int == 0 for value in (self.artifact_id, self.workspace_id, self.environment_id)):
            raise ValueError("artifact deletion request requires exact identity")
        times = (self.retention_deadline, self.requested_at, self.valid_until)
        if any(value.tzinfo is None or value.utcoffset() is None for value in times):
            raise ValueError("artifact deletion timestamps must be timezone-aware")
        if self.valid_until <= self.requested_at:
            raise ValueError("deletion authority validity must extend beyond request time")
        if (self.action == "retention_expiration"
                and self.requested_at < self.retention_deadline):
            raise ValueError("retention expiration cannot precede its deadline")
        if self.action == "staging_cleanup" and self.artifact_kind != "staging":
            raise ValueError("staging cleanup applies only to staging artifacts")
        if self.artifact_kind == "aggregate_export" and self.action != "retention_expiration":
            raise ValueError("aggregate exports may only be deleted on retention expiry")
        expected_artifact_class = {
            "staging": RetentionArtifactClass.PRIVATE_ASR_STAGING,
            "original": RetentionArtifactClass.PRIVATE_ASR_SOURCE_ORIGINAL,
            "annotation": RetentionArtifactClass.PRIVATE_ASR_ANNOTATION,
            "report": RetentionArtifactClass.PRIVATE_ASR_DETAILED_REPORT,
            "aggregate_export": RetentionArtifactClass.PRIVATE_ASR_SANITIZED_AGGREGATE,
        }[self.artifact_kind]
        expected_workspace = self.workspace_id
        expected_environment = self.environment_id
        if any(binding.workspace_id != expected_workspace or binding.environment_id != expected_environment
               for binding in self.source_bindings):
            raise ValueError("source bindings must share artifact workspace and environment")
        if any(binding.artifact_class is not expected_artifact_class for binding in self.source_bindings):
            raise ValueError("source retention class must match artifact kind")
        identities = [(b.workspace_id, b.environment_id, b.source_object_id,
                       b.source_revision_id, b.revision_digest) for b in self.source_bindings]
        if len(identities) != len(set(identities)):
            raise ValueError("source bindings must have distinct exact identities")
        expected = [(self.workspace_id, self.environment_id, source.source_object_id,
                     source.source_revision_id, source.source_revision_digest)
                    for source in self.expected_source_lineage]
        if len(expected) != len(set(expected)):
            raise ValueError("expected source lineage must have distinct exact identities")
        if set(identities) != set(expected):
            raise ValueError("source bindings must exactly cover artifact lineage")
        object.__setattr__(self, "expected_source_lineage", tuple(sorted(
            self.expected_source_lineage, key=lambda source: (
                str(source.source_object_id), str(source.source_revision_id),
                source.source_revision_digest))))
        object.__setattr__(self, "source_bindings", tuple(sorted(self.source_bindings, key=lambda b: (
            str(b.workspace_id), str(b.environment_id), str(b.source_object_id),
            str(b.source_revision_id), b.revision_digest))))
        return self

    def calculate_digest(self) -> str:
        payload = self.model_dump(mode="json")
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class PrivateArtifactDeletionAuthorization(PrivateASRApplicationContract):
    """Current authority result fenced to one exact deletion request."""

    schema_version: Literal["private-asr-artifact-deletion-authorization:v1"] = (
        "private-asr-artifact-deletion-authorization:v1"
    )
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    authorization_ref: PrivateReference
    authority_version: str = Field(pattern=r"^[a-z][a-z0-9_.:-]{0,63}$")
    revocation_epoch: StrictInt = Field(ge=0)
    valid_until: datetime
    authorized: StrictBool

    @model_validator(mode="after")
    def validate_authorization(self) -> "PrivateArtifactDeletionAuthorization":
        if (
            self.authorization_ref.reference_kind != "private_asr_deletion_authorization"
            or self.authorization_ref.reference_id.int == 0
            or self.valid_until.tzinfo is None
            or self.valid_until.utcoffset() is None
        ):
            raise ValueError("artifact deletion authorization is invalid")
        return self


class PrivateArtifactDeletionFenceContributorOutcome(PrivateASRApplicationContract):
    """Fence evidence for one source contributor in a composite deletion."""

    schema_version: Literal["private-asr-deletion-fence-contributor:v2"] = (
        "private-asr-deletion-fence-contributor:v2"
    )
    authority_kind: Literal["postgresql_f1", "external_f2"]
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    authorization_reference: UUID
    source_binding_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_lineage: PrivateArtifactSourceLineage
    permit_reference: UUID | None = None
    permit_valid_until: datetime | None = None

    @model_validator(mode="after")
    def validate_contributor(self) -> "PrivateArtifactDeletionFenceContributorOutcome":
        if self.authority_kind == "external_f2":
            if self.permit_reference is None or self.permit_reference.int == 0:
                raise ValueError("F2 contributor requires an acquired permit")
            if (
                self.permit_valid_until is None
                or self.permit_valid_until.tzinfo is None
                or self.permit_valid_until.utcoffset() is None
            ):
                raise ValueError("F2 contributor permit validity is required")
        elif self.permit_reference is not None or self.permit_valid_until is not None:
            raise ValueError("F1 contributor cannot carry an external permit")
        if self.authorization_reference.int == 0:
            raise ValueError("deletion fence contributor authorization is required")
        return self


class PrivateArtifactDeletionFenceOutcome(PrivateASRApplicationContract):
    """One all-or-nothing set of per-source fence acquisitions."""

    schema_version: Literal["private-asr-composite-deletion-fence:v1"] = (
        "private-asr-composite-deletion-fence:v1"
    )
    artifact_id: UUID
    workspace_id: UUID
    environment_id: UUID
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    authorization_reference: UUID
    contributors: tuple[PrivateArtifactDeletionFenceContributorOutcome, ...] = Field(
        min_length=1
    )
    acquired_at: datetime

    @model_validator(mode="after")
    def validate_outcome(self) -> "PrivateArtifactDeletionFenceOutcome":
        if any(value.int == 0 for value in (
            self.artifact_id,
            self.workspace_id,
            self.environment_id,
            self.authorization_reference,
        )):
            raise ValueError("composite deletion fence identity must be complete")
        if self.acquired_at.tzinfo is None or self.acquired_at.utcoffset() is None:
            raise ValueError("composite deletion fence time must be timezone-aware")
        identities = tuple(_source_lineage_contract_key(
            contributor.source_lineage
        ) for contributor in self.contributors)
        if len(set(identities)) != len(identities):
            raise ValueError("composite deletion fence lineage must be unique")
        object.__setattr__(
            self,
            "contributors",
            tuple(sorted(self.contributors, key=lambda contributor: (
                str(contributor.source_lineage.source_object_id),
                str(contributor.source_lineage.source_revision_id),
                contributor.source_lineage.source_revision_digest,
            ))),
        )
        return self

    def matches(
        self,
        request: PrivateArtifactDeletionRequestV2,
        authorization: PrivateArtifactDeletionAuthorization,
    ) -> bool:
        return (
            self.artifact_id == request.artifact_id
            and self.workspace_id == request.workspace_id
            and self.environment_id == request.environment_id
            and self.request_digest == request.calculate_digest()
            and self.request_digest == authorization.request_digest
            and self.authorization_reference == authorization.authorization_ref.reference_id
            and request.requested_at <= self.acquired_at < request.valid_until
            and self.acquired_at < authorization.valid_until
            and all(
                item.request_digest == request.calculate_digest()
                and item.authorization_reference
                == authorization.authorization_ref.reference_id
                and any(
                    binding.calculate_digest() == item.source_binding_digest
                    and binding.source_object_id == item.source_lineage.source_object_id
                    and binding.source_revision_id == item.source_lineage.source_revision_id
                    and binding.revision_digest == item.source_lineage.source_revision_digest
                    for binding in request.source_bindings
                )
                and (
                    item.authority_kind != "external_f2"
                    or (
                        item.permit_valid_until is not None
                        and self.acquired_at < item.permit_valid_until
                        and item.permit_valid_until
                        <= min(request.valid_until, authorization.valid_until)
                    )
                )
                for item in self.contributors
            )
            and {
                _source_lineage_contract_key(item)
                for item in (
                    contributor.source_lineage for contributor in self.contributors
                )
            }
            == {
                _source_lineage_contract_key(item)
                for item in request.expected_source_lineage
            }
        )


def _source_lineage_contract_key(
    source: PrivateArtifactSourceLineage,
) -> str:
    return json.dumps(
        source.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


class CompositeSourceRetentionState(PrivateASRApplicationContract):
    """Current, independently resolved policy facts for one source in an artifact."""

    schema_version: Literal["private-artifact-source-retention-state:v1"] = (
        "private-artifact-source-retention-state:v1"
    )
    source_object_id: UUID
    source_revision_id: UUID
    source_revision_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    current_generation: StrictInt = Field(ge=0)
    resolved_generation: StrictInt = Field(ge=0)
    current_applicability_resolved: StrictBool
    required_permission_current: StrictBool
    revoked: StrictBool
    deletion_eligible: StrictBool
    retention_elapsed: StrictBool
    active_hold: StrictBool

    @model_validator(mode="after")
    def validate_identity(self) -> "CompositeSourceRetentionState":
        if self.source_object_id.int == 0 or self.source_revision_id.int == 0:
            raise ValueError("source retention identity must be complete")
        return self

    @property
    def generation_current(self) -> bool:
        return self.current_generation == self.resolved_generation

    @property
    def available(self) -> bool:
        return (
            self.generation_current
            and self.current_applicability_resolved
            and self.required_permission_current
            and not self.revoked
        )


class CompositeArtifactRetentionDecision(PrivateASRApplicationContract):
    """All-or-nothing visibility and deletion decision over exact source lineage.

    This records policy evaluation only. It never mutates source or artifact
    lifecycle state and never treats a deadline as deletion authority.
    """

    schema_version: Literal["private-artifact-composite-retention-decision:v1"] = (
        "private-artifact-composite-retention-decision:v1"
    )
    artifact_id: UUID
    workspace_id: UUID
    environment_id: UUID
    expected_source_lineage: tuple[PrivateArtifactSourceLineage, ...]
    source_states: tuple[CompositeSourceRetentionState, ...]
    whole_artifact_authorization: "CompositeDeletionAuthorizationEvidence | None" = None

    @model_validator(mode="after")
    def validate_complete_composite(self) -> "CompositeArtifactRetentionDecision":
        identities = tuple(
            (
                state.source_object_id,
                state.source_revision_id,
                state.source_revision_digest,
            )
            for state in self.source_states
        )
        expected_identities = tuple(
            (
                source.source_object_id,
                source.source_revision_id,
                source.source_revision_digest,
            )
            for source in self.expected_source_lineage
        )
        if (
            self.artifact_id.int == 0
            or self.workspace_id.int == 0
            or self.environment_id.int == 0
            or not identities
            or not expected_identities
        ):
            raise ValueError("composite retention requires artifact and source lineage")
        if (
            len(set(identities)) != len(identities)
            or len(set(expected_identities)) != len(expected_identities)
            or set(identities) != set(expected_identities)
        ):
            raise ValueError("composite state must match exact source lineage")
        if self.whole_artifact_authorization is not None:
            evidence = self.whole_artifact_authorization
            if (
                evidence.artifact_id != self.artifact_id
                or evidence.workspace_id != self.workspace_id
                or evidence.environment_id != self.environment_id
                or evidence.lineage_sha256 != self.calculate_lineage_digest()
            ):
                raise ValueError("composite deletion authorization binding is invalid")
        if self.whole_artifact_authorization is not None and not self.deletion_eligible:
            raise ValueError("whole artifact authorization requires every source eligible")
        return self

    def calculate_lineage_digest(self) -> str:
        canonical = sorted(
            (
                str(source.source_object_id),
                str(source.source_revision_id),
                source.source_revision_digest,
            )
            for source in self.expected_source_lineage
        )
        payload = json.dumps(canonical, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    @property
    def available(self) -> bool:
        return all(state.available for state in self.source_states)

    @property
    def deletion_eligible(self) -> bool:
        return all(
            state.generation_current
            and state.current_applicability_resolved
            and state.deletion_eligible
            and state.retention_elapsed
            and not state.active_hold
            for state in self.source_states
        )

    @property
    def deletion_permitted(self) -> bool:
        return self.deletion_eligible and self.whole_artifact_authorization is not None


class CompositeDeletionAuthorizationEvidence(PrivateASRApplicationContract):
    """One authority result scoped to the complete immutable composite identity."""

    schema_version: Literal["private-artifact-composite-deletion-authorization:v1"] = (
        "private-artifact-composite-deletion-authorization:v1"
    )
    artifact_id: UUID
    workspace_id: UUID
    environment_id: UUID
    lineage_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    authorization_ref: PrivateReference

    @model_validator(mode="after")
    def validate_binding(self) -> "CompositeDeletionAuthorizationEvidence":
        if (
            any(
                value.int == 0
                for value in (self.artifact_id, self.workspace_id, self.environment_id)
            )
            or self.authorization_ref.reference_kind != "private_asr_deletion_authorization"
            or self.authorization_ref.reference_id.int == 0
        ):
            raise ValueError("composite deletion authorization evidence is invalid")
        return self


class PrivateArtifactDeletionAuthority(Protocol):
    """Resolve current deletion authority inside the artifact transaction.

    Implementations must bind the decision to the supplied request digest and
    keep its approval/revocation epoch valid through the transaction commit.
    For v2, the authority must compare every binding's applicability identity
    and generation with the transaction-current pointer, and resolve the
    referenced policy, approval, exceptions and holds. A request's internal
    consistency never proves that its applicability remains current. External
    authorities need a coordinated fence or bounded commit lease.
    """

    async def authorize_deletion(
        self,
        request: PrivateArtifactDeletionRequest | PrivateArtifactDeletionRequestV2,
        *,
        context: TrustedPersistenceContext,
        transaction: object,
    ) -> PrivateArtifactDeletionAuthorization: ...


class PrivateArtifactDeletionFencePort(Protocol):
    """Fence one exact deletion decision through the storage transaction.

    The authority decision and the fence are separate concerns. Implementations
    must acquire every contributor in canonical source identity order and bind
    the complete source set to one outcome. F2 implementations unwind earlier
    permits if a later contributor fails. The consumer calls `validate_before_commit`
    while the transaction is open, then `complete` only after a known commit or
    rollback. An absent or unsupported contributor denies the whole operation.
    """

    async def acquire(
        self,
        request: PrivateArtifactDeletionRequestV2,
        authorization: PrivateArtifactDeletionAuthorization,
        *,
        context: TrustedPersistenceContext,
        transaction: object,
    ) -> PrivateArtifactDeletionFenceOutcome: ...

    async def validate_before_commit(
        self,
        outcome: PrivateArtifactDeletionFenceOutcome,
        *,
        transaction: object,
    ) -> None: ...

    async def complete(
        self,
        outcome: PrivateArtifactDeletionFenceOutcome,
        *,
        transaction: object,
        committed: bool,
    ) -> None: ...

    async def abandon(
        self,
        outcome: PrivateArtifactDeletionFenceOutcome,
    ) -> None: ...


class PrivateArtifactDeletionFenceContributorPort(Protocol):
    """Acquire and settle the permit for one exact source contributor."""

    async def acquire_contributor(
        self,
        request: PrivateArtifactDeletionRequestV2,
        authorization: PrivateArtifactDeletionAuthorization,
        contributor: PrivateArtifactDeletionSourceBinding,
        *,
        context: TrustedPersistenceContext,
        transaction: object,
    ) -> PrivateArtifactDeletionFenceContributorOutcome: ...

    async def validate_contributor_before_commit(
        self,
        outcome: PrivateArtifactDeletionFenceContributorOutcome,
        *,
        transaction: object,
    ) -> None: ...

    async def complete_contributor(
        self,
        outcome: PrivateArtifactDeletionFenceContributorOutcome,
        *,
        transaction: object,
        committed: bool,
    ) -> None: ...


class PrivateArtifactDeletionFenceResolver(Protocol):
    """Resolve one supported F1/F2 adapter for an exact source contributor."""

    def resolve(
        self,
        contributor: PrivateArtifactDeletionSourceBinding,
        *,
        context: TrustedPersistenceContext,
    ) -> PrivateArtifactDeletionFenceContributorPort: ...


class ScopedPrivateArtifactDeletionAuditPort(Protocol):
    """Append content-free deletion evidence in the artifact transaction."""

    async def append_artifact_deletion(
        self,
        transaction: object,
        *,
        request: PrivateArtifactDeletionRequest | PrivateArtifactDeletionRequestV2,
        authorization: PrivateArtifactDeletionAuthorization,
        context: TrustedPersistenceContext,
    ) -> PrivateReference: ...


class PrivateASRManifestLineageVerifier(Protocol):
    """Verify that report lineage is admitted by the current exact manifest."""

    async def verify_current(
        self,
        *,
        transaction: object,
        context: TrustedPersistenceContext,
        dataset_id: UUID,
        manifest_version: str,
        manifest_sha256: str,
        retention_profile_ref: PrivateReference,
        source_bindings: tuple[PrivateReportSourceBinding, ...],
    ) -> bool: ...


class AggregateMeasurement(PrivateASRApplicationContract):
    """Closed aggregate input; deliberately has no row, locator or text fields."""

    schema_version: Literal["private-asr-aggregate-measurement:v1"] = "private-asr-aggregate-measurement:v1"
    category: AggregateCategory
    denominator: StrictInt = Field(ge=0)
    numerator: StrictInt = Field(ge=0)
    metric_name: AggregateMetric


class MetadataInventoryPort(Protocol):
    """May return approved metadata. It must not expose a content resolver."""

    async def list_metadata(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        context: TrustedPersistenceContext,
    ) -> tuple[MetadataInventoryEntry, ...]: ...


class AdmissionAuthorityPort(Protocol):
    async def verify_current_authority(
        self,
        request: PurposeAdmissionRequest,
        *,
        access_policy: AccessPolicyVersion,
        context: TrustedPersistenceContext,
        evaluated_at: datetime,
    ) -> VerifiedAuthority: ...

    async def recheck(
        self, admitted: AdmittedSourceRevision, *, evaluated_at: datetime
    ) -> bool: ...


class AccessPolicyPort(Protocol):
    async def current_for(
        self, source: SourceObject, *, context: TrustedPersistenceContext
    ) -> AccessPolicyVersion: ...


class SourceRevisionIdentityPort(Protocol):
    async def exact_revision_number(
        self, source: SourceObject, revision: SourceRevision
    ) -> int | None: ...

    async def is_exact_revision_current(self, admitted: AdmittedSourceRevision) -> bool: ...


class AdmittedRevisionContentPort(Protocol):
    """Only this explicitly separate port can stream admitted source bytes."""

    def read_exact_revision(
        self, admitted: AdmittedSourceRevision
    ) -> AsyncIterator[bytes]: ...


class OriginalReferencePort(Protocol):
    async def bind_immutable_original(
        self, admitted: AdmittedSourceRevision, *, sha256: str, byte_length: int
    ) -> PrivateReference: ...


class ScopedOriginalReferencePort(Protocol):
    async def bind_immutable_original(
        self, admitted: AdmittedSourceRevision, *, sha256: str, byte_length: int,
        context: TrustedPersistenceContext,
    ) -> PrivateReference: ...


class DerivedStagingPort(Protocol):
    async def begin(self, admitted: AdmittedSourceRevision) -> PrivateReference: ...
    async def append(self, staging_ref: PrivateReference, chunk: bytes) -> None: ...
    async def finalize(self, staging_ref: PrivateReference, *, sha256: str,
                       byte_length: int) -> PrivateReference: ...
    async def discard(self, staging_ref: PrivateReference) -> None: ...


class ScopedDerivedStagingPort(Protocol):
    async def begin(
        self, admitted: AdmittedSourceRevision, *, context: TrustedPersistenceContext
    ) -> PrivateReference: ...

    async def append(
        self, staging_ref: PrivateReference, chunk: bytes, *,
        context: TrustedPersistenceContext,
    ) -> None: ...

    async def finalize(
        self, staging_ref: PrivateReference, *, sha256: str, byte_length: int,
        context: TrustedPersistenceContext,
    ) -> PrivateReference: ...

    async def discard(
        self, staging_ref: PrivateReference, *, context: TrustedPersistenceContext
    ) -> None: ...


class PrivateAnnotationStoragePort(Protocol):
    async def store_immutable_annotation(
        self, record_id: UUID, annotation_version: int, payload: bytes
    ) -> PrivateReference: ...


class ScopedPrivateAnnotationStoragePort(Protocol):
    async def store_immutable_annotation(
        self, admitted: AdmittedSourceRevision, record_id: UUID,
        annotation_version: int, payload: bytes, *,
        retention_profile_ref: PrivateReference,
        audit_receipt_ref: PrivateReference,
        context: TrustedPersistenceContext,
    ) -> PrivateReference: ...


class PrivateReportStoragePort(Protocol):
    """Restricted report store with an authorization fence at its commit point.

    The implementation must invoke ``authorize_commit`` after preparing the
    write and immediately before its atomic commit; a refusal must leave no
    report visible. Because a full PostgreSQL transaction can be retried after
    a translated serialization failure, the callback must be read-only and
    safe to invoke more than once. It must not consume a one-shot approval or
    perform an external side effect.
    """

    async def store_private_report(
        self,
        payload: bytes,
        *,
        retention_profile_ref: PrivateReference,
        audit_receipt_ref: PrivateReference,
        authorize_commit: Callable[[], Awaitable[None]],
    ) -> PrivateReportReceipt: ...


class ScopedPrivateReportStoragePort(Protocol):
    """Context-aware report port; callback is read-only and retry-safe.

    The callback runs inside the atomic commit transaction and may run again
    after a bounded full-transaction retry. External approval leases require a
    coordinated fence contract and cannot be acquired through this callback.
    """

    async def store_private_report(
        self,
        payload: bytes,
        *,
        report_id: UUID,
        idempotency_key: str,
        source_bindings: tuple[PrivateReportSourceBinding, ...],
        retention_profile_ref: PrivateReference,
        audit_receipt_ref: PrivateReference,
        context: TrustedPersistenceContext,
        authorize_commit: Callable[[], Awaitable[None]],
    ) -> PrivateReportReceipt: ...


class CleanupPort(Protocol):
    async def delete_private_reference(self, reference: PrivateReference) -> None: ...


class ScopedCleanupPort(Protocol):
    async def delete_private_reference(
        self, reference: PrivateReference, *, context: TrustedPersistenceContext
    ) -> None: ...


class RevocationFencePort(Protocol):
    async def fence(self, source_revision_id: UUID, *, reason: VersionIdentifier) -> None: ...


class ScopedRevocationFencePort(Protocol):
    async def fence(
        self, source_revision_id: UUID, *, reason: VersionIdentifier,
        context: TrustedPersistenceContext,
    ) -> None: ...


class RetentionExpirationPort(Protocol):
    async def expire(self, reference: PrivateReference, *, policy_ref: PrivateReference) -> None: ...


class ScopedRetentionExpirationPort(Protocol):
    async def expire(
        self, reference: PrivateReference, *, policy_ref: PrivateReference,
        context: TrustedPersistenceContext,
    ) -> None: ...


class AuditReceiptPort(Protocol):
    async def receipt(
        self,
        *,
        operation: VersionIdentifier,
        source_revision_id: UUID | None,
        outcome: VersionIdentifier,
    ) -> PrivateReference: ...


class ScopedAuditReceiptPort(Protocol):
    async def receipt(
        self,
        *,
        operation: VersionIdentifier,
        source_revision_id: UUID | None,
        outcome: VersionIdentifier,
        context: TrustedPersistenceContext,
    ) -> PrivateReference: ...


class ClockPort(Protocol):
    def now(self) -> datetime: ...


class PrivacyPolicyApprovalPort(Protocol):
    async def resolve_current_approved(
        self,
        policy_version: VersionIdentifier,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        context: TrustedPersistenceContext,
    ) -> ApprovedPrivacyPolicy | None: ...


class PrivateAggregatePublicationAuthorityPort(Protocol):
    """Resolves current privacy and release authority in the export transaction.

    A production implementation must use the canonical approval owners and
    their revocation fence. Fixture-only implementations belong in tests.
    """

    async def verify_current_publication(
        self,
        transaction: object,
        *,
        report_id: UUID,
        sanitized_report: SanitizedCorpusReport,
        context: TrustedPersistenceContext,
    ) -> bool: ...


class TrustedMetadataInventory:
    """Inventory service intentionally has no content or hashing dependency."""

    def __init__(
        self, metadata: MetadataInventoryPort, contexts: TrustedContextVerifier
    ) -> None:
        self._metadata = metadata
        self._contexts = contexts

    async def list_candidates(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        context: TrustedPersistenceContext,
    ) -> tuple[MetadataInventoryEntry, ...]:
        try:
            context.validate_shape()
            trusted_context = self._contexts.verify(context)
            trusted_context.validate_shape()
        except Exception as exc:
            raise CorpusAdmissionDenied("trusted metadata context is invalid") from exc
        if not isinstance(trusted_context.scope, EnvironmentScope) or (
            trusted_context.scope.workspace_id,
            trusted_context.scope.environment_id,
        ) != (workspace_id, environment_id):
            raise CorpusAdmissionDenied("metadata scope does not match trusted context")
        entries = await self._metadata.list_metadata(
            workspace_id=workspace_id,
            environment_id=environment_id,
            context=trusted_context,
        )
        for entry in entries:
            if (entry.source_object.workspace_id, entry.source_object.environment_id) != (
                workspace_id,
                environment_id,
            ):
                raise CorpusAdmissionDenied("metadata scope mismatch")
            if entry.source_revision.source_object_id != entry.source_object.source_object_id:
                raise CorpusAdmissionDenied("metadata source revision mismatch")
            if (entry.source_revision.workspace_id, entry.source_revision.environment_id) != (
                entry.source_object.workspace_id,
                entry.source_object.environment_id,
            ):
                raise CorpusAdmissionDenied("metadata revision scope mismatch")
        return entries


class TwoStageSourceAdmission:
    """Exact-revision admission followed by authorized streaming verification."""

    def __init__(
        self,
        *,
        policies: AccessPolicyPort,
        authority: AdmissionAuthorityPort,
        contexts: TrustedContextVerifier,
        audit: ScopedAuditReceiptPort,
        revisions: SourceRevisionIdentityPort,
        content: AdmittedRevisionContentPort,
        staging: ScopedDerivedStagingPort,
        originals: ScopedOriginalReferencePort,
        clock: ClockPort,
    ) -> None:
        self._policies = policies
        self._authority = authority
        self._contexts = contexts
        self._audit = audit
        self._revisions = revisions
        self._content = content
        self._staging = staging
        self._originals = originals
        self._clock = clock

    async def admit(
        self,
        request: PurposeAdmissionRequest,
        *,
        context: TrustedPersistenceContext,
    ) -> AdmittedSourceRevision:
        trusted_context = self._trusted_context(
            context,
            workspace_id=request.workspace_id,
            environment_id=request.environment_id,
            actor_id=request.authorized_actor_id,
        )
        source, revision = request.source_object, request.source_revision
        if source.source_object_id != revision.source_object_id:
            raise CorpusAdmissionDenied("source revision mismatch")
        if (request.workspace_id, request.environment_id) != (
            source.workspace_id,
            source.environment_id,
        ):
            raise CorpusAdmissionDenied("source scope mismatch")
        if (revision.workspace_id, revision.environment_id) != (
            source.workspace_id,
            source.environment_id,
        ):
            raise CorpusAdmissionDenied("source revision scope mismatch")
        if request.allowed_environment_id != request.environment_id:
            raise CorpusAdmissionDenied("environment is not allowlisted")
        if revision.kind.value != "content":
            raise CorpusAdmissionDenied("immutable content revision is required")
        source_revision_number = await self._revisions.exact_revision_number(source, revision)
        if source_revision_number is None or source_revision_number <= 0:
            raise CorpusAdmissionDenied("exact source revision is unavailable")
        admitted_at = self._now()
        policy = await self._policies.current_for(source, context=trusted_context)
        if (
            policy.source_object_id != source.source_object_id
            or policy.workspace_id != source.workspace_id
            or policy.environment_id != source.environment_id
        ):
            raise CorpusAdmissionDenied("current access policy scope mismatch")
        if policy.access_policy_version_id != request.access_policy_version:
            raise CorpusAdmissionDenied("requested access policy is not current")
        authority = await self._authority.verify_current_authority(
            request,
            access_policy=policy,
            context=trusted_context,
            evaluated_at=admitted_at,
        )
        if (
            authority.purpose != "asr_model_evaluation"
            or not authority.current
            or authority.revoked
            or not authority.human_read_granted
            or not authority.process_granted
            or not authority.legal_data_evidence_confirmed
            or not authority.purpose_approved
            or not authority.actor_authorized
            or not authority.retention_current
            or not authority.audit_permitted
            or not authority.environment_allowed
            or authority.expires_at <= admitted_at
        ):
            raise CorpusAdmissionDenied("required current authority is unavailable")
        audit_receipt = await self._audit.receipt(
            operation="private_asr_source_admission",
            source_revision_id=revision.revision_id,
            outcome="allowed",
            context=trusted_context,
        )
        return AdmittedSourceRevision(
            source_object=source,
            source_revision=revision,
            source_revision_number=source_revision_number,
            workspace_id=source.workspace_id,
            environment_id=source.environment_id,
            purpose_approval_ref=request.purpose_approval_ref,
            legal_data_evidence_ref=request.legal_data_evidence_ref,
            retention_profile_ref=request.retention_profile_ref,
            actor_id=request.authorized_actor_id,
            audit_receipt_ref=audit_receipt,
            access_policy_version=policy.access_policy_version_id,
            policy_version=request.policy_version,
            authority=authority,
            admitted_at=admitted_at,
        )

    async def verify_content(
        self,
        admitted: AdmittedSourceRevision,
        *,
        context: TrustedPersistenceContext,
        previously_verified_sha256: str | None = None,
    ) -> ContentVerificationResult:
        """Hash exact bytes only after admission; discard any partial staging."""

        trusted_context = self._trusted_context(
            context,
            workspace_id=admitted.workspace_id,
            environment_id=admitted.environment_id,
            actor_id=admitted.actor_id,
        )
        now = self._now()
        if now >= admitted.authority.expires_at:
            raise CorpusAdmissionDenied("admission expired before content read")
        if not await self._authority.recheck(admitted, evaluated_at=now):
            raise CorpusAdmissionDenied("admission was revoked before content read")
        if not await self._revisions.is_exact_revision_current(admitted):
            raise CorpusAdmissionDenied("admitted source revision is no longer exact")
        await self._require_current_policy(admitted, context=trusted_context)
        revision = admitted.source_revision
        if not await self._authority.recheck(admitted, evaluated_at=self._now()):
            raise CorpusAdmissionDenied("admission was revoked before staging")
        staging_ref = await self._staging.begin(admitted, context=trusted_context)
        digest = hashlib.sha256()
        byte_length = 0
        try:
            staged_at = self._now()
            if staged_at >= admitted.authority.expires_at:
                raise CorpusAdmissionDenied("admission expired before source stream")
            trusted_context = self._trusted_context(
                context,
                workspace_id=admitted.workspace_id,
                environment_id=admitted.environment_id,
                actor_id=admitted.actor_id,
            )
            if not await self._authority.recheck(admitted, evaluated_at=staged_at):
                raise CorpusAdmissionDenied("admission changed before source stream")
            if not await self._revisions.is_exact_revision_current(admitted):
                raise CorpusVerificationError("source revision changed before source stream")
            await self._require_current_policy(admitted, context=trusted_context)
            async for chunk in self._content.read_exact_revision(admitted):
                if not isinstance(chunk, bytes):
                    raise CorpusVerificationError("source stream returned invalid data")
                digest.update(chunk)
                byte_length += len(chunk)
                await self._staging.append(
                    staging_ref, chunk, context=trusted_context
                )
            actual_digest = digest.hexdigest()
            if byte_length != revision.byte_length:
                raise CorpusVerificationError("source revision byte length changed")
            if revision.original_sha256 != actual_digest:
                raise CorpusVerificationError("source revision digest mismatch")
            if previously_verified_sha256 is not None and previously_verified_sha256 != actual_digest:
                raise CorpusVerificationError("previously verified digest mismatch")
            verified_at = self._now()
            if verified_at >= admitted.authority.expires_at:
                raise CorpusAdmissionDenied("admission expired during content read")
            if not await self._authority.recheck(admitted, evaluated_at=verified_at):
                raise CorpusAdmissionDenied("admission changed during content read")
            if not await self._revisions.is_exact_revision_current(admitted):
                raise CorpusVerificationError("source revision changed during content read")
            trusted_context = self._trusted_context(
                context,
                workspace_id=admitted.workspace_id,
                environment_id=admitted.environment_id,
                actor_id=admitted.actor_id,
            )
            await self._require_current_policy(admitted, context=trusted_context)
            audit_receipt = await self._audit.receipt(
                operation="private_asr_content_verification",
                source_revision_id=revision.revision_id,
                outcome="verified",
                context=trusted_context,
            )
            staged_ref = await self._staging.finalize(
                staging_ref, sha256=actual_digest, byte_length=byte_length,
                context=trusted_context,
            )
            original_ref = await self._originals.bind_immutable_original(
                admitted, sha256=actual_digest, byte_length=byte_length,
                context=trusted_context,
            )
            return ContentVerificationResult(
                admitted=admitted,
                digest_verification=AudioDigestVerification(
                    audio_sha256=actual_digest,
                    source_revision_id=revision.revision_id,
                    byte_length=byte_length,
                    verified_at=verified_at,
                    audit_receipt_ref=audit_receipt,
                ),
                byte_length=byte_length,
                staged_ref=staged_ref,
                original_ref=original_ref,
            )
        except BaseException:
            await self._staging.discard(staging_ref, context=trusted_context)
            raise

    async def recheck_current(
        self,
        admitted: AdmittedSourceRevision,
        *,
        context: TrustedPersistenceContext,
    ) -> bool:
        """Fence a later operation with current actor, expiry, rights and revision."""

        now = self._now()
        if now >= admitted.authority.expires_at:
            return False
        try:
            trusted = self._trusted_context(
                context,
                workspace_id=admitted.workspace_id,
                environment_id=admitted.environment_id,
                actor_id=admitted.actor_id,
            )
        except CorpusAdmissionDenied:
            return False
        try:
            if not await self._authority.recheck(admitted, evaluated_at=now):
                return False
            if not await self._revisions.is_exact_revision_current(admitted):
                return False
            await self._require_current_policy(admitted, context=trusted)
        except CorpusAdmissionDenied:
            return False
        except Exception:
            # Cancellation remains a BaseException and propagates.
            raise CorpusRecheckUnavailable("current admission could not be rechecked") from None
        return True

    async def _require_current_policy(
        self, admitted: AdmittedSourceRevision, *, context: TrustedPersistenceContext
    ) -> None:
        current = await self._policies.current_for(admitted.source_object, context=context)
        if (
            current.source_object_id != admitted.source_object.source_object_id
            or current.workspace_id != admitted.workspace_id
            or current.environment_id != admitted.environment_id
            or current.access_policy_version_id != admitted.access_policy_version
        ):
            raise CorpusAdmissionDenied("access policy changed since admission")

    def _now(self) -> datetime:
        value = self._clock.now()
        if value.tzinfo is None or value.utcoffset() is None:
            raise CorpusAdmissionDenied("trusted clock returned a naive timestamp")
        return value

    def _trusted_context(
        self,
        context: TrustedPersistenceContext,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        actor_id: UUID,
    ) -> TrustedPersistenceContext:
        try:
            context.validate_shape()
            trusted = self._contexts.verify(context)
            trusted.validate_shape()
        except Exception as exc:
            raise CorpusAdmissionDenied("trusted persistence context is invalid") from exc
        if not isinstance(trusted.scope, EnvironmentScope) or (
            trusted.scope.workspace_id,
            trusted.scope.environment_id,
        ) != (workspace_id, environment_id):
            raise CorpusAdmissionDenied("trusted context scope does not match admission")
        contextual_actor = trusted.acting_subject_id or trusted.service_principal_id
        if contextual_actor != actor_id:
            raise CorpusAdmissionDenied("trusted context actor does not match admission")
        return trusted


class SanitizedReportBuilder:
    """Sanitize only under a currently resolved, scope-bound approved policy."""

    def __init__(
        self,
        *,
        approvals: PrivacyPolicyApprovalPort,
        contexts: TrustedContextVerifier,
        clock: ClockPort,
    ) -> None:
        self._approvals = approvals
        self._contexts = contexts
        self._clock = clock

    async def build(
        self,
        *,
        policy_version: VersionIdentifier,
        measurements: Iterable[AggregateMeasurement],
        context: TrustedPersistenceContext,
    ) -> SanitizedCorpusReport:
        try:
            context.validate_shape()
            trusted = self._contexts.verify(context)
            trusted.validate_shape()
        except Exception as exc:
            raise PrivacySanitizationError("trusted report context is invalid") from exc
        if not isinstance(trusted.scope, EnvironmentScope):
            raise PrivacySanitizationError("report requires workspace and environment scope")
        if (
            trusted.purpose.value != "asr_model_evaluation"
            or trusted.operation.value != "sanitize_private_asr_report"
        ):
            raise PrivacySanitizationError("trusted context purpose or operation is not allowed")
        approval = await self._approvals.resolve_current_approved(
            policy_version,
            workspace_id=trusted.scope.workspace_id,
            environment_id=trusted.scope.environment_id,
            context=trusted,
        )
        now = self._clock.now()
        if now.tzinfo is None or now.utcoffset() is None:
            raise PrivacySanitizationError("trusted report clock is invalid")
        if (
            approval is None
            or approval.revoked
            or approval.policy.policy_version != policy_version
            or approval.workspace_id != trusted.scope.workspace_id
            or approval.environment_id != trusted.scope.environment_id
            or approval.approved_at > now
            or approval.expires_at <= now
        ):
            raise PrivacySanitizationError("no current approved privacy policy")
        return _sanitize_aggregate_report(
            policy=approval.policy,
            approval=approval,
            approval_version=approval.approval_version,
            generated_at=now,
            measurements=measurements,
        )


def _sanitize_aggregate_report(
    *,
    policy: PrivacyPolicy,
    approval: ApprovedPrivacyPolicy,
    approval_version: VersionIdentifier,
    generated_at: datetime,
    measurements: Iterable[AggregateMeasurement],
) -> SanitizedCorpusReport:
    """Build a strict allowlisted aggregate without accepting record-level data."""

    aggregates: list[SanitizedAggregate] = []
    metric_names: set[AggregateMetric] = set()
    for item in measurements:
        if item.category not in policy.approved_categories:
            raise PrivacySanitizationError("unapproved aggregate category")
        if item.metric_name not in policy.approved_metric_names:
            raise PrivacySanitizationError("unapproved aggregate metric")
        if item.numerator > item.denominator:
            raise PrivacySanitizationError("aggregate numerator exceeds denominator")
        if item.denominator < policy.minimum_cell_size:
            raise PrivacySanitizationError("aggregate cell is below privacy threshold")
        metric_names.add(item.metric_name)
        rate = (
            item.numerator * 1_000_000 // item.denominator
            if policy.allow_rates and item.denominator
            else None
        )
        aggregates.append(
            SanitizedAggregate(
                category=item.category,
                metric_name=item.metric_name,
                denominator=item.denominator,
                numerator=item.numerator,
                rate_millionths=rate,
            )
        )
    if not aggregates:
        raise PrivacySanitizationError("empty aggregate is not a verified report")
    return SanitizedCorpusReport(
        schema_version="r2-private-asr-sanitized-report:v1",
        privacy_policy_version=policy.policy_version,
        privacy_policy_approval_version=approval_version,
        generated_at=generated_at,
        privacy_policy_approval=approval,
            workspace_id=approval.workspace_id,
            environment_id=approval.environment_id,
        privacy_policy=policy,
        status="NOT_VERIFIED",
        sufficiency=QualificationSufficiency.NOT_VERIFIED,
        aggregates=tuple(aggregates),
        metric_names=tuple(sorted(metric_names, key=lambda item: item.value)),
    )


__all__ = [
    "AccessPolicyPort", "AdmittedRevisionContentPort", "AdmittedSourceRevision",
    "AdmissionAuthorityPort", "AggregateMeasurement", "AuditReceiptPort",
    "CleanupPort", "ClockPort", "ContentVerificationResult", "CorpusAdmissionDenied",
    "CorpusVerificationError", "DerivedStagingPort", "MetadataInventoryEntry",
    "MetadataInventoryPort", "OriginalReferencePort", "PrivacyPolicyApprovalPort",
    "PrivateAggregatePublicationAuthorityPort", "PrivateArtifactDeletionRequest",
    "PrivateArtifactDeletionRequestV2", "PrivateArtifactDeletionSourceBinding",
    "PrivateArtifactDeletionFenceOutcome",
    "PrivateArtifactDeletionFenceContributorOutcome",
    "PrivateArtifactDeletionAuthorization", "PrivateArtifactDeletionAuthority",
    "PrivateArtifactDeletionFencePort", "PrivateArtifactDeletionFenceContributorPort",
    "PrivateArtifactDeletionFenceResolver",
    "PrivacySanitizationError",
    "PrivateAnnotationStoragePort", "PrivateReportReceipt", "PrivateReportSourceBinding", "PrivateReportStoragePort",
    "PurposeAdmissionRequest", "RetentionExpirationPort", "RevocationFencePort",
    "ScopedAuditReceiptPort", "ScopedCleanupPort", "ScopedDerivedStagingPort",
    "ScopedOriginalReferencePort", "ScopedPrivateAnnotationStoragePort",
    "ScopedPrivateReportStoragePort", "ScopedRetentionExpirationPort", "ScopedRevocationFencePort",
    "SanitizedReportBuilder", "SourceRevisionIdentityPort", "TrustedMetadataInventory",
    "TwoStageSourceAdmission", "VerifiedAuthority",
]
