"""Traceability gate for the synthetic Gmail/Yandex Mail qualification."""

from __future__ import annotations

import ast
import hashlib
import json
import re
from pathlib import Path

import pytest

from spine.connectors.gmail import GmailRawMessageResult, GmailRawMessageStatus
from spine.connectors.gmail_observation import gmail_raw_message_observation
from spine.connectors.yandex_mail_message import (
    YandexMailRawMessageFetchCode,
    YandexMailRawMessageFetchResult,
)
from spine.connectors.yandex_mail_observation import yandex_mail_raw_message_observation
from spine.domain.connections.email_scope import EmailProviderOperation, MailboxReadScope
from spine.ingestion.email_mime import MimeParser, MimeParserProfile
from tests.qualification.canonical_ingestion_harness import (
    build_canonical_ingestion_harness,
    ingestion_command,
)
from tests.qualification.provider_adapter_harness import (
    ENVIRONMENT_ID,
    NOW,
    WORKSPACE_ID,
    stable_id,
)


ROOT = Path(__file__).resolve().parents[2]
REPORT_PATH = ROOT / "docs/qualification/issue-863-cross-provider-email.json"
RUNBOOK_PATH = ROOT / "docs/runbooks/r2-cross-provider-email-qualification.md"

SCENARIO_EVIDENCE: dict[str, tuple[str, ...]] = {
    "provider_identity_and_shared_canonical_mime_attachment_retrieval": (
        "tests/qualification/test_issue863_cross_provider_email_qualification.py::test_equivalent_synthetic_messages_share_canonical_contracts_with_provider_specific_identity",
        "tests/qualification/test_issue827_email_canonical_ingestion.py::test_synthetic_email_observation_reaches_canonical_acceptance_and_outbox",
        "tests/qualification/test_issue848_imap_canonical_observation.py::test_qualified_imap_message_reaches_canonical_acceptance_and_replays",
        "tests/qualification/test_issue848_imap_canonical_observation.py::test_copied_message_with_same_message_id_and_digest_remains_distinct",
        "tests/connectors/test_gmail.py::test_raw_message_returns_bytes_digest_and_provider_revision_evidence",
        "tests/ingestion/test_email_mime_parser.py::test_encoded_headers_and_multipart_alternative_produce_stable_part_tree",
        "tests/application/test_email_attachments.py::test_authorized_attachment_is_stored_with_exact_parent_part_lineage",
        "tests/application/test_email_message_citations.py::test_exact_email_body_range_resolves_from_pinned_artifact",
        "tests/application/test_email_attachment_citations.py::test_exact_attachment_lineage_resolves_authorized_parsed_range",
    ),
    "duplicate_metadata_only_change_and_source_revision": (
        "tests/application/test_gmail_message_state.py::test_duplicate_and_out_of_order_label_observations_converge",
        "tests/qualification/test_issue840_gmail_message_state.py::test_metadata_only_state_and_relationships_preserve_raw_revision",
        "tests/qualification/test_issue848_imap_canonical_observation.py::test_changed_bytes_under_immutable_imap_key_are_quarantined",
        "tests/application/test_email_sync.py::test_duplicate_page_and_effect_replay_return_stable_receipts",
    ),
    "stale_cursor_reset_and_generation_fencing": (
        "tests/application/test_gmail_history_cursor_recovery.py::test_history_404_preserves_checkpoint_and_requires_reconciliation",
        "tests/application/test_gmail_history_cursor_recovery.py::test_recovery_starts_one_idempotent_bounded_successor_from_old_checkpoint",
        "tests/application/test_yandex_mail_uidvalidity_reset.py::test_changed_uidvalidity_fences_old_attempt_and_starts_one_new_generation",
        "tests/application/test_yandex_mail_uidvalidity_reset.py::test_reset_before_stale_page_rejects_effect_and_cursor",
    ),
    "partial_crash_retry_and_idempotent_effects": (
        "tests/application/test_gmail_initial_snapshot.py::test_crash_after_durable_boundary_replays_without_cursor_gap",
        "tests/application/test_gmail_initial_snapshot.py::test_unsafe_or_incomplete_coverage_requires_reconciliation_without_cursor",
        "tests/connectors/test_yandex_mail_incremental.py::test_effect_crash_holds_checkpoint_and_reconnect_replays_same_page",
        "tests/connectors/test_yandex_mail_incremental.py::test_partial_batch_and_provider_outage_hold_until_safe_reconnect",
        "tests/application/test_email_attachments.py::test_attachment_replay_has_one_logical_child_effect",
    ),
    "provider_outage_malformed_and_oversized_content": (
        "tests/connectors/test_gmail_history.py::test_provider_errors_return_typed_safe_history_outcomes",
        "tests/connectors/test_yandex_mail.py::test_raw_message_malformed_provider_response_is_typed_and_safe",
        "tests/connectors/test_yandex_mail_message_fetch.py::test_declared_oversize_stops_before_first_literal_chunk",
        "tests/connectors/test_yandex_mail_message_fetch.py::test_truncated_literal_returns_typed_safe_incomplete_outcome",
        "tests/application/test_email_artifacts.py::test_malformed_mime_is_recorded_as_a_bounded_defective_artifact",
        "tests/application/test_email_attachments.py::test_oversized_attachment_fails_without_a_persisted_or_retrievable_child",
    ),
    "revocation_cross_tenant_denial_and_safe_diagnostics": (
        "tests/application/connections/test_email_read_admission.py::test_stale_or_revoked_scope_is_denied_before_provider_call",
        "tests/connectors/test_gmail.py::test_inactive_or_mismatched_connection_never_reaches_gmail",
        "tests/connectors/test_yandex_mail.py::test_wrong_mailbox_or_folder_is_denied_before_imap",
        "tests/qualification/test_issue848_imap_canonical_observation.py::test_wrong_tenant_or_generation_is_denied_before_provider_read",
        "tests/application/test_email_message_citations.py::test_email_citation_inspection_revalidates_current_authority_before_resolution",
        "tests/application/test_email_attachment_citations.py::test_revoked_parent_or_child_denies_attachment_without_excerpt",
        "tests/qualification/test_issue853_yandex_mail_end_to_end.py::test_issue853_scripted_path_runs_admission_sync_ingestion_and_safe_diagnostics",
    ),
    "postgresql_transaction_checkpoint_and_restart_durability": (
        "tests/integration/postgresql/test_email_sync_durability.py::test_postgresql_email_sync_replays_durable_effect_receipt_after_restart",
        "tests/integration/postgresql/test_email_sync_durability.py::test_postgresql_yandex_snapshot_coordinates_two_empty_folders_and_replays",
        "tests/integration/postgresql/test_email_sync_durability.py::test_postgresql_gmail_snapshot_crash_replays_every_durable_boundary",
        "tests/integration/postgresql/test_email_sync_durability.py::test_postgresql_gmail_crash_after_canonical_acceptance_has_one_effect",
        "tests/integration/postgresql/test_email_sync_durability.py::test_postgresql_email_sync_rolls_back_checkpoint_without_canonical_receipts",
    ),
    "provider_neutral_and_repository_compatibility": (
        "tests/qualification/test_issue614_google_offline_qualification.py::test_issue614_traceability_matrix_covers_required_scenarios",
        "tests/qualification/test_issue677_cross_provider_offline.py::test_issue677_matrix_points_to_existing_executable_evidence",
        "tests/application/test_connector_ingestion.py::test_google_v1_and_provider_v2_produce_equivalent_canonical_outcomes",
        "tests/test_architecture_scaffold.py::test_domain_layer_has_no_framework_imports",
    ),
}

