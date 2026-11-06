"""Offline, fail-closed evidence records for external commit-fence qualification.

These records describe evidence; they are not an authority registry, a provider
adapter, or a production composition input. This offline module cannot issue F2.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import Enum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from spine.application.commit_fence_provider_port import (
    CommitFenceFailureReason,
    CommitFenceOperationState,
)
from spine.domain.common import DefinitionModel

QualificationVersion = Annotated[
    StrictStr,
    StringConstraints(
        min_length=1,
        max_length=32,
        pattern=r"^[vV]?[0-9]{1,4}(?:\.[0-9]{1,4}){0,3}(?:-[A-Za-z][A-Za-z0-9]{0,15})?$",
    ),
]
QualificationScopeCode = Annotated[
    StrictStr,
    StringConstraints(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_.:-]{0,63}$",
    ),
]


class FenceDiagnosticMode(str, Enum):
    POSTGRESQL_F1 = "postgresql_f1"
    EXTERNAL_F2 = "external_f2"
    SYNTHETIC_TEST = "synthetic_test"
    UNKNOWN_F4 = "unknown_f4"


class FenceDiagnosticOutcome(str, Enum):
    ACQUIRED = CommitFenceOperationState.ACQUIRED.value
    DENIED = CommitFenceOperationState.DENIED.value
    STALE = CommitFenceOperationState.STALE_HOLDER.value
    EXPIRED = CommitFenceOperationState.EXPIRED.value
    AMBIGUOUS = CommitFenceOperationState.AMBIGUOUS.value
    RELEASED = CommitFenceOperationState.RELEASED.value
    INDETERMINATE = CommitFenceOperationState.INDETERMINATE.value
    UNSUPPORTED = CommitFenceOperationState.UNSUPPORTED.value
    F4_DENIED = "F4_DENIED"


class FenceDiagnosticReason(str, Enum):
    PROVIDER_NOT_VERIFIED = "provider_not_verified"
    CONTRACT_NOT_VERIFIED = "contract_not_verified"
    ENVIRONMENT_NOT_VERIFIED = "environment_not_verified"
    EVIDENCE_NOT_VERIFIED = "evidence_not_verified"
    INCOMPLETE_AUTHORITY_BINDING = "incomplete_authority_binding"
    EVIDENCE_EXPIRED = "evidence_expired"
    PROVIDER_RESULT_AMBIGUOUS = "provider_result_ambiguous"
    PROVIDER_UNAVAILABLE = CommitFenceFailureReason.PROVIDER_UNAVAILABLE.value
    CLOCK_UNCERTAIN = CommitFenceFailureReason.CLOCK_UNCERTAIN.value
    REVOCATION_ACKNOWLEDGEMENT_PENDING = (
        CommitFenceFailureReason.REVOCATION_ACKNOWLEDGEMENT_PENDING.value
    )
    SYNTHETIC_ONLY_EVIDENCE = "synthetic_only_evidence"
    EVIDENCE_UNSUPPORTED = "evidence_unsupported"
    UNSUPPORTED_SCHEMA_VERSION = "unsupported_schema_version"
    UNKNOWN_AUTHORITY_CLASS = "unknown_authority_class"
    OFFLINE_RECORD_NOT_AUTHORITY = "offline_record_not_authority"


class CommitFenceDiagnostic(DefinitionModel):
    """Closed-field, content-free operator observation for one fence operation."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["commit-fence-diagnostic:v1"] = "commit-fence-diagnostic:v1"
    operation_id: UUID
    authority_reference: UUID | None
    environment_reference: UUID | None
    fence_mode: FenceDiagnosticMode
    outcome_code: FenceDiagnosticOutcome
    reason_code: FenceDiagnosticReason | None
    evidence_reference: UUID | None
    diagnostic_timestamp: datetime

    @field_validator(
        "operation_id", "authority_reference", "environment_reference", "evidence_reference"
    )
    @classmethod
    def require_opaque_nonzero_references(cls, value: UUID | None) -> UUID | None:
        if value is not None and value.int == 0:
            raise ValueError("diagnostic references must be non-zero opaque identifiers")
        return value

    @field_validator("diagnostic_timestamp")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("diagnostic timestamp must be UTC")
        return value

    @model_validator(mode="after")
    def require_safe_reason_for_denial(self) -> CommitFenceDiagnostic:
        if self.outcome_code in {
            FenceDiagnosticOutcome.DENIED,
            FenceDiagnosticOutcome.EXPIRED,
            FenceDiagnosticOutcome.AMBIGUOUS,
            FenceDiagnosticOutcome.INDETERMINATE,
            FenceDiagnosticOutcome.UNSUPPORTED,
            FenceDiagnosticOutcome.F4_DENIED,
        } and self.reason_code is None:
            raise ValueError("denied fence diagnostic requires a safe reason code")
        return self

    def to_operator_payload(self) -> dict[str, str | None]:
        """Return only the explicit scalar allowlist used by operator sinks."""

        if type(self) is not CommitFenceDiagnostic:
            raise TypeError("operator diagnostics must use the exact versioned schema")
        return {
            "schema_version": self.schema_version,
            "operation_id": str(self.operation_id),
            "authority_reference": (
                None if self.authority_reference is None else str(self.authority_reference)
            ),
            "environment_reference": (
                None if self.environment_reference is None else str(self.environment_reference)
            ),
            "fence_mode": self.fence_mode.value,
            "outcome_code": self.outcome_code.value,
            "reason_code": None if self.reason_code is None else self.reason_code.value,
            "evidence_reference": (
                None if self.evidence_reference is None else str(self.evidence_reference)
            ),
            "diagnostic_timestamp": self.diagnostic_timestamp.isoformat(),
        }


