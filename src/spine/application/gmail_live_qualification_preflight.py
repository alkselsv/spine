"""Offline, fail-closed preflight for a separately authorized Gmail run.

This module deliberately has no provider transport dependency. Under the
effective Option C policy every proposed live content operation is blocked.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from enum import Enum
from typing import Annotated, Literal, Protocol
from uuid import UUID

from pydantic import (
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StringConstraints,
    field_validator,
    model_validator,
)

from spine.domain.common import DefinitionModel, FrozenDict, ValidatedDefinitionModel

GMAIL_LIVE_PREFLIGHT_SCHEMA = "gmail-live-qualification-preflight:v1"
GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
EMAIL_SYNC_OPS_PROFILE = "email-sync-ops:v1"
OPTION_C_POLICY_REFERENCE = "issue-876-option-c:v1"
SafeConditionCode = Annotated[
    str,
    StringConstraints(min_length=2, max_length=64, pattern=r"^[a-z][a-z0-9_]*$"),
]


class EvidenceState(str, Enum):
    VERIFIED = "VERIFIED"
    NOT_VERIFIED = "NOT_VERIFIED"


class CriterionResult(str, Enum):
    VERIFIED = "VERIFIED"
    MISSING = "MISSING"
    NOT_APPROVED = "NOT_APPROVED"
    NOT_VERIFIED = "NOT_VERIFIED"
    DENY = "DENY"


class PreflightOutcome(str, Enum):
    BLOCKED_TECHNICAL = "BLOCKED_TECHNICAL"
    NOT_APPROVED = "NOT_APPROVED"


class OpaqueEvidence(DefinitionModel):
    """Opaque evidence locator; no free-form text can enter the contract."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    reference: UUID | None = None
    state: EvidenceState = EvidenceState.NOT_VERIFIED


