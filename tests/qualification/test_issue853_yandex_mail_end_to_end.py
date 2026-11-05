"""Traceability and safe-report checks for issue #853 offline qualification."""

from __future__ import annotations

import json
from datetime import UTC, datetime
import hashlib
import re
from pathlib import Path
from uuid import UUID, uuid5, NAMESPACE_URL

import pytest

from spine.application.email_sync import (
    EmailSyncAdmission,
    EmailSyncAdmissionState,
    EmailSyncCoordinator,
    EmailSyncEffect,
    EmailSyncEffectReceipt,
    EmailSyncStatus,
    InMemoryEmailSyncRepository,
)
from spine.application.yandex_mail_reconciliation import (
    YandexMailReconciliationCommand,
    YandexMailReconciliationCoordinator,
    YandexMailReconciliationMessage,
)
from spine.application.yandex_mail_snapshot import (
    YandexMailInitialSnapshotCommand,
    YandexMailInitialSnapshotCoordinator,
    YandexMailSnapshotFolderCode,
    YandexMailSnapshotFolderDiscovery,
    YandexMailSnapshotFolderGeneration,
    YandexMailSnapshotGenerationCode,
    YandexMailSnapshotMessageMetadata,
    YandexMailSnapshotUidPage,
    YandexMailSnapshotUidPageCode,
    YandexMailSnapshotUidPageRequest,
)
from spine.connectors.yandex_mail_message import (
    YandexMailRawMessageFetchCode,
    YandexMailRawMessageFetchResult,
)
from spine.connectors.yandex_mail_snapshot import YandexMailCanonicalSnapshotMessageAdapter
from spine.domain.connections.email_scope import (
    EmailProviderOperation,
    MailboxReadScope,
)
from spine.domain.sources.email_lifecycle import EmailLifecycleCoverage
from spine.domain.sources.imap import ImapMessageKey
from tests.qualification.canonical_ingestion_harness import (
    POLICY_ID,
    build_canonical_ingestion_harness,
)

ROOT = Path(__file__).resolve().parents[2]
REPORT_PATH = ROOT / "docs/qualification/issue-853-yandex-mail-end-to-end.json"
WORKSPACE_ID = UUID(int=1)
ENVIRONMENT_ID = UUID(int=2)
CONNECTION_ID = UUID(int=3)
CONNECTION_VERSION_ID = UUID(int=4)
FOLDER_ID = UUID(int=8)
_RAW_MIME = (
    b"From: synthetic-sender@example.test\r\n"
    b"To: synthetic-recipient@example.test\r\n"
    b"Subject: Synthetic end-to-end qualification\r\n"
    b"Message-ID: <issue853@example.test>\r\n\r\n"
    b"Synthetic lifecycle diagnostic fixture.\r\n"
)
CRITERIA = {
    "tls_xoauth2_composition",
    "approved_folder_discovery_and_uid_identity",
    "bounded_raw_fetch_and_canonical_ingestion",
    "multi_folder_snapshot_and_replay",
    "incremental_polling_and_crash_replay",
    "uidvalidity_reset_and_generation_fencing",
    "moves_absence_and_lifecycle_replay",
    "tenant_authority_and_partial_coverage_fail_closed",
    "safe_diagnostics_and_provider_neutral_regressions",
    "scripted_end_to_end_admission_sync_canonical_and_diagnostics",
}


def _report() -> dict[str, object]:
    return json.loads(REPORT_PATH.read_text(encoding="utf-8"))


def _scope() -> MailboxReadScope:
    return MailboxReadScope(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        connection_id=CONNECTION_ID,
        connection_version_id=CONNECTION_VERSION_ID,
        provider_kind="yandex_mail",
        resource_scope_reference=UUID(int=5),
        mailbox_subject_reference=UUID(int=6),
        mailbox_binding_id=UUID(int=7),
        authorization_generation=3,
        configuration_generation=4,
        scope_generation=5,
        allowed_folder_references=(FOLDER_ID,),
        allowed_operations=tuple(EmailProviderOperation),
    )


class _Admission:
    def __init__(self) -> None:
        self.checks = 0

    async def check(self, admission: EmailSyncAdmission) -> EmailSyncAdmissionState:
        assert admission.workspace_id == WORKSPACE_ID
        self.checks += 1
        return EmailSyncAdmissionState.ALLOWED


class _UnusedEffects:
    async def apply(self, effect: EmailSyncEffect) -> EmailSyncEffectReceipt:
        del effect
        raise AssertionError("canonical acceptance must provide durable receipts")