class QualificationEvidenceStatus(str, Enum):
    NOT_PROVIDED = "NOT_PROVIDED"
    NOT_VERIFIED = "NOT_VERIFIED"
    VERIFIED = "VERIFIED"
    EXPIRED = "EXPIRED"
    AMBIGUOUS = "AMBIGUOUS"
    SYNTHETIC_ONLY = "SYNTHETIC_ONLY"
    UNSUPPORTED = "UNSUPPORTED"


class QualificationAuthorityClass(str, Enum):
    EXTERNAL_F2 = "EXTERNAL_F2"
    POSTGRESQL_F1 = "POSTGRESQL_F1"
    SYNTHETIC_TEST = "SYNTHETIC_TEST"
    UNKNOWN = "UNKNOWN"


class QualificationReviewerRole(str, Enum):
    PROVIDER_OWNER = "PROVIDER_OWNER"
    SECURITY_OWNER = "SECURITY_OWNER"
    OPERATIONS_OWNER = "OPERATIONS_OWNER"
    APPLICATION_INTEGRATION_OWNER = "APPLICATION_INTEGRATION_OWNER"
    DECISION_OWNER = "DECISION_OWNER"


class QualificationReviewerDecision(str, Enum):
    NOT_PROVIDED = "NOT_PROVIDED"
    NOT_VERIFIED = "NOT_VERIFIED"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"


class QualificationProgressStatus(str, Enum):
    OFFLINE_SCHEMA_VERIFIED = "OFFLINE_SCHEMA_VERIFIED"
    PROVIDER_NOT_VERIFIED = "PROVIDER_NOT_VERIFIED"
    ENVIRONMENT_NOT_VERIFIED = "ENVIRONMENT_NOT_VERIFIED"
    F2_NOT_QUALIFIED = "F2_NOT_QUALIFIED"
    F4_DENIED = "F4_DENIED"


