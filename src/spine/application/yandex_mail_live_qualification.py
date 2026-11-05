"""Offline, fail-closed gate for a separately authorized Yandex Mail run."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from enum import Enum
from typing import Literal, Protocol
from uuid import UUID

from pydantic import Field, StrictInt, StringConstraints
from typing_extensions import Annotated

from spine.domain.common import DefinitionModel

OpaqueRef = Annotated[str, StringConstraints(pattern=r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-8][0-9a-fA-F]{3}-[89aAbB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$")]


class Decision(str, Enum):
    VERIFIED = "VERIFIED"
    NOT_APPROVED = "NOT_APPROVED"
    NOT_VERIFIED = "NOT_VERIFIED"
    DENY = "DENY"


class Evidence(DefinitionModel):
    reference: OpaqueRef
    verified: bool = False
    subject_refs: tuple[OpaqueRef, ...] = ()
    artifact_classes: tuple[str, ...] = ()


class QualificationEvidence(DefinitionModel):
    mailbox_owner: Evidence | None = None
    data_owner: Evidence | None = None
    security: Evidence | None = None
    legal_compliance: Evidence | None = None
    operations: Evidence | None = None
    oauth_app: Evidence | None = None
    oauth_account: Evidence | None = None
    oauth_grant: Evidence | None = None
    corporate_agreement: Evidence | None = None
    scope: Evidence | None = None
    tls_xoauth2: Evidence | None = None
    retention: Evidence | None = None
    cleanup_revocation: Evidence | None = None
    incident_contacts: Evidence | None = None


class OpsProfile(DefinitionModel):
    commands_per_minute: int = 20
    commands_per_day: int = 2000
    commands_per_attempt: int = 100
    fetches_per_attempt: int = 25
    attempts: int = 5
    logical_operation_seconds: int = 1800
    connect_timeout_seconds: int = 5
    tls_timeout_seconds: int = 5
    command_timeout_seconds: int = 30
    attempt_seconds: int = 600
    raw_mime_bytes: int = 20 * 1024 * 1024
    message_bytes: int = 20 * 1024 * 1024
    attachment_bytes: int = 10 * 1024 * 1024
    attachments_per_message: int = 20
    archive_bytes: int = 30 * 1024 * 1024
    archive_ratio: int = 20
    archive_depth: int = 2
    extracted_text_bytes: int = 2 * 1024 * 1024
    parser_cpu_seconds: int = 2
    parser_memory_bytes: int = 128 * 1024 * 1024
    parser_concurrency: int = 2
    attachment_concurrency: int = 1
    staging_ttl_seconds: int = 3600
    staging_scan_minutes: int = 15
    staging_bytes_per_attempt: int = 256 * 1024 * 1024
    staging_bytes_per_environment: int = 1024 * 1024 * 1024
    cleanup_retries: int = 5
    cleanup_seconds: int = 600
    cleanup_warning_minutes: int = 15
    cleanup_critical_minutes: int = 60
    alert_on_staging_cap_breach: bool = True
    alert_on_backup_cleanup_failure: bool = True


PINNED_OPS_PROFILE = OpsProfile()


class YandexMailQualificationManifest(DefinitionModel):
    schema: str = "yandex-mail-live-fixture-manifest:v1"
    workspace_id: UUID
    environment_id: UUID
    connection_id: UUID
    connection_version_id: UUID
    configuration_generation: StrictInt = Field(ge=1)
    authorization_generation: StrictInt = Field(ge=1)
    mailbox_ref: OpaqueRef
    oauth_app_ref: OpaqueRef
    oauth_account_ref: OpaqueRef
    oauth_grant_ref: OpaqueRef
    included_folder_refs: tuple[OpaqueRef, ...]
    excluded_folder_refs: tuple[OpaqueRef, ...]
    purpose_ref: OpaqueRef
    operator_ref: OpaqueRef
    backup_operator_ref: OpaqueRef
    window_start: datetime
    window_end: datetime
    evidence: QualificationEvidence
    oauth_scope: str = "mail:imap_ro"
    transport_host_profile: str = "yandex-imap-tls993:v1"
    port: StrictInt = 993
    tls_certificate_validated: bool = True
    auth_mechanism: str = "XOAUTH2"
    commands: tuple[str, ...] = ("EXAMINE", "UID SEARCH", "UID FETCH PEEK")
    retention_classes: tuple[str, ...] = ("originals", "parsed", "attachments", "projections", "caches", "staging", "replicas", "backups", "diagnostics")
    ops_profile: str = "email-sync-ops:v1"
    ops_limits: OpsProfile = PINNED_OPS_PROFILE


class ExecutionAuthorization(DefinitionModel):
    schema: str = "yandex-mail-live-execution-authorization:v1"
    authorization_ref: OpaqueRef
    manifest_digest: str
    workspace_id: UUID
    environment_id: UUID
    connection_id: UUID
    connection_version_id: UUID
    configuration_generation: StrictInt = Field(ge=1)
    authorization_generation: StrictInt = Field(ge=1)
    approved_operation_profile: str = "yandex-mail-isolated-qualification:v1"
    approved_window_start: datetime
    approved_window_end: datetime
    approver_role: str = "Project Owner"
    approver_ref: OpaqueRef
    decision: Literal["APPROVED", "NOT_APPROVED"] = "NOT_APPROVED"
    conditions: tuple[str, ...] = ()


class CurrentBinding(DefinitionModel):
    workspace_id: UUID
    environment_id: UUID
    connection_id: UUID
    connection_version_id: UUID
    configuration_generation: StrictInt
    authorization_generation: StrictInt
    evaluated_at: datetime
    mailbox_ref: OpaqueRef
    folder_refs: tuple[OpaqueRef, ...]


class Criterion(DefinitionModel):
    id: str
    result: Decision
    evidence_ref: OpaqueRef | None = None
    safe_code: str


class PreflightReport(DefinitionModel):
    schema: str = "yandex-mail-live-qualification-report:v1"
    decision: Decision
    manifest_digest: str
    authorization_ref: OpaqueRef | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    cleanup_status: str = "NOT_VERIFIED"
    revocation_status: str = "NOT_VERIFIED"
    provider_call_permitted: bool = False
    criteria: tuple[Criterion, ...]
    contains_mailbox_content: bool = False
    production_generalization: str = "PROHIBITED"


class FakeYandexMailPort(Protocol):
    async def qualify(self, manifest: YandexMailQualificationManifest) -> object: ...


class AuthorizationGate(Protocol):
    async def current_binding(self, manifest: YandexMailQualificationManifest) -> CurrentBinding: ...
    async def consume(self, authorization_ref: str, digest: str, *, at: datetime) -> bool: ...


REQUIRED_EVIDENCE = tuple(QualificationEvidence.model_fields)
REQUIRED_STORES = {"originals", "parsed", "attachments", "projections", "caches", "staging", "replicas", "backups", "diagnostics"}
ALLOWED_COMMANDS = {"EXAMINE", "UID SEARCH", "UID FETCH PEEK"}
DENIED_TOKENS = ("password", "app-password", "pop3", "smtp", "mail:imap_full", "store", "copy", "move", "expunge", "append", "yandex-disk")


def manifest_digest(manifest: YandexMailQualificationManifest) -> str:
    payload = json.dumps(manifest.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def evaluate_yandex_mail_preflight(manifest: YandexMailQualificationManifest, authorization: ExecutionAuthorization | None, current: CurrentBinding) -> PreflightReport:
    """Evaluate all approvals before any connector/session can be composed."""
    digest = manifest_digest(manifest)
    base = (manifest.workspace_id, manifest.environment_id, manifest.connection_id, manifest.connection_version_id)
    now = current.evaluated_at
    auth = authorization
    result = Decision.VERIFIED
    rows: list[Criterion] = []
    def add(key: str, decision: Decision, code: str, ref: str | None = None) -> None:
        nonlocal result
        rows.append(Criterion(id=key, result=decision, safe_code=code, evidence_ref=ref))
        if decision == Decision.DENY or (decision == Decision.NOT_VERIFIED and result != Decision.DENY) or (decision == Decision.NOT_APPROVED and result == Decision.VERIFIED): result = decision
    serialized = json.dumps(manifest.model_dump(mode="json"), sort_keys=True).lower()
    protected = bool(re.search(r"[\w.+-]+@[\w.-]+\.[a-z]{2,}|bearer\s+|oauth\s|password|app-password|imap_full|pop3|smtp|expunge|append", serialized))
    if protected:
        add("schema_redaction", Decision.DENY, "protected_value")
    else: add("schema_redaction", Decision.VERIFIED, "redacted")
    if not manifest.included_folder_refs or set(manifest.included_folder_refs) & set(manifest.excluded_folder_refs): add("exact_scope", Decision.DENY, "scope_invalid")
    elif base != (current.workspace_id, current.environment_id, current.connection_id, current.connection_version_id): add("exact_scope", Decision.DENY, "binding_mismatch")
    elif current.mailbox_ref != manifest.mailbox_ref: add("exact_scope", Decision.DENY, "mailbox_mismatch")
    elif set(current.folder_refs) != set(manifest.included_folder_refs): add("exact_scope", Decision.DENY, "folder_mismatch")
    else: add("exact_scope", Decision.VERIFIED, "scope_bound")
    if (manifest.configuration_generation, manifest.authorization_generation) != (current.configuration_generation, current.authorization_generation): add("generations", Decision.DENY, "stale_generation")
    else: add("generations", Decision.VERIFIED, "current_generation")
    missing = [key for key in REQUIRED_EVIDENCE if getattr(manifest.evidence, key) is None or not getattr(manifest.evidence, key).verified]
    for key in REQUIRED_EVIDENCE:
        ev = getattr(manifest.evidence, key)
        d = Decision.NOT_VERIFIED if key in {"corporate_agreement", "oauth_grant"} and ev is not None and not ev.verified else Decision.NOT_APPROVED
        anchors = {"mailbox_owner": {manifest.mailbox_ref}, "data_owner": {manifest.mailbox_ref, manifest.purpose_ref, *manifest.included_folder_refs, *manifest.excluded_folder_refs}, "oauth_app": {manifest.oauth_app_ref}, "oauth_account": {manifest.oauth_account_ref}, "oauth_grant": {manifest.oauth_grant_ref}}
        unbound = ev is not None and key in anchors and not anchors[key] <= set(ev.subject_refs)
        add(key, Decision.NOT_APPROVED if unbound else (d if key in missing else Decision.VERIFIED), "evidence_unbound" if unbound else ("evidence_missing" if key in missing else "evidence_verified"), ev.reference if ev else None)
    prohibited = manifest.oauth_scope != "mail:imap_ro" or manifest.transport_host_profile != "yandex-imap-tls993:v1" or manifest.port != 993 or not manifest.tls_certificate_validated or manifest.auth_mechanism != "XOAUTH2" or not set(manifest.commands) <= ALLOWED_COMMANDS or any(any(token in command.lower() for token in DENIED_TOKENS) for command in manifest.commands)
    add("read_only_transport", Decision.DENY if prohibited else Decision.VERIFIED, "policy_violation" if prohibited else "read_only_tls_xoauth2")
    retention_evidence = manifest.evidence.retention
    retention_ok = REQUIRED_STORES <= set(manifest.retention_classes) and retention_evidence is not None and REQUIRED_STORES <= set(retention_evidence.artifact_classes)
    add("retention_cleanup", Decision.NOT_APPROVED if not retention_ok else Decision.VERIFIED, "coverage_missing" if not retention_ok else "coverage_complete", retention_evidence.reference if retention_evidence else None)
    add("ops_profile", Decision.NOT_APPROVED if manifest.ops_profile != "email-sync-ops:v1" or manifest.ops_limits != PINNED_OPS_PROFILE or not manifest.operator_ref or not manifest.backup_operator_ref else Decision.VERIFIED, "profile_or_operator_missing" if manifest.ops_profile != "email-sync-ops:v1" or manifest.ops_limits != PINNED_OPS_PROFILE else "profile_pinned")
    auth_ok = bool(auth and auth.decision == "APPROVED" and auth.approver_role == "Project Owner" and auth.approved_operation_profile == "yandex-mail-isolated-qualification:v1" and auth.manifest_digest == digest and auth.workspace_id == current.workspace_id and auth.environment_id == current.environment_id and auth.connection_id == current.connection_id and auth.connection_version_id == current.connection_version_id and auth.configuration_generation == current.configuration_generation and auth.authorization_generation == current.authorization_generation and auth.approved_window_start <= now <= auth.approved_window_end and manifest.window_start <= now <= manifest.window_end)
    add("one_time_authorization", Decision.VERIFIED if auth_ok else Decision.NOT_APPROVED, "authorization_bound" if auth_ok else "authorization_missing_or_stale", auth.authorization_ref if auth else None)
    return PreflightReport(decision=result, manifest_digest=digest, authorization_ref=auth.authorization_ref if auth else None, provider_call_permitted=result == Decision.VERIFIED, criteria=tuple(rows))


async def run_yandex_mail_qualification(report: PreflightReport, manifest: YandexMailQualificationManifest, authorization: ExecutionAuthorization, gate: AuthorizationGate, provider: FakeYandexMailPort) -> object:
    """Call only the injected offline fake after a VERIFIED preflight."""
    current = await gate.current_binding(manifest)
    fresh_report = evaluate_yandex_mail_preflight(manifest, authorization, current)
    digest = manifest_digest(manifest)
    if report.decision != Decision.VERIFIED or not report.provider_call_permitted or report.manifest_digest != digest or fresh_report.decision != Decision.VERIFIED:
        raise PermissionError("Yandex Mail qualification preflight denied")
    if not await gate.consume(authorization.authorization_ref, digest, at=current.evaluated_at):
        raise PermissionError("Yandex Mail authorization is stale or already consumed")
    return await provider.qualify(manifest)
