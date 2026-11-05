from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.application.gmail_live_qualification_preflight import (
    CriterionResult,
    CurrentGmailBinding,
    EvidenceState,
    GmailExecutionAuthorization,
    GmailLiveQualificationPreflight,
    GmailOpsLimits,
    GmailQualificationManifest,
    OpaqueEvidence,
    PreflightOutcome,
    manifest_digest,
)

NOW = datetime(2026, 11, 5, tzinfo=timezone.utc)
WORKSPACE = UUID(int=1)
ENVIRONMENT = UUID(int=2)
CONNECTION = UUID(int=3)
VERSION = UUID(int=4)
ARTIFACT_CLASSES = (
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
EVIDENCE_KEYS = (
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
    "restricted_scope_applicability",
    "restricted_scope_result",
    "google_quota_regime",
    *(f"retention_{name}" for name in ARTIFACT_CLASSES),
    *(f"cleanup_{name}" for name in ARTIFACT_CLASSES),
    "one_time_execution_authorization",
)


def _ops_limits() -> GmailOpsLimits:
    return GmailOpsLimits(
        poll_interval_minutes=15,
        freshness_target_minutes=30,
        reconciliation_days=7,
        initial_snapshot_jitter_hours=6,
        poll_jitter_percent=20,
        max_queue_age_hours=12,
        max_catch_up_age_hours=24,
        manual_resync_interval_minutes=30,
        manual_resyncs_per_day=3,
        deployment_mailboxes=4,
        workspace_mailboxes=2,
        requests_per_mailbox=1,
        deployment_quota_units_per_minute=60_000,
        deployment_quota_units_per_day=2_000_000,
        workspace_quota_units_per_minute=30_000,
        workspace_quota_units_per_day=1_500_000,
        mailbox_quota_units_per_minute=500,
        mailbox_quota_units_per_day=10_000,
        attempts_per_operation=5,
        retry_budget_seconds=1800,
        retry_backoff_min_seconds=1,
        retry_backoff_max_seconds=60,
        mailbox_breaker_failures=5,
        mailbox_breaker_window_minutes=10,
        provider_breaker_failure_percent=20,
        provider_breaker_min_attempts=20,
        provider_breaker_window_minutes=5,
        breaker_cooldown_minutes=5,
        connect_timeout_seconds=5,
        gmail_request_timeout_seconds=15,
        page_timeout_seconds=120,
        attempt_timeout_seconds=600,
        initial_snapshot_active_minutes=30,
        worker_heartbeat_seconds=15,
        lease_ttl_seconds=60,
        lease_renewal_seconds=20,
        cancellation_cleanup_seconds=30,
        per_attempt_quota_units=10_000,
        messages_per_attempt=100,
        raw_mime_bytes=0,
        parsed_body_bytes=5 * 1024 * 1024,
        html_bytes=2 * 1024 * 1024,
        header_bytes=64 * 1024,
        mime_parts=100,
        mime_nesting=10,
        attachments_per_message=10,
        attachment_bytes=10 * 1024 * 1024,
        aggregate_attachment_bytes=20 * 1024 * 1024,
        decoded_bytes=30 * 1024 * 1024,
        decompression_ratio=20,
        archive_depth=2,
        extracted_text_bytes=2 * 1024 * 1024,
        parser_cpu_seconds=2,
        parser_memory_bytes=128 * 1024 * 1024,
        parser_workers=2,
        attachment_workers=1,
        staging_ttl_seconds=3600,
        staging_scan_seconds=900,
        staging_cleanup_retries=5,
        staging_bytes_per_attempt=256 * 1024 * 1024,
        staging_bytes_per_environment=1024 * 1024 * 1024,
        queue_depth_deployment=500,
        queue_depth_workspace=50,
        sync_lag_warning_minutes=45,
        sync_lag_warning_duration_minutes=15,
        sync_lag_critical_minutes=120,
        sync_lag_critical_duration_minutes=30,
        provider_error_warning_percent=5,
        provider_error_warning_minutes=15,
        provider_error_critical_percent=25,
        provider_error_critical_minutes=10,
        mailbox_rate_limit_count=3,
        mailbox_rate_limit_window_minutes=5,
        environment_rate_limit_count=20,
        environment_rate_limit_window_minutes=10,
        reconciliation_warning_mailboxes=5,
        reconciliation_warning_age_hours=4,
        reconciliation_critical_mailboxes=20,
        reconciliation_critical_age_hours=24,
        cleanup_warning_minutes=15,
        cleanup_critical_minutes=60,
    )


def _manifest(**overrides) -> GmailQualificationManifest:
    values = {
        "schema_version": "gmail-live-fixture-manifest:v1",
        "workspace_id": WORKSPACE,
        "environment_id": ENVIRONMENT,
        "connection_id": CONNECTION,
        "connection_version_id": VERSION,
        "configuration_generation": 7,
        "authorization_generation": 9,
        "scope_generation": 11,
        "mailbox_reference": UUID(int=5),
        "mailbox_organization_owned": True,
        "mailbox_synthetic": True,
        "included_label_references": (UUID(int=6),),
        "excluded_label_references": (UUID(int=7),),
        "approved_included_label_references": (UUID(int=6),),
        "approved_excluded_label_references": (UUID(int=7),),
        "scope": "https://www.googleapis.com/auth/gmail.readonly",
        "purpose_code": "isolated_qualification",
        "execution_window_id": UUID(int=8),
        "operator_reference": UUID(int=9),
        "backup_operator_reference": UUID(int=10),
        "ops_limits": _ops_limits(),
        "plans_to_read_live_content": True,
        "retains_gmail_derived_bytes_or_identifiers": True,
        "evidence": {
            key: OpaqueEvidence(
                reference=UUID(int=index + 20), state=EvidenceState.VERIFIED
            )
            for index, key in enumerate(EVIDENCE_KEYS)
        },
    }
    values.update(overrides)
    return GmailQualificationManifest(**values)


def _binding(**overrides) -> CurrentGmailBinding:
    values = {
        "workspace_id": WORKSPACE,
        "environment_id": ENVIRONMENT,
        "connection_id": CONNECTION,
        "connection_version_id": VERSION,
        "configuration_generation": 7,
        "authorization_generation": 9,
        "scope_generation": 11,
        "active": True,
    }
    values.update(overrides)
    return CurrentGmailBinding(**values)


class _Authority:
    def __init__(self, *bindings: CurrentGmailBinding | None) -> None:
        self.bindings = list(bindings)
        self.calls = 0

    async def current_binding(self, manifest):
        del manifest
        binding = self.bindings[min(self.calls, len(self.bindings) - 1)]
        self.calls += 1
        return binding


def _authorization(manifest: GmailQualificationManifest) -> GmailExecutionAuthorization:
    return GmailExecutionAuthorization(
        schema_version="gmail-live-execution-authorization:v1",
        authorization_reference=manifest.evidence[
            "one_time_execution_authorization"
        ].reference,
        manifest_digest=manifest_digest(manifest),
        workspace_id=manifest.workspace_id,
        environment_id=manifest.environment_id,
        connection_id=manifest.connection_id,
        connection_version_id=manifest.connection_version_id,
        configuration_generation=manifest.configuration_generation,
        authorization_generation=manifest.authorization_generation,
        scope_generation=manifest.scope_generation,
        approved_operation_profile="gmail-isolated-qualification:v1",
        approved_window_start=NOW - timedelta(minutes=1),
        approved_window_end=NOW + timedelta(minutes=1),
        approver_reference=UUID(int=101),
        approver_role="Project Owner",
        decision="APPROVED",
    )


@pytest.mark.asyncio
async def test_current_option_c_blocks_even_fully_approved_manifest_before_provider_access() -> (
    None
):
    manifest = _manifest()
    authority = _Authority(_binding(), _binding())

    result = await GmailLiveQualificationPreflight(authority).evaluate(
        manifest, execution_authorization=_authorization(manifest), now=NOW
    )

    assert result.outcome is PreflightOutcome.BLOCKED_TECHNICAL
    assert result.provider_call_permitted is False
    assert result.policy_reference == "issue-876-option-c:v1"
    assert (
        next(item for item in result.criteria if item.id == "issue_876_option_c").result
        is CriterionResult.DENY
    )
    assert authority.calls == 2


@pytest.mark.asyncio
async def test_missing_approval_returns_not_approved_without_executable_decision() -> (
    None
):
    manifest = _manifest(evidence={})
    authority = _Authority(_binding(), _binding())

    result = await GmailLiveQualificationPreflight(authority).evaluate(
        manifest, now=NOW
    )

    assert result.outcome is PreflightOutcome.BLOCKED_TECHNICAL
    assert result.provider_call_permitted is False
    assert (
        next(
            item for item in result.criteria if item.id == "mailbox_owner_approval"
        ).result
        is CriterionResult.NOT_APPROVED
    )
    assert (
        next(
            item
            for item in result.criteria
            if item.id == "one_time_execution_authorization"
        ).result
        is CriterionResult.NOT_APPROVED
    )


@pytest.mark.parametrize(
    "missing_key",
    (
        "mailbox_owner_approval",
        "label_scope_approval",
        "data_owner_approval",
        "oauth_grant_scope",
        "retention_originals",
        "cleanup_backups",
        "one_time_execution_authorization",
    ),
)
@pytest.mark.asyncio
async def test_each_missing_required_evidence_stays_not_approved(
    missing_key: str,
) -> None:
    evidence = dict(_manifest().evidence)
    evidence.pop(missing_key)
    manifest = _manifest(evidence=evidence)

    result = await GmailLiveQualificationPreflight(
        _Authority(_binding(), _binding())
    ).evaluate(manifest, now=NOW)

    assert (
        next(item for item in result.criteria if item.id == missing_key).result
        is CriterionResult.NOT_APPROVED
    )
    assert result.provider_call_permitted is False


@pytest.mark.asyncio
async def test_unverified_external_evidence_remains_not_verified() -> None:
    evidence = dict(_manifest().evidence)
    evidence["restricted_scope_result"] = OpaqueEvidence(
        reference=UUID(int=500), state=EvidenceState.NOT_VERIFIED
    )
    manifest = _manifest(evidence=evidence)

    result = await GmailLiveQualificationPreflight(
        _Authority(_binding(), _binding())
    ).evaluate(manifest, now=NOW)

    item = next(
        item for item in result.criteria if item.id == "restricted_scope_result"
    )
    assert item.result is CriterionResult.NOT_VERIFIED
    assert item.evidence_reference == UUID(int=500)


@pytest.mark.asyncio
async def test_generation_change_during_preflight_is_denied() -> None:
    manifest = _manifest()

    result = await GmailLiveQualificationPreflight(
        _Authority(_binding(), _binding(authorization_generation=10))
    ).evaluate(manifest, now=NOW)

    assert (
        next(
            item
            for item in result.criteria
            if item.id == "final_generation_revalidation"
        ).result
        is CriterionResult.DENY
    )
    assert result.provider_call_permitted is False


@pytest.mark.asyncio
async def test_scope_generation_change_is_denied() -> None:
    manifest = _manifest()
    result = await GmailLiveQualificationPreflight(
        _Authority(_binding(scope_generation=12), _binding(scope_generation=12))
    ).evaluate(manifest, now=NOW)

    assert (
        next(item for item in result.criteria if item.id == "current_binding").result
        is CriterionResult.DENY
    )


@pytest.mark.asyncio
async def test_authorization_must_match_the_exact_manifest_and_evidence_reference() -> (
    None
):
    manifest = _manifest()
    authorization = _authorization(manifest).model_copy(
        update={"manifest_digest": "0" * 64}
    )

    result = await GmailLiveQualificationPreflight(
        _Authority(_binding(), _binding())
    ).evaluate(manifest, execution_authorization=authorization, now=NOW)

    assert (
        next(
            item
            for item in result.criteria
            if item.id == "one_time_execution_authorization"
        ).result
        is CriterionResult.DENY
    )
    assert result.provider_call_permitted is False


@pytest.mark.asyncio
async def test_cross_workspace_current_binding_is_denied() -> None:
    manifest = _manifest()
    result = await GmailLiveQualificationPreflight(
        _Authority(
            _binding(workspace_id=UUID(int=99)), _binding(workspace_id=UUID(int=99))
        )
    ).evaluate(manifest, now=NOW)

    assert (
        next(item for item in result.criteria if item.id == "current_binding").result
        is CriterionResult.DENY
    )


def test_manifest_rejects_provider_content_secret_fields() -> None:
    manifest_values = _manifest().model_dump()
    manifest_values["mailbox_address"] = "synthetic@example.test"
    with pytest.raises(ValidationError):
        GmailQualificationManifest(**manifest_values)


@pytest.mark.asyncio
async def test_non_readonly_scope_is_denied() -> None:
    manifest_values = _manifest().model_dump()
    manifest_values["scope"] = "https://www.googleapis.com/auth/gmail.modify"
    manifest = GmailQualificationManifest(**manifest_values)
    result = await GmailLiveQualificationPreflight(
        _Authority(_binding(), _binding())
    ).evaluate(manifest, now=NOW)
    assert (
        next(item for item in result.criteria if item.id == "exact_scope").result
        is CriterionResult.DENY
    )


@pytest.mark.asyncio
async def test_label_scope_outside_approved_allowlist_is_denied() -> None:
    manifest = _manifest(approved_included_label_references=(UUID(int=88),))
    result = await GmailLiveQualificationPreflight(
        _Authority(_binding(), _binding())
    ).evaluate(manifest, now=NOW)

    assert (
        next(item for item in result.criteria if item.id == "exact_scope").result
        is CriterionResult.DENY
    )


def test_manifest_rejects_scope_values_that_cannot_be_safe_provider_scope_ids() -> None:
    manifest_values = _manifest().model_dump()
    manifest_values["scope"] = "private mailbox content"
    with pytest.raises(ValidationError):
        GmailQualificationManifest(**manifest_values)


def test_ops_profile_rejects_expansion_beyond_approved_limits() -> None:
    values = _ops_limits().model_dump()
    values["poll_interval_minutes"] = 16
    with pytest.raises(ValidationError):
        GmailOpsLimits(**values)

    values = _ops_limits().model_dump()
    values["poll_interval_minutes"] = 14
    with pytest.raises(ValidationError):
        GmailOpsLimits(**values)

    values = _ops_limits().model_dump()
    values["deployment_quota_units_per_day"] = 2_000_001
    with pytest.raises(ValidationError):
        GmailOpsLimits(**values)


def test_execution_authorization_rejects_unbounded_role_or_condition_text() -> None:
    manifest = _manifest()
    authorization = _authorization(manifest).model_dump()
    authorization["approver_role"] = "Mailbox Owner"
    with pytest.raises(ValidationError):
        GmailExecutionAuthorization(**authorization)

    authorization = _authorization(manifest).model_dump()
    authorization["conditions"] = ["synthetic@example.test"]
    with pytest.raises(ValidationError):
        GmailExecutionAuthorization(**authorization)


def test_manifest_rejects_unknown_evidence_names_and_serializes_only_safe_fields() -> (
    None
):
    manifest_values = _manifest().model_dump()
    manifest_values["evidence"]["subject"] = OpaqueEvidence(reference=UUID(int=999))
    with pytest.raises(ValidationError):
        GmailQualificationManifest(**manifest_values)

    serialized = _manifest().model_dump_json()
    assert "mailbox_address" not in serialized
    assert "subject" not in serialized
    assert "access_token" not in serialized


def test_manifest_evidence_is_immutable_after_digest_binding() -> None:
    manifest = _manifest()
    with pytest.raises(TypeError):
        manifest.evidence["mailbox_owner_approval"] = OpaqueEvidence(
            reference=UUID(int=999)
        )


def test_preflight_application_module_does_not_import_gmail_transport() -> None:
    path = (
        Path(__file__).resolve().parents[2]
        / "src/spine/application/gmail_live_qualification_preflight.py"
    )
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = {
        node.module if isinstance(node, ast.ImportFrom) else alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in (
            node.names
            if isinstance(node, ast.Import)
            else [ast.alias(name=node.module or "")]
        )
    }
    assert not any(
        value and value.startswith("spine.connectors.gmail") for value in imported
    )