class QualificationEvidence(DefinitionModel):
    """One bounded evidence pointer; no provider payload or free-text field."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    status: QualificationEvidenceStatus = QualificationEvidenceStatus.NOT_PROVIDED
    evidence_reference: UUID | None = None
    valid_until: datetime | None = None

    @model_validator(mode="after")
    def validate_evidence_pointer(self) -> QualificationEvidence:
        if self.status is QualificationEvidenceStatus.NOT_PROVIDED:
            if self.evidence_reference is not None or self.valid_until is not None:
                raise ValueError("not-provided evidence cannot carry references or expiry")
        elif self.status is QualificationEvidenceStatus.VERIFIED:
            if self.evidence_reference is None:
                raise ValueError("verified evidence requires an opaque evidence reference")
        if self.evidence_reference is not None and self.evidence_reference.int == 0:
            raise ValueError("evidence reference must be non-zero")
        if self.valid_until is not None and (
            self.valid_until.tzinfo is None or self.valid_until.utcoffset() != timedelta(0)
        ):
            raise ValueError("evidence expiry must be a UTC timestamp")
        return self


class QualificationAuthorityBinding(DefinitionModel):
    """Exact authority binding references without exposing identity metadata."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    status: QualificationEvidenceStatus = QualificationEvidenceStatus.NOT_PROVIDED
    issuer_reference: UUID | None = None
    subject_reference: UUID | None = None
    workspace_reference: UUID | None = None
    environment_reference: UUID | None = None
    purpose: QualificationScopeCode | None = None
    action: QualificationScopeCode | None = None
    evidence_reference: UUID | None = None

    @model_validator(mode="after")
    def validate_exact_binding(self) -> QualificationAuthorityBinding:
        values = (
            self.issuer_reference,
            self.subject_reference,
            self.workspace_reference,
            self.environment_reference,
            self.purpose,
            self.action,
        )
        if self.status is QualificationEvidenceStatus.VERIFIED:
            if any(value is None for value in values) or self.evidence_reference is None:
                raise ValueError("verified authority binding requires every exact scope field")
        elif self.status is QualificationEvidenceStatus.NOT_PROVIDED:
            if any(value is not None for value in values) or self.evidence_reference is not None:
                raise ValueError("not-provided binding cannot contain partial scope values")
        if self.evidence_reference is not None and self.evidence_reference.int == 0:
            raise ValueError("binding evidence reference must be non-zero")
        for value in (
            self.issuer_reference,
            self.subject_reference,
            self.workspace_reference,
            self.environment_reference,
        ):
            if value is not None and value.int == 0:
                raise ValueError("authority binding references must be non-zero")
        return self


class QualificationParameters(DefinitionModel):
    """Provider/environment-specific numeric facts; all are absent by default."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    status: QualificationEvidenceStatus = QualificationEvidenceStatus.NOT_PROVIDED
    permit_lifetime_ms: StrictInt | None = Field(default=None, ge=1)
    maximum_transaction_ms: StrictInt | None = Field(default=None, ge=1)
    renewal_supported: bool | None = None
    renewal_interval_ms: StrictInt | None = Field(default=None, ge=1)
    clock_uncertainty_ms: StrictInt | None = Field(default=None, ge=0)
    safety_margin_ms: StrictInt | None = Field(default=None, ge=0)
    request_timeout_ms: StrictInt | None = Field(default=None, ge=1)
    retry_limit: StrictInt | None = Field(default=None, ge=0)
    evidence_reference: UUID | None = None

    @model_validator(mode="after")
    def validate_parameter_evidence(self) -> QualificationParameters:
        values = (
            self.permit_lifetime_ms,
            self.maximum_transaction_ms,
            self.clock_uncertainty_ms,
            self.safety_margin_ms,
            self.request_timeout_ms,
            self.retry_limit,
        )
        if self.status is QualificationEvidenceStatus.NOT_PROVIDED:
            if (
                any(value is not None for value in values)
                or self.renewal_supported is not None
                or self.renewal_interval_ms is not None
                or self.evidence_reference is not None
            ):
                raise ValueError("not-provided parameters cannot contain values")
        elif self.status is QualificationEvidenceStatus.VERIFIED:
            if any(value is None for value in values) or self.evidence_reference is None:
                raise ValueError("verified parameters require every value and evidence reference")
            if self.renewal_supported is None:
                raise ValueError("verified parameters must state whether renewal is supported")
            if (self.renewal_supported and self.renewal_interval_ms is None) or (
                not self.renewal_supported and self.renewal_interval_ms is not None
            ):
                raise ValueError("renewal interval must match the documented renewal capability")
        if self.evidence_reference is not None and self.evidence_reference.int == 0:
            raise ValueError("parameter evidence reference must be non-zero")
        return self


class QualificationReviewerDecisionRecord(DefinitionModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    role: QualificationReviewerRole
    decision: QualificationReviewerDecision = QualificationReviewerDecision.NOT_PROVIDED
    reviewer_reference: UUID | None = None
    decided_at: datetime | None = None
    evidence_reference: UUID | None = None

    @model_validator(mode="after")
    def validate_decision_evidence(self) -> QualificationReviewerDecisionRecord:
        if self.decision is QualificationReviewerDecision.NOT_PROVIDED:
            if any(
                value is not None
                for value in (self.reviewer_reference, self.decided_at, self.evidence_reference)
            ):
                raise ValueError("not-provided decision cannot contain reviewer evidence")
        elif self.decision is QualificationReviewerDecision.ACCEPTED:
            if any(
                value is None
                for value in (self.reviewer_reference, self.decided_at, self.evidence_reference)
            ):
                raise ValueError("accepted decision requires attributed evidence")
        if self.reviewer_reference is not None and self.reviewer_reference.int == 0:
            raise ValueError("reviewer reference must be non-zero")
        if self.evidence_reference is not None and self.evidence_reference.int == 0:
            raise ValueError("review evidence reference must be non-zero")
        if self.decided_at is not None and (
            self.decided_at.tzinfo is None or self.decided_at.utcoffset() != timedelta(0)
        ):
            raise ValueError("review decision timestamp must be UTC")
        return self


class UnsupportedQualificationScope(DefinitionModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    scope_reference: UUID
    reason_code: FenceDiagnosticReason

    @field_validator("scope_reference")
    @classmethod
    def require_nonzero_scope_reference(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("unsupported scope reference must be non-zero")
        return value


class UnsupportedQualificationScopes(DefinitionModel):
    """Evidence-backed list of scopes that remain F4 after a future decision."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    status: QualificationEvidenceStatus = QualificationEvidenceStatus.NOT_PROVIDED
    scopes: tuple[UnsupportedQualificationScope, ...] = Field(default=(), max_length=128)
    evidence_reference: UUID | None = None
    valid_until: datetime | None = None

    @model_validator(mode="after")
    def validate_unsupported_scopes(self) -> UnsupportedQualificationScopes:
        if self.status is QualificationEvidenceStatus.NOT_PROVIDED:
            if self.scopes or self.evidence_reference is not None or self.valid_until is not None:
                raise ValueError("not-provided unsupported scopes cannot imply an empty allowlist")
        elif self.status is QualificationEvidenceStatus.VERIFIED:
            if not self.scopes or self.evidence_reference is None:
                raise ValueError("verified unsupported scopes require entries and evidence")
        if self.evidence_reference is not None and self.evidence_reference.int == 0:
            raise ValueError("unsupported-scope evidence reference must be non-zero")
        if self.valid_until is not None and (
            self.valid_until.tzinfo is None or self.valid_until.utcoffset() != timedelta(0)
        ):
            raise ValueError("unsupported-scope evidence expiry must be UTC")
        return self


