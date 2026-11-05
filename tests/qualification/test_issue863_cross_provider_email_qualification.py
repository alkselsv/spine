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
from spine.domain.artifacts.email_html import EmailHtmlSanitizerProfile
from spine.domain.artifacts.email_attachment import (
    EmailAttachmentArtifact,
    EmailAttachmentProcessingReceipt,
    email_attachment_id,
    email_attachment_artifact_digest,
    email_attachment_processing_receipt_digest,
    encode_email_attachment_locator,
)
from spine.domain.artifacts.email_message import (
    EmailMessageArtifact,
    email_message_artifact_digest,
)
from spine.ingestion.email_mime import MimeParser, MimeParserProfile
from spine.ingestion.email_html import EmailHtmlSanitizer
from spine.application.citation_inspector import CitationInspectorRequester
from spine.application.email_citations import (
    EmailAttachmentCitationExcerptResolver,
    build_email_attachment_citation_locator,
    build_email_message_citation_locator,
)
from spine.application.projection_locator import canonical_projection_locator_digest
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
from tests.application.test_citation_inspector import CurrentState, SUBJECT, context
from tests.application.test_email_message_citations import _inspector_fixture
from tests.application.test_email_attachment_citations import (
    RAW as ATTACHMENT_CITATION_RAW,
    _ParsedReader,
    _Reader,
    _ReceiptReader,
    _fixture as attachment_citation_fixture,
)
from spine.domain.context_bundles import EvidenceLocator
from spine.memory.contracts import StableLocator