class _ScriptedSnapshotReader:
    async def discover_folders(
        self, scope: MailboxReadScope
    ) -> YandexMailSnapshotFolderDiscovery:
        return YandexMailSnapshotFolderDiscovery(
            workspace_id=scope.workspace_id,
            environment_id=scope.environment_id,
            complete=True,
            code=YandexMailSnapshotFolderCode.COMPLETE,
            discovered_count=1,
            approved_count=1,
            folders=(FOLDER_ID,),
        )

    async def observe_folder_generation(
        self, scope: MailboxReadScope, folder_reference: UUID
    ) -> YandexMailSnapshotFolderGeneration:
        return YandexMailSnapshotFolderGeneration(
            complete=True,
            code=YandexMailSnapshotGenerationCode.COMPLETE,
            workspace_id=scope.workspace_id,
            environment_id=scope.environment_id,
            connection_id=scope.connection_id,
            connection_version_id=scope.connection_version_id,
            mailbox_binding_id=scope.mailbox_binding_id,
            folder_reference=folder_reference,
            authorization_generation=scope.authorization_generation,
            configuration_generation=scope.configuration_generation,
            scope_generation=scope.scope_generation,
            uid_validity=101,
            uid_next=43,
            read_only=True,
        )

    async def enumerate_uid_page(
        self, request: YandexMailSnapshotUidPageRequest
    ) -> YandexMailSnapshotUidPage:
        scope = request.scope
        return YandexMailSnapshotUidPage(
            complete=True,
            code=YandexMailSnapshotUidPageCode.COMPLETE,
            workspace_id=scope.workspace_id,
            environment_id=scope.environment_id,
            connection_id=scope.connection_id,
            connection_version_id=scope.connection_version_id,
            mailbox_binding_id=scope.mailbox_binding_id,
            folder_reference=request.folder_reference,
            authorization_generation=scope.authorization_generation,
            configuration_generation=scope.configuration_generation,
            scope_generation=scope.scope_generation,
            uid_validity=request.uid_validity,
            covered_uid_range=(request.start_uid, request.end_uid),
            messages=(YandexMailSnapshotMessageMetadata(uid=42, size_octets=len(_RAW_MIME)),),
        )


class _ScriptedRawMessageAdmission:
    def __init__(self, scope: MailboxReadScope) -> None:
        self.scope = scope
        self.calls = 0

    async def read(self, command: object, context: object) -> YandexMailRawMessageFetchResult:
        del context
        self.calls += 1
        scope = self.scope
        assert command.scope.workspace_id == scope.workspace_id
        return YandexMailRawMessageFetchResult(
            code=YandexMailRawMessageFetchCode.COMPLETE,
            complete=True,
            workspace_id=scope.workspace_id,
            environment_id=scope.environment_id,
            connection_id=scope.connection_id,
            connection_version_id=scope.connection_version_id,
            mailbox_binding_id=scope.mailbox_binding_id,
            folder_reference=FOLDER_ID,
            authorization_generation=scope.authorization_generation,
            configuration_generation=scope.configuration_generation,
            scope_generation=scope.scope_generation,
            uid_validity=101,
            uid=42,
            raw_bytes=_RAW_MIME,
            content_length=len(_RAW_MIME),
            content_sha256=hashlib.sha256(_RAW_MIME).hexdigest(),
            declared_octets=len(_RAW_MIME),
            envelope=b'("Sat, 31 Oct 2026 09:30:00 +0000" "Synthetic")',
            internal_date=datetime(2026, 11, 1, tzinfo=UTC),
            fetch_data_item="BODY.PEEK[]",
            seen_preserved=True,
            mutation_evidence_complete=True,
        )