FORBIDDEN_REPORT_PATTERNS = (
    r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
    r"(?i)\b(?:access[_ -]?token|refresh[_ -]?token|password)\s*[:=]",
    r"(?i)\"(?:subject|folder_name|raw_message|provider_body)\"\s*:",
)


def _test_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    }


def _mailbox_scope(provider: str) -> MailboxReadScope:
    connection_id = stable_id(f"issue863-{provider}-connection")
    return MailboxReadScope(
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        connection_id=connection_id,
        connection_version_id=stable_id(f"issue863-{provider}-connection-version"),
        provider_kind=provider,
        resource_scope_reference=stable_id(f"issue863-{provider}-scope"),
        mailbox_subject_reference=stable_id(f"issue863-{provider}-mailbox"),
        mailbox_binding_id=stable_id(f"issue863-{provider}-binding"),
        authorization_generation=1,
        configuration_generation=1,
        scope_generation=1,
        allowed_label_references=(stable_id("issue863-gmail-label"),) if provider == "google" else (),
        allowed_folder_references=(stable_id("issue863-yandex-folder"),) if provider == "yandex_mail" else (),
        allowed_operations=(EmailProviderOperation.READ_RAW_MESSAGE,),
    )


@pytest.mark.asyncio
async def test_equivalent_synthetic_messages_share_canonical_contracts_with_provider_specific_identity() -> None:
    raw_mime = (
        b"From: sender@example.test\r\n"
        b"To: recipient@example.test\r\n"
        b"Subject: Synthetic qualification only\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: multipart/mixed; boundary=issue863-boundary\r\n\r\n"
        b"--issue863-boundary\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
        b"Synthetic body.\r\n"
        b"--issue863-boundary\r\n"
        b"Content-Type: application/octet-stream\r\n"
        b"Content-Disposition: attachment; filename=synthetic.bin\r\n"
        b"Content-Transfer-Encoding: base64\r\n\r\n"
        b"AQIDBA==\r\n"
        b"--issue863-boundary--\r\n"
    )
    digest = hashlib.sha256(raw_mime).hexdigest()
    gmail_scope = _mailbox_scope("google")
    label = gmail_scope.allowed_label_references[0]
    gmail = gmail_raw_message_observation(
        GmailRawMessageResult(
            status=GmailRawMessageStatus.ACCEPTED,
            mailbox_scope=gmail_scope,
            requested_message_id="synthetic-gmail-message",
            authorized_label_reference=label,
            message_id="synthetic-gmail-message",
            raw_mime=raw_mime,
            sha256=digest,
            byte_length=len(raw_mime),
            observed_label_references=(label,),
            internal_date_ms=int(NOW.timestamp() * 1000),
            history_id="12345",
            thread_id="synthetic-thread",
        ),
        connection_version_id=gmail_scope.connection_version_id,
        received_at=NOW,
    )

    yandex_scope = _mailbox_scope("yandex_mail")
    folder = yandex_scope.allowed_folder_references[0]
    yandex = yandex_mail_raw_message_observation(
        scope=yandex_scope,
        result=YandexMailRawMessageFetchResult(
            code=YandexMailRawMessageFetchCode.COMPLETE,
            complete=True,
            workspace_id=yandex_scope.workspace_id,
            environment_id=yandex_scope.environment_id,
            connection_id=yandex_scope.connection_id,
            connection_version_id=yandex_scope.connection_version_id,
            mailbox_binding_id=yandex_scope.mailbox_binding_id,
            folder_reference=folder,
            authorization_generation=yandex_scope.authorization_generation,
            configuration_generation=yandex_scope.configuration_generation,
            scope_generation=yandex_scope.scope_generation,
            uid_validity=101,
            uid=42,
            raw_bytes=raw_mime,
            content_length=len(raw_mime),
            content_sha256=digest,
            declared_octets=len(raw_mime),
            envelope=b"synthetic envelope",
            internal_date=NOW,
            fetch_data_item="BODY.PEEK[]",
            seen_preserved=True,
            mutation_evidence_complete=True,
        ),
    )

    gmail_representation = gmail.representation
    yandex_representation = yandex.representation
    assert gmail.external_identity.external_object_id != yandex.external_identity.external_object_id
    assert gmail.external_identity.external_namespace != yandex.external_identity.external_namespace
    assert gmail_representation.original_bytes == yandex_representation.original_bytes == raw_mime
    assert gmail_representation.media_type == yandex_representation.media_type == "message/rfc822"
    assert gmail_representation.extraction_profile == yandex_representation.extraction_profile == "raw_email_mime:v1"
    assert gmail_representation.locator_contract_version == yandex_representation.locator_contract_version == "email_message:v1"

    parsed = MimeParser(
        MimeParserProfile(
            max_total_bytes=4096,
            max_header_bytes=1024,
            max_parts=8,
            max_depth=4,
            max_decoded_bytes=4096,
            max_parse_seconds=1.0,
        )
    ).parse_with_attachments(raw_mime)
    assert len(parsed.attachments) == 1
    assert parsed.attachments[0].content == b"\x01\x02\x03\x04"

    harness = await build_canonical_ingestion_harness()
    gmail_result = await harness.orchestrator.ingest_observation(
        ingestion_command(gmail, identity="issue863-gmail"), gmail
    )
    yandex_result = await harness.orchestrator.ingest_observation(
        ingestion_command(yandex, identity="issue863-yandex"), yandex
    )

    assert gmail_result.source.source_object_id != yandex_result.source.source_object_id
    assert gmail_result.revision.original_sha256 == yandex_result.revision.original_sha256 == digest
    assert gmail_result.revision.byte_length == yandex_result.revision.byte_length == len(raw_mime)
    assert gmail_result.revision.media_type == yandex_result.revision.media_type == "message/rfc822"
    assert harness.uploader.objects[gmail_result.revision.original_reference.object_id] == raw_mime
    assert harness.uploader.objects[yandex_result.revision.original_reference.object_id] == raw_mime
    assert harness.projection_intent_count == 2