class GmailQualificationManifest(ValidatedDefinitionModel):
    """Exact synthetic fixture and run plan. Sensitive provider data is unrepresentable."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["gmail-live-fixture-manifest:v1"]
    workspace_id: UUID
    environment_id: UUID
    connection_id: UUID
    connection_version_id: UUID
    configuration_generation: StrictInt = Field(ge=1)
    authorization_generation: StrictInt = Field(ge=1)
    scope_generation: StrictInt = Field(ge=1)
    mailbox_reference: UUID
    mailbox_organization_owned: StrictBool
    mailbox_synthetic: StrictBool
    included_label_references: tuple[UUID, ...] = Field(min_length=1)
    excluded_label_references: tuple[UUID, ...] = ()
    approved_included_label_references: tuple[UUID, ...] = Field(min_length=1)
    approved_excluded_label_references: tuple[UUID, ...] = ()
    scope: str = Field(
        pattern=r"^https://www\.googleapis\.com/auth/gmail\.[a-z]+$",
        max_length=80,
    )
    purpose_code: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    execution_window_id: UUID
    operator_reference: UUID
    backup_operator_reference: UUID | None = None
    policy_reference: Literal["issue-876-option-c:v1"] = OPTION_C_POLICY_REFERENCE
    ops_profile: Literal["email-sync-ops:v1"] = EMAIL_SYNC_OPS_PROFILE
    ops_limits: GmailOpsLimits | None = None
    plans_to_read_live_content: StrictBool = True
    retains_gmail_derived_bytes_or_identifiers: StrictBool = True
    evidence: dict[str, OpaqueEvidence]

    @field_validator("evidence")
    @classmethod
    def freeze_evidence(cls, value: dict[str, OpaqueEvidence]) -> FrozenDict:
        if isinstance(value, Mapping):
            return FrozenDict(value)
        raise ValueError("evidence must be an opaque mapping")

    @model_validator(mode="after")
    def validate_evidence_keys(self) -> GmailQualificationManifest:
        unknown = set(self.evidence) - set(_REQUIRED_EVIDENCE)
        if unknown:
            raise ValueError("evidence keys must use the approved criterion allowlist")
        if set(self.included_label_references) & set(self.excluded_label_references):
            raise ValueError("included and excluded label references must be disjoint")
        return self


class GmailOpsLimits(DefinitionModel):
    """Bounded runtime profile values; each value may tighten the #821 ceiling."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    poll_interval_minutes: Literal[15]
    freshness_target_minutes: StrictInt = Field(ge=1, le=30)
    reconciliation_days: StrictInt = Field(ge=1, le=7)
    initial_snapshot_jitter_hours: StrictInt = Field(ge=0, le=6)
    poll_jitter_percent: StrictInt = Field(ge=0, le=20)
    max_queue_age_hours: StrictInt = Field(ge=1, le=12)
    max_catch_up_age_hours: StrictInt = Field(ge=1, le=24)
    manual_resync_interval_minutes: StrictInt = Field(ge=30)
    manual_resyncs_per_day: StrictInt = Field(ge=1, le=3)
    deployment_mailboxes: StrictInt = Field(ge=1, le=4)
    workspace_mailboxes: StrictInt = Field(ge=1, le=2)
    requests_per_mailbox: StrictInt = Field(ge=1, le=1)
    deployment_quota_units_per_minute: StrictInt = Field(ge=1, le=60_000)
    deployment_quota_units_per_day: StrictInt = Field(ge=1, le=2_000_000)
    workspace_quota_units_per_minute: StrictInt = Field(ge=1, le=30_000)
    workspace_quota_units_per_day: StrictInt = Field(ge=1, le=1_500_000)
    mailbox_quota_units_per_minute: StrictInt = Field(ge=1, le=500)
    mailbox_quota_units_per_day: StrictInt = Field(ge=1, le=10_000)
    attempts_per_operation: StrictInt = Field(ge=1, le=5)
    retry_budget_seconds: StrictInt = Field(ge=1, le=1800)
    retry_backoff_min_seconds: StrictInt = Field(ge=1)
    retry_backoff_max_seconds: StrictInt = Field(ge=1, le=60)
    mailbox_breaker_failures: StrictInt = Field(ge=1, le=5)
    mailbox_breaker_window_minutes: StrictInt = Field(ge=1, le=10)
    provider_breaker_failure_percent: StrictInt = Field(ge=1, le=20)
    provider_breaker_min_attempts: StrictInt = Field(ge=1, le=20)
    provider_breaker_window_minutes: StrictInt = Field(ge=1, le=5)
    breaker_cooldown_minutes: StrictInt = Field(ge=5)
    connect_timeout_seconds: StrictInt = Field(ge=1, le=5)
    gmail_request_timeout_seconds: StrictInt = Field(ge=1, le=15)
    page_timeout_seconds: StrictInt = Field(ge=1, le=120)
    attempt_timeout_seconds: StrictInt = Field(ge=1, le=600)
    initial_snapshot_active_minutes: StrictInt = Field(ge=1, le=30)
    worker_heartbeat_seconds: StrictInt = Field(ge=1, le=15)
    lease_ttl_seconds: StrictInt = Field(ge=1, le=60)
    lease_renewal_seconds: StrictInt = Field(ge=1, le=20)
    cancellation_cleanup_seconds: StrictInt = Field(ge=1, le=30)
    per_attempt_quota_units: StrictInt = Field(ge=1, le=10_000)
    messages_per_attempt: StrictInt = Field(ge=1, le=100)
    raw_mime_bytes: StrictInt = Field(ge=0, le=0)
    parsed_body_bytes: StrictInt = Field(ge=1, le=5 * 1024 * 1024)
    html_bytes: StrictInt = Field(ge=1, le=2 * 1024 * 1024)
    header_bytes: StrictInt = Field(ge=1, le=64 * 1024)
    mime_parts: StrictInt = Field(ge=1, le=100)
    mime_nesting: StrictInt = Field(ge=1, le=10)
    attachments_per_message: StrictInt = Field(ge=1, le=10)
    attachment_bytes: StrictInt = Field(ge=1, le=10 * 1024 * 1024)
    aggregate_attachment_bytes: StrictInt = Field(ge=1, le=20 * 1024 * 1024)
    decoded_bytes: StrictInt = Field(ge=1, le=30 * 1024 * 1024)
    decompression_ratio: StrictInt = Field(ge=1, le=20)
    archive_depth: StrictInt = Field(ge=1, le=2)
    extracted_text_bytes: StrictInt = Field(ge=1, le=2 * 1024 * 1024)
    parser_cpu_seconds: StrictInt = Field(ge=1, le=2)
    parser_memory_bytes: StrictInt = Field(ge=1, le=128 * 1024 * 1024)
    parser_workers: StrictInt = Field(ge=1, le=2)
    attachment_workers: StrictInt = Field(ge=1, le=1)
    staging_ttl_seconds: StrictInt = Field(ge=1, le=3600)
    staging_scan_seconds: StrictInt = Field(ge=1, le=900)
    staging_cleanup_retries: StrictInt = Field(ge=1, le=5)
    staging_bytes_per_attempt: StrictInt = Field(ge=1, le=256 * 1024 * 1024)
    staging_bytes_per_environment: StrictInt = Field(ge=1, le=1024 * 1024 * 1024)
    queue_depth_deployment: StrictInt = Field(ge=1, le=500)
    queue_depth_workspace: StrictInt = Field(ge=1, le=50)
    sync_lag_warning_minutes: StrictInt = Field(ge=1, le=45)
    sync_lag_warning_duration_minutes: StrictInt = Field(ge=1, le=15)
    sync_lag_critical_minutes: StrictInt = Field(ge=1, le=120)
    sync_lag_critical_duration_minutes: StrictInt = Field(ge=1, le=30)
    provider_error_warning_percent: StrictInt = Field(ge=1, le=5)
    provider_error_warning_minutes: StrictInt = Field(ge=1, le=15)
    provider_error_critical_percent: StrictInt = Field(ge=1, le=25)
    provider_error_critical_minutes: StrictInt = Field(ge=1, le=10)
    mailbox_rate_limit_count: StrictInt = Field(ge=1, le=3)
    mailbox_rate_limit_window_minutes: StrictInt = Field(ge=1, le=5)
    environment_rate_limit_count: StrictInt = Field(ge=1, le=20)
    environment_rate_limit_window_minutes: StrictInt = Field(ge=1, le=10)
    reconciliation_warning_mailboxes: StrictInt = Field(ge=1, le=5)
    reconciliation_warning_age_hours: StrictInt = Field(ge=1, le=4)
    reconciliation_critical_mailboxes: StrictInt = Field(ge=1, le=20)
    reconciliation_critical_age_hours: StrictInt = Field(ge=1, le=24)
    cleanup_warning_minutes: StrictInt = Field(ge=1, le=15)
    cleanup_critical_minutes: StrictInt = Field(ge=1, le=60)

    @model_validator(mode="after")
    def validate_ordered_limits(self) -> GmailOpsLimits:
        if self.retry_backoff_min_seconds > self.retry_backoff_max_seconds:
            raise ValueError("retry backoff minimum must not exceed its maximum")
        if self.sync_lag_warning_minutes > self.sync_lag_critical_minutes:
            raise ValueError("sync lag warning must precede its critical threshold")
        if self.provider_error_warning_percent > self.provider_error_critical_percent:
            raise ValueError(
                "provider error warning must precede its critical threshold"
            )
        if self.cleanup_warning_minutes > self.cleanup_critical_minutes:
            raise ValueError("cleanup warning must precede its critical threshold")
        return self