def _unprovided_reviewer_decisions() -> tuple[QualificationReviewerDecisionRecord, ...]:
    return tuple(
        QualificationReviewerDecisionRecord(role=role)
        for role in QualificationReviewerRole
    )


class ExternalFenceQualificationRecord(DefinitionModel):
    """Offline evidence template. It deliberately has no F2-qualified state."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["external-commit-fence-qualification:v1"] = (
        "external-commit-fence-qualification:v1"
    )
    record_reference: UUID
    authority_class: QualificationAuthorityClass = QualificationAuthorityClass.UNKNOWN
    qualification_status: Literal["F2_NOT_QUALIFIED"] = "F2_NOT_QUALIFIED"
    provider_reference: UUID | None = None
    provider_identity: QualificationEvidence = Field(default_factory=QualificationEvidence)
    provider_contract_version: QualificationVersion | None = None
    provider_contract: QualificationEvidence = Field(default_factory=QualificationEvidence)
    environment_reference: UUID | None = None
    environment: QualificationEvidence = Field(default_factory=QualificationEvidence)
    authority_binding: QualificationAuthorityBinding = Field(
        default_factory=QualificationAuthorityBinding
    )
    permit_acquisition_release: QualificationEvidence = Field(
        default_factory=QualificationEvidence
    )
    revocation_acknowledgement_order: QualificationEvidence = Field(
        default_factory=QualificationEvidence
    )
    expiry_renewal_guarantees: QualificationEvidence = Field(
        default_factory=QualificationEvidence
    )
    stale_holder_generation_semantics: QualificationEvidence = Field(
        default_factory=QualificationEvidence
    )
    clock_uncertainty: QualificationEvidence = Field(default_factory=QualificationEvidence)
    transaction_commit_bounds: QualificationEvidence = Field(
        default_factory=QualificationEvidence
    )
    timeout_retry_recovery: QualificationEvidence = Field(
        default_factory=QualificationEvidence
    )
    provider_conformance_results: QualificationEvidence = Field(
        default_factory=QualificationEvidence
    )
    parameters: QualificationParameters = Field(default_factory=QualificationParameters)
    adapter_version: QualificationVersion | None = None
    composition_version: QualificationVersion | None = None
    adapter_composition_evidence: QualificationEvidence = Field(
        default_factory=QualificationEvidence
    )
    reviewer_decisions: tuple[QualificationReviewerDecisionRecord, ...] = Field(
        default_factory=_unprovided_reviewer_decisions,
        max_length=len(QualificationReviewerRole),
    )
    remaining_unsupported_scopes: UnsupportedQualificationScopes = Field(
        default_factory=UnsupportedQualificationScopes
    )

    @field_validator("record_reference")
    @classmethod
    def require_nonzero_record_reference(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("qualification record reference must be non-zero")
        return value

    @model_validator(mode="after")
    def validate_qualified_identity_references(self) -> ExternalFenceQualificationRecord:
        if self.provider_identity.status is QualificationEvidenceStatus.VERIFIED:
            if self.provider_reference is None:
                raise ValueError("verified provider identity requires an opaque provider reference")
        elif (
            self.provider_identity.status is QualificationEvidenceStatus.NOT_PROVIDED
            and self.provider_reference is not None
        ):
            raise ValueError("not-provided provider identity cannot carry a provider reference")
        if self.provider_contract.status is QualificationEvidenceStatus.VERIFIED:
            if self.provider_contract_version is None:
                raise ValueError("verified provider contract requires its exact version")
        elif (
            self.provider_contract.status is QualificationEvidenceStatus.NOT_PROVIDED
            and self.provider_contract_version is not None
        ):
            raise ValueError("not-provided provider contract cannot carry a contract version")
        if self.environment.status is QualificationEvidenceStatus.VERIFIED:
            if self.environment_reference is None:
                raise ValueError("verified environment requires an opaque environment reference")
        elif (
            self.environment.status is QualificationEvidenceStatus.NOT_PROVIDED
            and self.environment_reference is not None
        ):
            raise ValueError("not-provided environment cannot carry an environment reference")
        for value in (self.provider_reference, self.environment_reference):
            if value is not None and value.int == 0:
                raise ValueError("provider and environment references must be non-zero")
        reviewer_roles = tuple(item.role for item in self.reviewer_decisions)
        if len(reviewer_roles) != len(set(reviewer_roles)):
            raise ValueError("qualification record cannot repeat reviewer roles")
        return self


class F2QualificationAssessment(DefinitionModel):
    """Content-free offline readiness result; this model never grants F2."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["external-commit-fence-qualification-assessment:v1"] = (
        "external-commit-fence-qualification-assessment:v1"
    )
    qualification_status: QualificationProgressStatus
    outcome_code: FenceDiagnosticOutcome
    reason_code: FenceDiagnosticReason
    evidence_reference: UUID | None = None