def test_issue863_matrix_covers_required_cross_provider_scenarios() -> None:
    report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))

    assert report["schema_version"] == "issue-863-cross-provider-email:v1"
    assert report["ticket"] == 863
    assert report["offline_result"] == "PASS"
    assert report["live_provider_semantics"] == "NOT_VERIFIED"
    assert report["scenario_status"] == {
        scenario: "PASS" for scenario in SCENARIO_EVIDENCE
    }
    assert report["scenario_traceability"] == {
        scenario: list(nodes) for scenario, nodes in SCENARIO_EVIDENCE.items()
    }
    for node_id in (node for nodes in SCENARIO_EVIDENCE.values() for node in nodes):
        path, test_name = node_id.split("::", 1)
        assert test_name in _test_names(ROOT / path), node_id


def test_issue863_report_pins_synthetic_inputs_and_safe_diagnostics() -> None:
    report_text = REPORT_PATH.read_text(encoding="utf-8")
    report = json.loads(report_text)

    assert report["fixture_manifest_version"] == "spine-cross-provider-email-fixtures:v1"
    assert report["fixture_policy"] == {
        "synthetic_only": True,
        "network_access": False,
        "provider_credentials": False,
        "real_provider_payloads": False,
        "production_database": False,
    }
    assert report["privacy"] == {
        "tokens_recorded": False,
        "mailbox_addresses_recorded": False,
        "subjects_recorded": False,
        "folder_names_recorded": False,
        "raw_mime_recorded": False,
        "attachment_names_or_contents_recorded": False,
        "provider_error_text_recorded": False,
    }
    assert report["compatibility_gates"] == {
        "google_drive_and_sheets": "PASS",
        "yandex_disk": "PASS",
        "provider_neutral_v2": "PASS",
        "domain_boundary": "PASS",
        "generated_api": "PASS",
    }
    assert all(re.search(pattern, report_text) is None for pattern in FORBIDDEN_REPORT_PATTERNS)


def test_issue863_runbook_names_postgresql_and_compatibility_gates() -> None:
    runbook = RUNBOOK_PATH.read_text(encoding="utf-8")
    command = runbook.split("```bash\n", 1)[1].split("\n```", 1)[0]

    assert "tests/integration/postgresql/test_email_sync_durability.py" in command
    assert "tests/qualification/test_issue614_google_offline_qualification.py" in command
    assert "tests/qualification/test_issue677_cross_provider_offline.py" in command
    assert "tests/application/test_connector_ingestion.py" in command
    assert "tests/test_architecture_scaffold.py" in command
    assert "pnpm web:api:check" in runbook
    assert "no network or Gmail/Yandex credentials" in runbook
    assert "NOT_VERIFIED" in runbook
