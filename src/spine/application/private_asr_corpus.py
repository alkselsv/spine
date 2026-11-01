"""Offline private ASR corpus admission, storage ports and report boundary."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from datetime import datetime
import hashlib
from typing import Literal, Protocol
from uuid import UUID

from pydantic import ConfigDict, Field, StrictInt, model_validator

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
    PrivateReference,
    PrivacyPolicy,
    QualificationSufficiency,
    SanitizedAggregate,
    SanitizedCorpusReport,
)
from spine.domain.sources.access_policy import AccessPolicyVersion
from spine.domain.sources.models import SourceObject, SourceRevision


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
    source_revision_id: UUID


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
    report visible.
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
    """Context-aware report port; callback runs inside atomic commit transaction."""

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
    "MetadataInventoryPort", "OriginalReferencePort", "PrivacyPolicyApprovalPort", "PrivacySanitizationError",
    "PrivateAnnotationStoragePort", "PrivateReportReceipt", "PrivateReportSourceBinding", "PrivateReportStoragePort",
    "PurposeAdmissionRequest", "RetentionExpirationPort", "RevocationFencePort",
    "ScopedAuditReceiptPort", "ScopedCleanupPort", "ScopedDerivedStagingPort",
    "ScopedOriginalReferencePort", "ScopedPrivateAnnotationStoragePort",
    "ScopedPrivateReportStoragePort", "ScopedRetentionExpirationPort", "ScopedRevocationFencePort",
    "SanitizedReportBuilder", "SourceRevisionIdentityPort", "TrustedMetadataInventory",
    "TwoStageSourceAdmission", "VerifiedAuthority",
]