@pytest.mark.asyncio
async def test_issue853_scripted_path_runs_admission_sync_ingestion_and_safe_diagnostics() -> None:
    scope = _scope()
    admission = _Admission()
    sync = EmailSyncCoordinator(
        repository=InMemoryEmailSyncRepository(),
        admission=admission,
        effects=_UnusedEffects(),
        clock=lambda: datetime(2026, 11, 5, tzinfo=UTC),
    )
    harness = await build_canonical_ingestion_harness()
    raw_messages = _ScriptedRawMessageAdmission(scope)
    canonical_adapter = YandexMailCanonicalSnapshotMessageAdapter(
        ingestion=harness.orchestrator, raw_messages=raw_messages
    )
    snapshot = YandexMailInitialSnapshotCoordinator(
        sync=sync,
        reader=_ScriptedSnapshotReader(),
        messages=canonical_adapter,
    )
    result = await snapshot.run(
        YandexMailInitialSnapshotCommand(
            scope=scope,
            attempt_key="issue853-scripted-e2e",
            uid_page_size=2,
            uid_page_response_bytes=4096,
            max_uid_pages_per_folder=4,
            max_message_bytes=4096,
            access_policy_version_id=POLICY_ID,
        )
    )

    assert result.attempt.status is EmailSyncStatus.COMPLETE, (
        result.attempt.status,
        result.attempt.failure_code,
        result.attempt.pages,
    )
    assert result.attempt.committed_checkpoint is not None
    assert result.durable_message_effects == 1
    assert result.completed_folders == 1
    assert admission.checks > 0
    assert raw_messages.calls == 1
    assert harness.projection_intent_count == 1

    diagnostic_attempt = await sync.create_attempt(
        scope,
        attempt_key="issue853-safe-diagnostic",
    )
    diagnostic = YandexMailReconciliationCommand(
        scope=scope,
        attempt_id=diagnostic_attempt.attempt_id,
        scan_id=uuid5(NAMESPACE_URL, "issue853-safe-diagnostic"),
        scan_completed_at=datetime(2026, 11, 5, tzinfo=UTC),
        coverage=EmailLifecycleCoverage.PROVIDER_OUTAGE,
        previous=(
            YandexMailReconciliationMessage(
                key=ImapMessageKey(
                    workspace_id=scope.workspace_id,
                    connection_id=scope.connection_id,
                    mailbox_binding_id=scope.mailbox_binding_id,
                    folder_binding_id=FOLDER_ID,
                    uid_validity=101,
                    uid=42,
                ),
                source_object_id=uuid5(NAMESPACE_URL, "issue853-source"),
                source_revision_id=uuid5(NAMESPACE_URL, "issue853-revision"),
                content_sha256=hashlib.sha256(_RAW_MIME).hexdigest(),
            ),
        ),
    )
    diagnosed = await YandexMailReconciliationCoordinator(sync).reconcile(diagnostic)

    assert diagnosed.attempt.failure_code == "provider_outage"
    assert diagnosed.receipts
    safe_diagnostics = json.dumps(
        [receipt.model_dump(mode="json") for receipt in diagnosed.receipts],
        sort_keys=True,
    )
    assert "provider_outage" in safe_diagnostics
    assert _RAW_MIME.decode("ascii") not in safe_diagnostics
    assert "synthetic-sender@example.test" not in safe_diagnostics


def test_issue853_report_pins_offline_fixtures_and_versions_each_criterion() -> None:
    report = _report()

    assert report["schema_version"] == "issue-853-yandex-mail-end-to-end-evidence:v1"
    assert report["issue"] == 853
    assert report["offline_result"] == "PASS"
    assert report["live_yandex_mail_semantics"] == "NOT_VERIFIED"
    assert report["fixture_manifest_version"] == "spine-yandex-mail-qualification-fixtures-v1"
    assert "Synthetic" in report["fixture_policy"]
    assert "no Yandex credentials" in report["fixture_policy"]
    versions = report["adapter_contract_versions"]
    assert isinstance(versions, list) and versions
    assert all(isinstance(version, str) and version.endswith(":v1") for version in versions)
    profiles = report["qualification_profiles"]
    assert profiles["folder_discovery"]["version"] == "yandex-mail-folder-discovery-profile:v1"
    assert profiles["initial_snapshot_fixture"]["version"] == "yandex-mail-initial-snapshot-command:v1"
    assert profiles["uidvalidity_reset_fixture"]["version"] == "yandex-mail-uidvalidity-reset-profile:v1"

    criteria = report["criteria"]
    assert isinstance(criteria, dict)
    assert set(criteria) == CRITERIA
    for criterion in criteria.values():
        assert criterion["status"] == "PASS"
        assert criterion["evidence"]


def test_issue853_evidence_references_existing_tests_and_safe_report_has_no_payloads() -> None:
    report = _report()
    criteria = report["criteria"]
    assert isinstance(criteria, dict)
    for criterion in criteria.values():
        for node_id in criterion["evidence"]:
            relative_path, test_name = node_id.split("::", 1)
            source = ROOT / relative_path
            assert source.is_file(), node_id
            assert re.search(rf"\bdef {re.escape(test_name)}\s*\(", source.read_text(encoding="utf-8")), node_id

    serialized = json.dumps(report, sort_keys=True)
    forbidden_patterns = (
        r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
        r"(?i)\b(?:access[_ -]?token|refresh[_ -]?token|password)\s*[:=]",
        r"(?i)\b(?:subject|folder[_ -]?name|raw[_ -]?message|server[_ -]?line)\s*[:=]",
    )
    for pattern in forbidden_patterns:
        assert re.search(pattern, serialized) is None