def build_offline_qualification_template(*, record_reference: UUID) -> ExternalFenceQualificationRecord:
    """Build a typed, fully NOT_PROVIDED template without time or provider defaults."""

    return ExternalFenceQualificationRecord(record_reference=record_reference)


_REQUIRED_EVIDENCE_FIELDS = (
    "provider_identity",
    "provider_contract",
    "environment",
    "permit_acquisition_release",
    "revocation_acknowledgement_order",
    "expiry_renewal_guarantees",
    "stale_holder_generation_semantics",
    "clock_uncertainty",
    "transaction_commit_bounds",
    "timeout_retry_recovery",
    "provider_conformance_results",
    "adapter_composition_evidence",
    "remaining_unsupported_scopes",
)


def _assessment(
    status: QualificationProgressStatus,
    reason: FenceDiagnosticReason,
    evidence_reference: UUID | None = None,
) -> F2QualificationAssessment:
    return F2QualificationAssessment(
        qualification_status=status,
        outcome_code=FenceDiagnosticOutcome.F4_DENIED,
        reason_code=reason,
        evidence_reference=evidence_reference,
    )


def evaluate_f2_qualification(
    record: ExternalFenceQualificationRecord, *, evaluated_at: datetime
) -> F2QualificationAssessment:
    """Classify evidence only; this evaluator always denies production F2 use."""

    if type(record) is not ExternalFenceQualificationRecord:
        return _assessment(
            QualificationProgressStatus.F4_DENIED,
            FenceDiagnosticReason.UNKNOWN_AUTHORITY_CLASS,
        )
    if evaluated_at.tzinfo is None or evaluated_at.utcoffset() != timedelta(0):
        raise ValueError("qualification evaluation timestamp must be UTC")
    if record.authority_class is QualificationAuthorityClass.SYNTHETIC_TEST:
        return _assessment(
            QualificationProgressStatus.F4_DENIED,
            FenceDiagnosticReason.SYNTHETIC_ONLY_EVIDENCE,
        )
    if record.authority_class is not QualificationAuthorityClass.EXTERNAL_F2:
        return _assessment(
            QualificationProgressStatus.F4_DENIED,
            FenceDiagnosticReason.UNKNOWN_AUTHORITY_CLASS,
        )

    provider = record.provider_identity
    if provider.status is QualificationEvidenceStatus.SYNTHETIC_ONLY:
        return _assessment(
            QualificationProgressStatus.F4_DENIED,
            FenceDiagnosticReason.SYNTHETIC_ONLY_EVIDENCE,
            provider.evidence_reference,
        )
    if provider.status is not QualificationEvidenceStatus.VERIFIED:
        return _assessment(
            QualificationProgressStatus.PROVIDER_NOT_VERIFIED,
            _reason_for_evidence(provider.status),
            provider.evidence_reference,
        )
    if provider.valid_until is not None and evaluated_at >= provider.valid_until:
        return _assessment(
            QualificationProgressStatus.F4_DENIED,
            FenceDiagnosticReason.EVIDENCE_EXPIRED,
            provider.evidence_reference,
        )

    if (
        record.provider_contract_version is None
        or record.provider_contract.status is not QualificationEvidenceStatus.VERIFIED
    ):
        return _assessment(
            QualificationProgressStatus.PROVIDER_NOT_VERIFIED,
            FenceDiagnosticReason.CONTRACT_NOT_VERIFIED,
            record.provider_contract.evidence_reference,
        )
    if record.provider_contract.valid_until is not None and evaluated_at >= record.provider_contract.valid_until:
        return _assessment(
            QualificationProgressStatus.F4_DENIED,
            FenceDiagnosticReason.EVIDENCE_EXPIRED,
            record.provider_contract.evidence_reference,
        )

    if record.environment.status is not QualificationEvidenceStatus.VERIFIED:
        return _assessment(
            QualificationProgressStatus.ENVIRONMENT_NOT_VERIFIED,
            FenceDiagnosticReason.ENVIRONMENT_NOT_VERIFIED,
            record.environment.evidence_reference,
        )
    if record.environment.valid_until is not None and evaluated_at >= record.environment.valid_until:
        return _assessment(
            QualificationProgressStatus.F4_DENIED,
            FenceDiagnosticReason.EVIDENCE_EXPIRED,
            record.environment.evidence_reference,
        )

    binding = record.authority_binding
    if binding.status is not QualificationEvidenceStatus.VERIFIED:
        return _assessment(
            QualificationProgressStatus.F4_DENIED,
            FenceDiagnosticReason.INCOMPLETE_AUTHORITY_BINDING,
            binding.evidence_reference,
        )

    evidence = [getattr(record, field) for field in _REQUIRED_EVIDENCE_FIELDS]
    evidence.append(binding)
    evidence.append(record.parameters)
    for item in evidence:
        status = item.status
        reference = item.evidence_reference
        if status is QualificationEvidenceStatus.SYNTHETIC_ONLY:
            return _assessment(
                QualificationProgressStatus.F4_DENIED,
                FenceDiagnosticReason.SYNTHETIC_ONLY_EVIDENCE,
                reference,
            )
        valid_until = getattr(item, "valid_until", None)
        if status is QualificationEvidenceStatus.EXPIRED or (
            valid_until is not None and evaluated_at >= valid_until
        ):
            return _assessment(
                QualificationProgressStatus.F4_DENIED,
                FenceDiagnosticReason.EVIDENCE_EXPIRED,
                reference,
            )
        if status is QualificationEvidenceStatus.AMBIGUOUS:
            return _assessment(
                QualificationProgressStatus.F4_DENIED,
                FenceDiagnosticReason.PROVIDER_RESULT_AMBIGUOUS,
                reference,
            )
        if status is QualificationEvidenceStatus.UNSUPPORTED:
            return _assessment(
                QualificationProgressStatus.F4_DENIED,
                FenceDiagnosticReason.EVIDENCE_UNSUPPORTED,
                reference,
            )
        if status is not QualificationEvidenceStatus.VERIFIED:
            return _assessment(
                QualificationProgressStatus.F4_DENIED,
                FenceDiagnosticReason.EVIDENCE_NOT_VERIFIED,
                reference,
            )

    if not record.provider_conformance_results.evidence_reference:
        return _assessment(
            QualificationProgressStatus.F4_DENIED,
            FenceDiagnosticReason.EVIDENCE_NOT_VERIFIED,
        )
    required_roles = frozenset(QualificationReviewerRole)
    reviewer_decisions = {item.role: item.decision for item in record.reviewer_decisions}
    if any(
        reviewer_decisions.get(role) is not QualificationReviewerDecision.ACCEPTED
        for role in required_roles
    ):
        return _assessment(
            QualificationProgressStatus.F4_DENIED,
            FenceDiagnosticReason.EVIDENCE_NOT_VERIFIED,
        )
    return _assessment(
        QualificationProgressStatus.F2_NOT_QUALIFIED,
        FenceDiagnosticReason.OFFLINE_RECORD_NOT_AUTHORITY,
    )