ROOT = Path(__file__).resolve().parents[2]
REPORT_PATH = ROOT / "docs/qualification/issue-863-cross-provider-email.json"
FIXTURE_MANIFEST_PATH = ROOT / "docs/qualification/issue-863-email-fixture-manifest.json"
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
    (
        _unused,
        base_citation,
        _base_locator,
        base_parent,
        base_attachment,
        base_receipt,
        parsed_attachment,
        _base_bundle,
    ) = attachment_citation_fixture()
    raw_mime = ATTACHMENT_CITATION_RAW
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
    assert parsed.attachments[0].content == b"Attachment evidence"

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

    parser = MimeParser(
        MimeParserProfile(
            max_total_bytes=4096,
            max_header_bytes=1024,
            max_parts=8,
            max_depth=4,
            max_decoded_bytes=4096,
            max_parse_seconds=1.0,
        )
    )
    mime = parser.parse(raw_mime)
    sanitizer = EmailHtmlSanitizer(
        EmailHtmlSanitizerProfile(
            max_total_input_bytes=4096,
            max_total_output_bytes=4096,
            max_parts=8,
            max_depth=4,
        )
    )

    def body_part(part):
        if (
            part.media_type == "text/plain"
            and part.content_disposition != "attachment"
            and part.text
        ):
            return part
        return next(
            (match for child in part.children if (match := body_part(child)) is not None),
            None,
        )

    selected_part = body_part(mime.root)
    assert selected_part is not None
    assert selected_part.text is not None
    excerpts: list[str] = []
    for provider, result in (("gmail", gmail_result), ("yandex", yandex_result)):
        provisional_artifact = EmailMessageArtifact.model_construct(
            artifact_id=stable_id(f"issue863-{provider}-message-artifact"),
            workspace_id=result.source.workspace_id,
            environment_id=result.source.environment_id,
            source_object_id=result.source.source_object_id,
            source_revision_id=result.revision.revision_id,
            canonical_acceptance_id=result.canonical_acceptance.acceptance.canonical_acceptance_id,
            source_revision_digest=result.revision.revision_digest,
            raw_sha256=digest,
            raw_byte_length=len(raw_mime),
            parser_profile=parser.profile.reference,
            sanitizer_profile=sanitizer.reference,
            mime=mime,
            sanitized_html=sanitizer.sanitize_all(mime),
            selected_representation="plain_text",
            selected_text=selected_part.text,
            artifact_sha256="0" * 64,
        )
        artifact = EmailMessageArtifact.model_validate(
            provisional_artifact.model_copy(
                update={"artifact_sha256": email_message_artifact_digest(provisional_artifact)}
            ).model_dump()
        )
        locator = build_email_message_citation_locator(
            artifact,
            part_path=selected_part.part_path,
            representation="plain_text",
            start_codepoint=0,
            end_codepoint=len(selected_part.text),
        )
        inspector, citation = _inspector_fixture(
            artifact,
            locator,
            state=CurrentState(),
        )
        inspected = await inspector.inspect(
            context=context(),
            requester=CitationInspectorRequester(SUBJECT),
            citation_id=citation.citation_id,
        )
        assert inspected.evidence is not None
        excerpts.append(inspected.evidence.excerpt)

    assert excerpts == [selected_part.text, selected_part.text]

    attachment_excerpts: list[str] = []
    for provider, result in (("gmail", gmail_result), ("yandex", yandex_result)):
        acceptance = result.canonical_acceptance.acceptance
        provisional_parent = base_parent.model_copy(
            update={
                "source_object_id": result.source.source_object_id,
                "source_revision_id": result.revision.revision_id,
                "source_revision_digest": result.revision.revision_digest,
                "canonical_acceptance_id": acceptance.canonical_acceptance_id,
                "artifact_sha256": "0" * 64,
            }
        )
        parent = EmailMessageArtifact.model_validate(
            provisional_parent.model_copy(
                update={"artifact_sha256": email_message_artifact_digest(provisional_parent)}
            ).model_dump()
        )
        provisional_attachment = base_attachment.model_copy(
            update={
                "artifact_id": email_attachment_id(
                    workspace_id=parent.workspace_id,
                    environment_id=parent.environment_id,
                    source_revision_id=parent.source_revision_id,
                    mime_part_path=base_attachment.mime_part_path,
                    extraction_profile=base_attachment.extraction_profile,
                ),
                "source_object_id": parent.source_object_id,
                "source_revision_id": parent.source_revision_id,
                "source_revision_digest": parent.source_revision_digest,
                "canonical_acceptance_id": parent.canonical_acceptance_id,
                "artifact_sha256": "0" * 64,
            }
        )
        attachment = EmailAttachmentArtifact.model_validate(
            provisional_attachment.model_copy(
                update={
                    "artifact_sha256": email_attachment_artifact_digest(
                        provisional_attachment
                    )
                }
            ).model_dump()
        )
        provisional_receipt = base_receipt.model_copy(
            update={
                "processing_receipt_id": stable_id(f"issue863-{provider}-attachment-receipt"),
                "parent_source_object_id": parent.source_object_id,
                "parent_source_revision_id": parent.source_revision_id,
                "parent_source_revision_digest": parent.source_revision_digest,
                "parent_canonical_acceptance_id": parent.canonical_acceptance_id,
                "attachment_artifact_id": attachment.artifact_id,
                "attachment_artifact_digest": attachment.artifact_sha256,
                "receipt_sha256": "0" * 64,
            }
        )
        receipt = EmailAttachmentProcessingReceipt.model_validate(
            provisional_receipt.model_copy(
                update={
                    "receipt_sha256": email_attachment_processing_receipt_digest(
                        provisional_receipt
                    )
                }
            ).model_dump()
        )
        attachment_locator = build_email_attachment_citation_locator(
            parent=parent,
            attachment=attachment,
            receipt=receipt,
            parsed_artifact=parsed_attachment,
            child_locator=StableLocator(kind="character_range", value="line:1:0-19"),
        )
        attachment_reference = encode_email_attachment_locator(attachment_locator)
        stable_attachment_locator = StableLocator(
            kind="custom", value=attachment_reference
        )
        evidence_reference = base_citation.evidence_reference.model_copy(
            update={
                "processing_receipt_id": receipt.processing_receipt_id,
                "locator_contract_version": attachment_locator.schema_version,
                "locator": EvidenceLocator(
                    kind="custom", value=attachment_reference
                ),
                "locator_digest": canonical_projection_locator_digest(
                    locator_contract_version=attachment_locator.schema_version,
                    locator=stable_attachment_locator,
                ),
            }
        )
        provider_citation = base_citation.model_copy(
            update={"evidence_reference": evidence_reference}
        )
        resolver = EmailAttachmentCitationExcerptResolver(
            messages=_Reader(parent, "artifact_id"),
            attachments=_Reader(attachment, "artifact_id"),
            processing=_ReceiptReader(receipt),
            parsed_artifacts=_ParsedReader(parsed_attachment),
        )
        resolved = await resolver.resolve(provider_citation, historical=False)
        assert resolved is not None
        assert resolver.dependency_source_identities(provider_citation) == (
            (result.source.source_object_id, result.revision.revision_id),
            (
                parsed_attachment.source_object_id,
                parsed_attachment.source_revision_id,
            ),
        )
        attachment_excerpts.append(resolved.excerpt)

    assert attachment_excerpts == ["Attachment evidence", "Attachment evidence"]


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
    manifest_text = FIXTURE_MANIFEST_PATH.read_text(encoding="utf-8")
    manifest = json.loads(manifest_text)

    assert report["fixture_manifest_version"] == "spine-cross-provider-email-fixtures:v1"
    assert report["fixture_manifest_path"] == str(FIXTURE_MANIFEST_PATH.relative_to(ROOT))
    assert manifest["fixture_manifest_version"] == report["fixture_manifest_version"]
    assert manifest["schema_version"] == "issue-863-email-fixture-manifest:v1"
    assert manifest["synthetic_only"] is True
    assert manifest["network_access"] is False
    assert manifest["provider_credentials"] is False
    for descriptor in manifest["fixture_descriptors"]:
        path, test_name = descriptor["source_test"].split("::", 1)
        assert test_name in _test_names(ROOT / path), descriptor["fixture_id"]
    assert manifest["privacy"] == {
        "payloads_embedded": False,
        "addresses_recorded": False,
        "subjects_recorded": False,
        "folder_names_recorded": False,
        "filenames_recorded": False,
        "credentials_recorded": False,
    }
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
    assert report["gate_results"]["focused_email_and_postgresql_matrix"] == {
        "status": "PASS",
        "result": "497 passed",
        "command": "Run command in docs/runbooks/r2-cross-provider-email-qualification.md",
    }
    assert report["gate_results"]["generated_api_contract"]["status"] == "PASS"
    assert report["gate_results"]["compileall"]["status"] == "PASS"
    assert report["gate_results"]["full_repository_pytest"] == {
        "status": "NOT_VERIFIED",
        "result": "7087 passed, 7 skipped, 6 failed because ffprobe is missing",
        "missing_prerequisite": "ffprobe",
        "command": "uv run pytest -q",
    }
    assert report["gate_results"]["pull_request_ci"]["status"] == "NOT_VERIFIED"
    assert all(re.search(pattern, report_text) is None for pattern in FORBIDDEN_REPORT_PATTERNS)
    assert all(re.search(pattern, manifest_text) is None for pattern in FORBIDDEN_REPORT_PATTERNS)


def test_issue863_runbook_names_postgresql_and_compatibility_gates() -> None:
    runbook = RUNBOOK_PATH.read_text(encoding="utf-8")
    command = runbook.split("```bash\n", 1)[1].split("\n```", 1)[0]

    assert "tests/integration/postgresql/test_email_sync_durability.py" in command
    assert "tests/qualification/test_issue614_google_offline_qualification.py" in command
    assert "tests/qualification/test_issue677_cross_provider_offline.py" in command
    assert "tests/application/test_connector_ingestion.py" in command
    assert "tests/test_architecture_scaffold.py" in command
    assert "pnpm web:api:check" in runbook
    assert "docs/qualification/issue-863-email-fixture-manifest.json" in runbook
    assert "no network or Gmail/Yandex credentials" in runbook
    assert "NOT_VERIFIED" in runbook