class GmailExecutionAuthorization(DefinitionModel):
    """One-time approval bound to the exact manifest and bounded time window."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["gmail-live-execution-authorization:v1"]
    authorization_reference: UUID | None
    manifest_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    workspace_id: UUID
    environment_id: UUID
    connection_id: UUID
    connection_version_id: UUID
    configuration_generation: StrictInt = Field(ge=1)
    authorization_generation: StrictInt = Field(ge=1)
    scope_generation: StrictInt = Field(ge=1)
    approved_operation_profile: Literal["gmail-isolated-qualification:v1"]
    approved_window_start: datetime | None
    approved_window_end: datetime | None
    approver_reference: UUID | None
    approver_role: Literal["Project Owner"]
    decision: Literal["APPROVED", "NOT_APPROVED"]
    conditions: tuple[SafeConditionCode, ...] = ()

    @field_validator("approved_window_start", "approved_window_end")
    @classmethod
    def validate_aware_window(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("authorization window must be timezone-aware")
        return value


class CurrentGmailBinding(DefinitionModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    workspace_id: UUID
    environment_id: UUID
    connection_id: UUID
    connection_version_id: UUID
    configuration_generation: StrictInt = Field(ge=1)
    authorization_generation: StrictInt = Field(ge=1)
    scope_generation: StrictInt = Field(ge=1)
    active: StrictBool


class GmailCurrentBindingAuthority(Protocol):
    async def current_binding(
        self, manifest: GmailQualificationManifest
    ) -> CurrentGmailBinding | None: ...


class GmailPreflightCriterion(DefinitionModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    id: str
    result: CriterionResult
    evidence_reference: UUID | None = None
    safe_reason_code: str


class GmailPreflightResult(DefinitionModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["gmail-live-qualification-preflight:v1"] = (
        GMAIL_LIVE_PREFLIGHT_SCHEMA
    )
    outcome: PreflightOutcome
    manifest_digest: str
    criteria: tuple[GmailPreflightCriterion, ...]
    provider_call_permitted: Literal[False] = False
    policy_reference: Literal["issue-876-option-c:v1"] = OPTION_C_POLICY_REFERENCE


_APPROVAL_EVIDENCE = (
    "mailbox_owner_approval",
    "label_scope_approval",
    "data_owner_approval",
    "security_approval",
    "legal_compliance_approval",
    "operations_approval",
    "oauth_project_ownership",
    "oauth_app_ownership",
    "oauth_grant_scope",
    "workspace_admin_consent_authority",
)
_GOOGLE_EVIDENCE = (
    "restricted_scope_applicability",
    "restricted_scope_result",
    "google_quota_regime",
)
_ARTIFACT_CLASSES = (
    "originals",
    "parsed",
    "attachments",
    "projections",
    "indexes",
    "embeddings",
    "caches",
    "staging",
    "replicas",
    "backups",
    "diagnostics",
)
_RETENTION_EVIDENCE = tuple(f"retention_{name}" for name in _ARTIFACT_CLASSES)
_CLEANUP_EVIDENCE = tuple(f"cleanup_{name}" for name in _ARTIFACT_CLASSES)
_REQUIRED_EVIDENCE = (
    *_APPROVAL_EVIDENCE,
    *_GOOGLE_EVIDENCE,
    *_RETENTION_EVIDENCE,
    *_CLEANUP_EVIDENCE,
    "one_time_execution_authorization",
)


class GmailLiveQualificationPreflight:
    """Evaluate exact bindings offline; never constructs or calls a provider."""

    def __init__(self, authority: GmailCurrentBindingAuthority) -> None:
        self._authority = authority

    async def evaluate(
        self,
        manifest: GmailQualificationManifest,
        *,
        execution_authorization: GmailExecutionAuthorization | None = None,
        now: datetime,
    ) -> GmailPreflightResult:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("preflight clock must be timezone-aware")
        digest = manifest_digest(manifest)
        criteria: list[GmailPreflightCriterion] = []

        def add(
            identifier: str,
            result: CriterionResult,
            reason: str,
            ref: UUID | None = None,
        ) -> None:
            criteria.append(
                GmailPreflightCriterion(
                    id=identifier,
                    result=result,
                    evidence_reference=ref,
                    safe_reason_code=reason,
                )
            )

        scope_mismatch = (
            manifest.scope != GMAIL_READONLY_SCOPE
            or not manifest.included_label_references
            or manifest.included_label_references
            != manifest.approved_included_label_references
            or manifest.excluded_label_references
            != manifest.approved_excluded_label_references
        )
        add(
            "exact_scope",
            CriterionResult.DENY if scope_mismatch else CriterionResult.VERIFIED,
            "scope_mismatch" if scope_mismatch else "scope_exact",
        )

        try:
            binding = await self._authority.current_binding(manifest)
            binding_available = True
        except Exception:  # noqa: BLE001 -- authority failures must produce a content-free denial.
            binding = None
            binding_available = False
        binding_matches = _binding_matches(manifest, binding)
        current_binding_result = (
            CriterionResult.NOT_VERIFIED
            if not binding_available
            else CriterionResult.VERIFIED
            if binding_matches
            else CriterionResult.DENY
        )
        current_binding_reason = (
            "binding_unavailable"
            if not binding_available
            else "binding_current"
            if binding_matches
            else "binding_mismatch"
        )
        add("current_binding", current_binding_result, current_binding_reason)
        synthetic_mailbox_ok = (
            manifest.mailbox_organization_owned and manifest.mailbox_synthetic
        )
        add(
            "synthetic_mailbox",
            CriterionResult.VERIFIED if synthetic_mailbox_ok else CriterionResult.DENY,
            "synthetic_mailbox_bound"
            if synthetic_mailbox_ok
            else "mailbox_not_approved_synthetic",
        )

        for key in _APPROVAL_EVIDENCE:
            evidence = manifest.evidence.get(key)
            if evidence is None or evidence.reference is None:
                add(key, CriterionResult.NOT_APPROVED, "approval_missing")
            elif evidence.state is not EvidenceState.VERIFIED:
                add(
                    key,
                    CriterionResult.NOT_VERIFIED,
                    "evidence_unverified",
                    evidence.reference,
                )
            else:
                add(
                    key,
                    CriterionResult.VERIFIED,
                    "evidence_verified",
                    evidence.reference,
                )

        for key in _GOOGLE_EVIDENCE:
            evidence = manifest.evidence.get(key)
            if evidence is None or evidence.reference is None:
                add(key, CriterionResult.NOT_VERIFIED, "external_result_missing")
            elif evidence.state is not EvidenceState.VERIFIED:
                add(
                    key,
                    CriterionResult.NOT_VERIFIED,
                    "external_result_unverified",
                    evidence.reference,
                )
            else:
                add(
                    key,
                    CriterionResult.VERIFIED,
                    "external_result_verified",
                    evidence.reference,
                )

        # Option C is evaluated before retention evidence: no evidence can override it.
        retains = (
            manifest.plans_to_read_live_content
            or manifest.retains_gmail_derived_bytes_or_identifiers
        )
        add(
            "issue_876_option_c",
            CriterionResult.DENY if retains else CriterionResult.NOT_APPROVED,
            "option_c_no_go" if retains else "future_option_b_not_approved",
        )

        for key in (*_RETENTION_EVIDENCE, *_CLEANUP_EVIDENCE):
            evidence = manifest.evidence.get(key)
            if evidence is None or evidence.reference is None:
                add(key, CriterionResult.NOT_APPROVED, "retention_or_cleanup_missing")
            elif evidence.state is not EvidenceState.VERIFIED:
                add(
                    key,
                    CriterionResult.NOT_VERIFIED,
                    "retention_or_cleanup_unverified",
                    evidence.reference,
                )
            else:
                add(
                    key,
                    CriterionResult.VERIFIED,
                    "retention_or_cleanup_verified",
                    evidence.reference,
                )

        operators_present = manifest.operator_reference.int != 0 and (
            manifest.backup_operator_reference is not None
            and manifest.backup_operator_reference.int != 0
        )
        ops_profile_bound = (
            manifest.ops_profile == EMAIL_SYNC_OPS_PROFILE
            and manifest.ops_limits is not None
        )
        add(
            "ops_profile",
            CriterionResult.VERIFIED
            if ops_profile_bound and operators_present
            else CriterionResult.NOT_APPROVED,
            "ops_profile_pinned"
            if ops_profile_bound and operators_present
            else "ops_profile_limits_or_operator_missing",
        )

        authorization_result, authorization_reason, authorization_ref = (
            _authorization_status(manifest, execution_authorization, digest, now)
        )
        add(
            "one_time_execution_authorization",
            authorization_result,
            authorization_reason,
            authorization_ref,
        )
        add(
            "provider_call_permitted",
            CriterionResult.DENY,
            "live_provider_calls_disabled",
        )

        # Revalidate generations at the end, immediately before producing the decision.
        try:
            latest_binding = await self._authority.current_binding(manifest)
            latest_binding_available = True
        except Exception:  # noqa: BLE001 -- final authority failures must fail closed.
            latest_binding = None
            latest_binding_available = False
        if not latest_binding_available:
            add(
                "final_generation_revalidation",
                CriterionResult.NOT_VERIFIED,
                "generation_revalidation_unavailable",
            )
        elif not _binding_matches(manifest, latest_binding):
            add(
                "final_generation_revalidation",
                CriterionResult.DENY,
                "generation_changed",
            )
        else:
            add(
                "final_generation_revalidation",
                CriterionResult.VERIFIED,
                "generation_current",
            )

        outcome = (
            PreflightOutcome.BLOCKED_TECHNICAL
            if any(item.result is CriterionResult.DENY for item in criteria)
            else PreflightOutcome.NOT_APPROVED
        )
        return GmailPreflightResult(
            outcome=outcome, manifest_digest=digest, criteria=tuple(criteria)
        )


def manifest_digest(manifest: GmailQualificationManifest) -> str:
    """Canonical SHA-256 binding without serializing free-form sensitive values."""
    import hashlib
    import json

    payload = manifest.model_dump(mode="json")
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()


def _binding_matches(
    manifest: GmailQualificationManifest, current: CurrentGmailBinding | None
) -> bool:
    return (
        current is not None
        and current.active
        and (
            current.workspace_id == manifest.workspace_id
            and current.environment_id == manifest.environment_id
            and current.connection_id == manifest.connection_id
            and current.connection_version_id == manifest.connection_version_id
            and current.configuration_generation == manifest.configuration_generation
            and current.authorization_generation == manifest.authorization_generation
            and current.scope_generation == manifest.scope_generation
        )
    )


def _authorization_status(
    manifest: GmailQualificationManifest,
    authorization: GmailExecutionAuthorization | None,
    digest: str,
    now: datetime,
) -> tuple[CriterionResult, str, UUID | None]:
    if (
        authorization is None
        or authorization.decision != "APPROVED"
        or authorization.authorization_reference is None
    ):
        return CriterionResult.NOT_APPROVED, "run_authorization_missing", None
    evidence = manifest.evidence.get("one_time_execution_authorization")
    if evidence is None or evidence.reference is None:
        return (
            CriterionResult.NOT_APPROVED,
            "run_authorization_evidence_missing",
            authorization.authorization_reference,
        )
    if evidence.state is not EvidenceState.VERIFIED:
        return (
            CriterionResult.NOT_VERIFIED,
            "run_authorization_evidence_unverified",
            evidence.reference,
        )
    matches = (
        authorization.manifest_digest == digest
        and evidence.reference == authorization.authorization_reference
        and authorization.workspace_id == manifest.workspace_id
        and authorization.environment_id == manifest.environment_id
        and authorization.connection_id == manifest.connection_id
        and authorization.connection_version_id == manifest.connection_version_id
        and authorization.configuration_generation == manifest.configuration_generation
        and authorization.authorization_generation == manifest.authorization_generation
        and authorization.scope_generation == manifest.scope_generation
        and authorization.approver_reference is not None
        and authorization.approved_window_start is not None
        and authorization.approved_window_end is not None
        and authorization.approved_window_start
        <= now
        <= authorization.approved_window_end
        and authorization.approved_window_start < authorization.approved_window_end
    )
    if not matches:
        return (
            CriterionResult.DENY,
            "run_authorization_mismatch",
            authorization.authorization_reference,
        )
    return (
        CriterionResult.VERIFIED,
        "run_authorization_bound",
        authorization.authorization_reference,
    )


__all__ = [
    "EMAIL_SYNC_OPS_PROFILE",
    "GMAIL_LIVE_PREFLIGHT_SCHEMA",
    "GMAIL_READONLY_SCOPE",
    "CriterionResult",
    "CurrentGmailBinding",
    "EvidenceState",
    "GmailCurrentBindingAuthority",
    "GmailExecutionAuthorization",
    "GmailLiveQualificationPreflight",
    "GmailOpsLimits",
    "GmailPreflightCriterion",
    "GmailPreflightResult",
    "GmailQualificationManifest",
    "OpaqueEvidence",
    "PreflightOutcome",
    "manifest_digest",
]