def _reason_for_evidence(status: QualificationEvidenceStatus) -> FenceDiagnosticReason:
    return {
        QualificationEvidenceStatus.NOT_PROVIDED: FenceDiagnosticReason.PROVIDER_NOT_VERIFIED,
        QualificationEvidenceStatus.NOT_VERIFIED: FenceDiagnosticReason.PROVIDER_NOT_VERIFIED,
        QualificationEvidenceStatus.VERIFIED: FenceDiagnosticReason.EVIDENCE_NOT_VERIFIED,
        QualificationEvidenceStatus.EXPIRED: FenceDiagnosticReason.EVIDENCE_EXPIRED,
        QualificationEvidenceStatus.AMBIGUOUS: FenceDiagnosticReason.PROVIDER_RESULT_AMBIGUOUS,
        QualificationEvidenceStatus.SYNTHETIC_ONLY: FenceDiagnosticReason.SYNTHETIC_ONLY_EVIDENCE,
        QualificationEvidenceStatus.UNSUPPORTED: FenceDiagnosticReason.EVIDENCE_UNSUPPORTED,
    }[status]


def parse_f2_qualification(
    payload: object, *, evaluated_at: datetime
) -> F2QualificationAssessment:
    """Validate an offline record and collapse all invalid inputs to safe F4."""

    if type(payload) is not dict:
        return _assessment(
            QualificationProgressStatus.F4_DENIED,
            FenceDiagnosticReason.UNKNOWN_AUTHORITY_CLASS,
        )
    try:
        record = ExternalFenceQualificationRecord.model_validate(payload, strict=False)
    except ValidationError:
        schema_version = payload.get("schema_version")
        reason = (
            FenceDiagnosticReason.UNSUPPORTED_SCHEMA_VERSION
            if schema_version != "external-commit-fence-qualification:v1"
            else FenceDiagnosticReason.EVIDENCE_NOT_VERIFIED
        )
        return _assessment(QualificationProgressStatus.F4_DENIED, reason)
    return evaluate_f2_qualification(record, evaluated_at=evaluated_at)


__all__ = [
    "CommitFenceDiagnostic",
    "F2QualificationAssessment",
    "FenceDiagnosticMode",
    "FenceDiagnosticOutcome",
    "FenceDiagnosticReason",
    "ExternalFenceQualificationRecord",
    "QualificationAuthorityBinding",
    "QualificationAuthorityClass",
    "QualificationEvidence",
    "QualificationEvidenceStatus",
    "QualificationParameters",
    "QualificationProgressStatus",
    "QualificationReviewerDecision",
    "QualificationReviewerDecisionRecord",
    "QualificationReviewerRole",
    "QualificationScopeCode",
    "QualificationVersion",
    "UnsupportedQualificationScope",
    "UnsupportedQualificationScopes",
    "build_offline_qualification_template",
    "evaluate_f2_qualification",
    "parse_f2_qualification",
]
