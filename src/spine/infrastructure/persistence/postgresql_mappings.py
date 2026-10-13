"""Infrastructure-only SQLAlchemy mappings for canonical tenant records."""

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID


metadata = MetaData(schema="spine")

workspaces = Table(
    "workspaces",
    metadata,
    Column("id", PostgreSQLUUID(as_uuid=True), primary_key=True),
    Column("slug", Text, nullable=False),
    Column("display_name", Text, nullable=False),
)

environments = Table(
    "environments",
    metadata,
    Column("id", PostgreSQLUUID(as_uuid=True), primary_key=True),
    Column("workspace_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("kind", Text, nullable=False),
    Column("display_name", Text, nullable=False),
)

idempotency_receipts = Table(
    "idempotency_receipts",
    metadata,
    Column("receipt_id", PostgreSQLUUID(as_uuid=True), primary_key=True),
    Column("workspace_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("environment_id", PostgreSQLUUID(as_uuid=True), nullable=True),
    Column("operation_name", Text, nullable=False),
    Column("operation_schema_version", Integer, nullable=False),
    Column("idempotency_key", Text, nullable=False),
    Column("digest_algorithm_version", Text, nullable=False),
    Column("command_digest", String(64), nullable=False),
    Column("result_type", Text, nullable=True),
    Column("result_id", PostgreSQLUUID(as_uuid=True), nullable=True),
    Column("result_schema_version", Integer, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

outbox_intents = Table(
    "outbox_intents",
    metadata,
    Column(
        "event_id",
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    ),
    Column("workspace_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("environment_id", PostgreSQLUUID(as_uuid=True), nullable=True),
    Column("event_type", Text, nullable=False),
    Column("event_schema_version", Integer, nullable=False),
    Column("aggregate_type", Text, nullable=True),
    Column("aggregate_id", PostgreSQLUUID(as_uuid=True), nullable=True),
    Column("aggregate_schema_version", Integer, nullable=True),
    Column("producer_deduplication_id", Text, nullable=True),
    Column("payload", JSONB, nullable=False),
    Column("trace_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("correlation_id", PostgreSQLUUID(as_uuid=True), nullable=True),
    Column("causation_id", PostgreSQLUUID(as_uuid=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

outbox_deliveries = Table(
    "outbox_deliveries",
    metadata,
    Column("event_id", PostgreSQLUUID(as_uuid=True), primary_key=True),
    Column("workspace_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("environment_id", PostgreSQLUUID(as_uuid=True), nullable=True),
    Column("consumer_name", Text, nullable=True),
    Column("status", String(32), nullable=False),
    Column("attempt_count", Integer, nullable=False),
    Column("next_attempt_at", DateTime(timezone=True), nullable=True),
    Column("lease_owner", Text, nullable=True),
    Column("lease_token", PostgreSQLUUID(as_uuid=True), nullable=True),
    Column("lease_expires_at", DateTime(timezone=True), nullable=True),
    Column("last_error_code", Text, nullable=True),
    Column("delivered_at", DateTime(timezone=True), nullable=True),
    Column("quarantined_at", DateTime(timezone=True), nullable=True),
    Column("version", Integer, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

audit_events = Table(
    "audit_events",
    metadata,
    Column(
        "audit_event_id",
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    ),
    Column("workspace_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("environment_id", PostgreSQLUUID(as_uuid=True), nullable=True),
    Column("event_type", Text, nullable=False),
    Column("schema_version", Integer, nullable=False),
    Column("origin", String(16), nullable=False),
    Column("acting_subject_id", PostgreSQLUUID(as_uuid=True), nullable=True),
    Column("service_principal_id", PostgreSQLUUID(as_uuid=True), nullable=True),
    Column("trace_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("correlation_id", PostgreSQLUUID(as_uuid=True), nullable=True),
    Column("causation_id", PostgreSQLUUID(as_uuid=True), nullable=True),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("appended_at", DateTime(timezone=True), nullable=False),
    Column("target_type", Text, nullable=True),
    Column("target_id", PostgreSQLUUID(as_uuid=True), nullable=True),
    Column("target_schema_version", Integer, nullable=True),
    Column("outcome", Text, nullable=False),
    Column("reason", Text, nullable=False),
    Column("producer_deduplication_id", Text, nullable=True),
    Column("payload", JSONB, nullable=False),
)

source_objects = Table(
    "source_objects", metadata,
    Column("source_object_id", PostgreSQLUUID(as_uuid=True), primary_key=True),
    Column("workspace_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("environment_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("source_kind", Text, nullable=False),
    Column("identity_mode", Text, nullable=False),
    Column("connection_id", PostgreSQLUUID(as_uuid=True)),
    Column("external_namespace", Text),
    Column("external_generation", Text),
    Column("external_object_id", Text),
    Column("upload_identity", Text),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

source_revisions = Table(
    "source_revisions", metadata,
    Column("revision_id", PostgreSQLUUID(as_uuid=True), primary_key=True),
    Column("source_object_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("workspace_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("environment_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("kind", Text, nullable=False),
    Column("revision_digest", Text, nullable=False),
    Column("revision_schema_version", Text, nullable=False),
    Column("canonicalization_profile", Text, nullable=False),
    Column("unicode_table_digest", Text, nullable=False),
    Column("bcp47_table_digest", Text, nullable=False),
    Column("revision_metadata_schema", Text, nullable=False),
    Column("revision_metadata", JSONB),
    Column("revision_metadata_digest", Text),
    Column("original_reference", JSONB),
    Column("original_sha256", Text),
    Column("byte_length", Integer),
    Column("media_type", Text),
    Column("reappearance_after_tombstone_revision_id", PostgreSQLUUID(as_uuid=True)),
    Column("deletion_reason", Text),
    Column("deletion_provenance", Text),
    Column("observed_at", DateTime(timezone=True), nullable=False),
)

source_revision_provenance = Table(
    "source_revision_provenance", metadata,
    Column("provenance_id", PostgreSQLUUID(as_uuid=True), primary_key=True),
    Column("source_object_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("revision_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("workspace_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("environment_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("producer_kind", Text, nullable=False),
    Column("producer_reference", Text, nullable=False),
    Column("event_identity", Text, nullable=False),
    Column("event_digest", Text, nullable=False),
    Column("connection_id", PostgreSQLUUID(as_uuid=True)),
    Column("upload_command_reference", Text),
    Column("origin_locator_kind", Text),
    Column("origin_locator_value", Text),
    Column("origin_locator_schema", Text),
    Column("origin_locator_digest", Text),
    Column("order_scheme", Text),
    Column("order_token", Text),
    Column("observer_service", Text, nullable=False),
    Column("received_at", DateTime(timezone=True), nullable=False),
    Column("observed_at", DateTime(timezone=True), nullable=False),
)

source_revision_receipt_associations = Table(
    "source_revision_receipt_associations", metadata,
    Column("association_id", PostgreSQLUUID(as_uuid=True), primary_key=True),
    Column("source_object_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("revision_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("provenance_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("workspace_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("environment_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("original_receipt_kind", Text, nullable=False),
    Column("original_receipt_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("original_command_result_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("original_command_result_schema_version", Integer, nullable=False),
    Column("original_upload_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("original_reference", JSONB, nullable=False),
    Column("original_sha256", String(64), nullable=False),
    Column("byte_length", Integer, nullable=False),
    Column("verification_status", Text, nullable=False),
    Column("storage_status", Text, nullable=False),
    Column("finalized_at", DateTime(timezone=True), nullable=False),
    Column("associated_at", DateTime(timezone=True), nullable=False),
)

ingestion_runs = Table(
    "ingestion_runs", metadata,
    Column("ingestion_run_id", PostgreSQLUUID(as_uuid=True), primary_key=True), Column("workspace_id", PostgreSQLUUID(as_uuid=True), nullable=False), Column("environment_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("command_identity", Text, nullable=False), Column("command_digest", String(64), nullable=False), Column("idempotency_receipt_id", PostgreSQLUUID(as_uuid=True), nullable=False), Column("trigger_kind", Text, nullable=False), Column("trigger_reference", Text), Column("requested_scope_schema", Text, nullable=False), Column("requested_scope_digest", String(64), nullable=False), Column("requested_at", DateTime(timezone=True), nullable=False),
)

ingestion_run_sources = Table(
    "ingestion_run_sources", metadata,
    Column("ingestion_run_source_id", PostgreSQLUUID(as_uuid=True), primary_key=True), Column("ingestion_run_id", PostgreSQLUUID(as_uuid=True), nullable=False), Column("workspace_id", PostgreSQLUUID(as_uuid=True), nullable=False), Column("environment_id", PostgreSQLUUID(as_uuid=True), nullable=False), Column("input_ordinal", Integer, nullable=False), Column("input_identity", Text, nullable=False), Column("input_digest", String(64), nullable=False), Column("command_identity", Text, nullable=False), Column("command_digest", String(64), nullable=False), Column("created_at", DateTime(timezone=True), nullable=False),
)

ingestion_run_source_bindings = Table(
    "ingestion_run_source_bindings", metadata,
    Column("binding_id", PostgreSQLUUID(as_uuid=True), primary_key=True), Column("ingestion_run_source_id", PostgreSQLUUID(as_uuid=True), nullable=False), Column("source_object_id", PostgreSQLUUID(as_uuid=True), nullable=False), Column("source_revision_id", PostgreSQLUUID(as_uuid=True), nullable=False), Column("workspace_id", PostgreSQLUUID(as_uuid=True), nullable=False), Column("environment_id", PostgreSQLUUID(as_uuid=True), nullable=False), Column("binding_command_identity", Text, nullable=False), Column("binding_command_digest", String(64), nullable=False), Column("original_reference", JSONB), Column("original_receipt_kind", Text), Column("original_receipt_id", PostgreSQLUUID(as_uuid=True)), Column("original_receipt_digest", String(64)), Column("idempotency_receipt_id", PostgreSQLUUID(as_uuid=True), nullable=False), Column("bound_at", DateTime(timezone=True), nullable=False),
)

parsed_documents = Table(
    "parsed_documents", metadata,
    Column("artifact_id", PostgreSQLUUID(as_uuid=True), primary_key=True),
    Column("workspace_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("environment_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("source_object_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("source_revision_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("source_revision_digest", String(64), nullable=False),
    Column("original_sha256", String(64), nullable=False),
    Column("artifact_digest", String(64), nullable=False),
    Column("schema_version", Text, nullable=False),
    Column("parser_name", Text, nullable=False),
    Column("parser_version", Text, nullable=False),
    Column("parser_config_reference", Text, nullable=False),
    Column("locator_contract_version", Text, nullable=False),
    Column("media_type", Text, nullable=False),
    Column("coverage", Text, nullable=False),
    Column("exclusions", JSONB, nullable=False),
    Column("source_byte_length", Integer, nullable=False),
    Column("source_codepoint_length", Integer, nullable=False),
    Column("command_identity", Text, nullable=False),
    Column("command_digest", String(64), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

parsed_document_segments = Table(
    "parsed_document_segments", metadata,
    Column("segment_id", PostgreSQLUUID(as_uuid=True), primary_key=True),
    Column("artifact_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("workspace_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("environment_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("segment_ordinal", Integer, nullable=False),
    Column("schema_version", Text, nullable=False),
    Column("segment_digest", String(64), nullable=False),
    Column("text", Text, nullable=False),
    Column("text_digest", String(64), nullable=False),
    Column("locator", JSONB, nullable=False),
    Column("heading_path", JSONB, nullable=False),
)

ingestion_transitions = Table(
    "ingestion_transitions", metadata,
    Column("ingestion_transition_id", PostgreSQLUUID(as_uuid=True), primary_key=True), Column("workspace_id", PostgreSQLUUID(as_uuid=True), nullable=False), Column("environment_id", PostgreSQLUUID(as_uuid=True), nullable=False), Column("target_kind", Text, nullable=False), Column("ingestion_run_id", PostgreSQLUUID(as_uuid=True), nullable=False), Column("ingestion_run_source_id", PostgreSQLUUID(as_uuid=True)), Column("predecessor_transition_id", PostgreSQLUUID(as_uuid=True)), Column("state", Text, nullable=False), Column("attempt", Integer, nullable=False), Column("safe_metrics_schema", Text), Column("safe_metrics", JSONB), Column("actor_or_service", Text, nullable=False), Column("trace_id", PostgreSQLUUID(as_uuid=True), nullable=False), Column("occurred_at", DateTime(timezone=True), nullable=False),
)

ingestion_warnings = Table(
    "ingestion_warnings", metadata,
    Column("ingestion_warning_id", PostgreSQLUUID(as_uuid=True), primary_key=True), Column("workspace_id", PostgreSQLUUID(as_uuid=True), nullable=False), Column("environment_id", PostgreSQLUUID(as_uuid=True), nullable=False), Column("ingestion_run_source_id", PostgreSQLUUID(as_uuid=True), nullable=False), Column("attempt", Integer, nullable=False), Column("stage", Text, nullable=False), Column("code", Text, nullable=False), Column("retryable", Boolean, nullable=False), Column("safe_detail_schema", Text, nullable=False), Column("safe_detail", JSONB, nullable=False), Column("safe_detail_digest", String(64), nullable=False), Column("opaque_detail_ref", PostgreSQLUUID(as_uuid=True)), Column("occurred_at", DateTime(timezone=True), nullable=False),
)

ingestion_failures = Table(
    "ingestion_failures", metadata,
    Column("ingestion_failure_id", PostgreSQLUUID(as_uuid=True), primary_key=True), Column("workspace_id", PostgreSQLUUID(as_uuid=True), nullable=False), Column("environment_id", PostgreSQLUUID(as_uuid=True), nullable=False), Column("ingestion_run_source_id", PostgreSQLUUID(as_uuid=True), nullable=False), Column("attempt", Integer, nullable=False), Column("stage", Text, nullable=False), Column("code", Text, nullable=False), Column("retryable", Boolean, nullable=False), Column("safe_detail_schema", Text, nullable=False), Column("safe_detail", JSONB, nullable=False), Column("safe_detail_digest", String(64), nullable=False), Column("opaque_detail_ref", PostgreSQLUUID(as_uuid=True)), Column("occurred_at", DateTime(timezone=True), nullable=False),
)

processing_receipts = Table(
    "processing_receipts", metadata,
    Column("processing_receipt_id", PostgreSQLUUID(as_uuid=True), primary_key=True), Column("workspace_id", PostgreSQLUUID(as_uuid=True), nullable=False), Column("environment_id", PostgreSQLUUID(as_uuid=True), nullable=False), Column("ingestion_run_source_id", PostgreSQLUUID(as_uuid=True), nullable=False), Column("binding_id", PostgreSQLUUID(as_uuid=True), nullable=False), Column("source_object_id", PostgreSQLUUID(as_uuid=True), nullable=False), Column("source_revision_id", PostgreSQLUUID(as_uuid=True), nullable=False), Column("processor_kind", Text, nullable=False), Column("parser_name", Text, nullable=False), Column("parser_version", Text, nullable=False), Column("parser_config_reference", Text, nullable=False), Column("locator_contract_version", Text, nullable=False), Column("original_sha256", String(64), nullable=False), Column("output_kind", Text), Column("output_reference", PostgreSQLUUID(as_uuid=True)), Column("output_digest", String(64)), Column("coverage", Text, nullable=False), Column("exclusions", JSONB, nullable=False), Column("warning_ids", JSONB, nullable=False), Column("failure_ids", JSONB, nullable=False), Column("command_identity", Text, nullable=False), Column("command_digest", String(64), nullable=False), Column("idempotency_receipt_id", PostgreSQLUUID(as_uuid=True), nullable=False), Column("completed_at", DateTime(timezone=True), nullable=False),
)
admission_decisions = Table(
    "admission_decisions", metadata,
    Column("admission_decision_id", PostgreSQLUUID(as_uuid=True), primary_key=True),
    Column("workspace_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("environment_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("source_object_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("source_revision_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("decision_version", Integer, nullable=False),
    Column("predecessor_decision_id", PostgreSQLUUID(as_uuid=True)),
    Column("disposition", Text, nullable=False),
    Column("validator_profile_version", Text, nullable=False),
    Column("reason_schema_version", Text, nullable=False),
    Column("reason_codes", JSONB, nullable=False),
    Column("authority_reference", JSONB, nullable=False),
    Column("trace_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("audit_event_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("decided_at", DateTime(timezone=True), nullable=False),
)

access_policy_versions = Table(
    "access_policy_versions", metadata,
    Column("access_policy_version_id", PostgreSQLUUID(as_uuid=True), primary_key=True),
    Column("workspace_id", PostgreSQLUUID(as_uuid=True), nullable=False), Column("environment_id", PostgreSQLUUID(as_uuid=True), nullable=False), Column("source_object_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("version", Integer, nullable=False), Column("predecessor_policy_version_id", PostgreSQLUUID(as_uuid=True)), Column("document_schema_version", Text, nullable=False), Column("document_hash", String(64), nullable=False),
    Column("origin_kind", Text, nullable=False), Column("origin_reference", PostgreSQLUUID(as_uuid=True), nullable=False), Column("origin_version", Integer, nullable=False), Column("origin_hash", String(64), nullable=False),
    Column("created_by_canonical_human_id", PostgreSQLUUID(as_uuid=True), nullable=False), Column("created_at", DateTime(timezone=True), nullable=False),
)
access_policy_grants = Table(
    "access_policy_grants", metadata,
    Column("access_grant_id", PostgreSQLUUID(as_uuid=True), primary_key=True), Column("workspace_id", PostgreSQLUUID(as_uuid=True), nullable=False), Column("environment_id", PostgreSQLUUID(as_uuid=True), nullable=False), Column("source_object_id", PostgreSQLUUID(as_uuid=True), nullable=False), Column("access_policy_version_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("principal_kind", Text, nullable=False), Column("principal_id", PostgreSQLUUID(as_uuid=True), nullable=False), Column("permission", Text, nullable=False), Column("purpose", Text), Column("operation", Text), Column("valid_from", DateTime(timezone=True)), Column("valid_until", DateTime(timezone=True)),
)
policy_proposals = Table(
    "policy_proposals", metadata,
    Column("policy_proposal_id", PostgreSQLUUID(as_uuid=True), primary_key=True), Column("workspace_id", PostgreSQLUUID(as_uuid=True), nullable=False), Column("environment_id", PostgreSQLUUID(as_uuid=True), nullable=False), Column("source_object_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("proposal_version", Integer, nullable=False), Column("predecessor_proposal_id", PostgreSQLUUID(as_uuid=True)), Column("proposed_policy_version_id", PostgreSQLUUID(as_uuid=True), nullable=False), Column("proposed_policy_hash", String(64), nullable=False), Column("requester_canonical_human_id", PostgreSQLUUID(as_uuid=True), nullable=False), Column("expected_policy_version_id", PostgreSQLUUID(as_uuid=True)), Column("expected_policy_generation", Integer, nullable=False), Column("benefit_classification", Text, nullable=False), Column("benefit_schema_version", Text, nullable=False), Column("membership_generation", Integer, nullable=False), Column("authority_generation", Integer, nullable=False), Column("service_authority_generation", Integer, nullable=False), Column("submitted_at", DateTime(timezone=True), nullable=False),
)
policy_approvals = Table(
    "policy_approvals", metadata,
    Column("policy_approval_id", PostgreSQLUUID(as_uuid=True), primary_key=True), Column("workspace_id", PostgreSQLUUID(as_uuid=True), nullable=False), Column("environment_id", PostgreSQLUUID(as_uuid=True), nullable=False), Column("source_object_id", PostgreSQLUUID(as_uuid=True), nullable=False), Column("policy_proposal_id", PostgreSQLUUID(as_uuid=True), nullable=False), Column("proposal_version", Integer, nullable=False), Column("proposal_hash", String(64), nullable=False), Column("approver_canonical_human_id", PostgreSQLUUID(as_uuid=True), nullable=False), Column("identity_proof_reference", PostgreSQLUUID(as_uuid=True), nullable=False), Column("authority_snapshot_reference", PostgreSQLUUID(as_uuid=True), nullable=False), Column("membership_generation", Integer, nullable=False), Column("authority_generation", Integer, nullable=False), Column("decision", Text, nullable=False), Column("reason_code", Text, nullable=False), Column("decided_at", DateTime(timezone=True), nullable=False),
)
policy_activations = Table(
    "policy_activations", metadata,
    Column("policy_activation_id", PostgreSQLUUID(as_uuid=True), primary_key=True), Column("workspace_id", PostgreSQLUUID(as_uuid=True), nullable=False), Column("environment_id", PostgreSQLUUID(as_uuid=True), nullable=False), Column("source_object_id", PostgreSQLUUID(as_uuid=True), nullable=False), Column("predecessor_policy_version_id", PostgreSQLUUID(as_uuid=True)), Column("new_policy_version_id", PostgreSQLUUID(as_uuid=True), nullable=False), Column("policy_generation", Integer, nullable=False), Column("policy_proposal_id", PostgreSQLUUID(as_uuid=True)), Column("policy_proposal_version", Integer), Column("policy_proposal_hash", String(64)), Column("policy_approval_id", PostgreSQLUUID(as_uuid=True)), Column("policy_approval_decision", Text), Column("initial_eligibility_decision_id", PostgreSQLUUID(as_uuid=True)), Column("requester_canonical_human_id", PostgreSQLUUID(as_uuid=True), nullable=False), Column("activator_canonical_human_id", PostgreSQLUUID(as_uuid=True), nullable=False), Column("membership_generation", Integer, nullable=False), Column("authority_generation", Integer, nullable=False), Column("service_authority_generation", Integer, nullable=False), Column("idempotency_receipt_id", PostgreSQLUUID(as_uuid=True), nullable=False), Column("audit_event_id", PostgreSQLUUID(as_uuid=True), nullable=False), Column("outbox_event_id", PostgreSQLUUID(as_uuid=True), nullable=False), Column("activated_at", DateTime(timezone=True), nullable=False),
)
current_access_policies = Table(
    "current_access_policies", metadata,
    Column("source_object_id", PostgreSQLUUID(as_uuid=True), primary_key=True), Column("workspace_id", PostgreSQLUUID(as_uuid=True), nullable=False), Column("environment_id", PostgreSQLUUID(as_uuid=True), nullable=False), Column("access_policy_version_id", PostgreSQLUUID(as_uuid=True), nullable=False), Column("policy_activation_id", PostgreSQLUUID(as_uuid=True), nullable=False), Column("policy_generation", Integer, nullable=False), Column("cas_version", Integer, nullable=False),
)

backup_fence_leases = Table(
    "backup_fence_leases",
    metadata,
    Column("deployment_scope_id", Text, primary_key=True),
    Column("active_fence_epoch", BigInteger),
    Column("fence_state", String(32), nullable=False),
    Column("cutoff_ticket_sequence", BigInteger),
    Column("lease_owner", Text),
    Column("lease_expires_at", DateTime(timezone=True)),
    Column("lease_renewal", DateTime(timezone=True)),
    Column("takeover_reference", Text),
    Column("ticket_admission", String(16), nullable=False),
    Column("cleanup_release_progress", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

recovery_quiescence_evidence = Table(
    "recovery_quiescence_evidence",
    metadata,
    Column("deployment_scope_id", Text, nullable=False),
    Column("fence_epoch", BigInteger, nullable=False),
    Column("schema_version", Integer, nullable=False),
    Column("cutoff_ticket_sequence", BigInteger, nullable=False),
    Column("watermark", Text, nullable=False),
    Column("pre_fence_terminal_ticket_count", BigInteger, nullable=False),
    Column("indeterminate_ticket_count", BigInteger, nullable=False),
    Column("captured_at", DateTime(timezone=True), nullable=False),
    Column("verification_digest", String(64), nullable=False),
    PrimaryKeyConstraint("deployment_scope_id", "fence_epoch"),
)

fence_operation_tickets = Table(
    "fence_operation_tickets",
    metadata,
    Column("ticket_id", Text, primary_key=True),
    Column("deployment_scope_id", Text, nullable=False),
    Column("ticket_sequence", BigInteger, nullable=False),
    Column("fence_epoch", BigInteger),
    Column("operation_kind", String(64), nullable=False),
    Column("target_reference", Text, nullable=False),
    Column("admitted_at", DateTime(timezone=True), nullable=False),
    Column("state", String(64), nullable=False),
    Column("terminal_at", DateTime(timezone=True)),
    Column("safe_failure_category", String(64)),
    Column("correlation_id", Text),
    Column("schema_version", Integer, nullable=False),
)

storage_recovery_manifests = Table(
    "storage_recovery_manifests",
    metadata,
    Column("recovery_set_id", Text, primary_key=True),
    Column("deployment_scope_id", Text, nullable=False),
    Column("schema_version", Integer, nullable=False),
    Column("profile", String(32), nullable=False),
    Column("manifest_status", String(32), nullable=False),
    Column("fence_epoch", BigInteger, nullable=False),
    Column("cutoff_ticket_sequence", BigInteger, nullable=False),
    Column("quiescent_evidence", JSONB),
    Column("postgres_boundary", Text, nullable=False),
    Column("postgres_recovery_watermark", Text),
    Column("object_volume_generation", Text, nullable=False),
    Column("postgres_component_state", String(16), nullable=False),
    Column("object_component_state", String(16), nullable=False),
    Column("postgres_backup_identity", Text),
    Column("postgres_verification_reference", Text),
    Column("object_verification_reference", Text),
    Column("drained_operation_count", BigInteger, nullable=False),
    Column("orphaned_operation_count", BigInteger, nullable=False),
    Column("indeterminate_operation_count", BigInteger, nullable=False),
    Column("integrity_algorithm", String(16), nullable=False),
    Column("manifest_digest", String(64), nullable=False),
    Column("scope", Text, nullable=False),
    Column("start_watermark", Text),
    Column("end_watermark", Text),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("completed_at", DateTime(timezone=True)),
)

recovery_evidence_invalidations = Table(
    "recovery_evidence_invalidations",
    metadata,
    Column("deployment_scope_id", Text, nullable=False),
    Column("sequence", BigInteger, nullable=False),
    Column("schema_version", Integer, nullable=False),
    Column("recovery_set_id", Text, nullable=False),
    Column("manifest_digest", String(64), nullable=False),
    Column("event_kind", String(32), nullable=False),
    Column("reason_code", String(128), nullable=False),
    Column("replacement_recovery_set_id", Text),
    Column("replacement_manifest_digest", String(64)),
    Column("replacement_validation_reference", Text),
    Column("replacement_approval_reference", Text),
    Column("previous_event_digest", String(64)),
    Column("event_digest", String(64), nullable=False),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    PrimaryKeyConstraint(
        "deployment_scope_id",
        "sequence",
        name="pk_recovery_evidence_invalidations",
    ),
)
canonical_generation_counters = Table(
    "canonical_generation_counters", metadata,
    Column("workspace_id", PostgreSQLUUID(as_uuid=True), primary_key=True),
    Column("environment_id", PostgreSQLUUID(as_uuid=True), primary_key=True),
    Column("current_generation", Integer, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

canonical_acceptances = Table(
    "canonical_acceptances", metadata,
    Column("canonical_acceptance_id", PostgreSQLUUID(as_uuid=True), primary_key=True),
    Column("source_object_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("source_revision_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("workspace_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("environment_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("predecessor_revision_id", PostgreSQLUUID(as_uuid=True)),
    Column("predecessor_acceptance_id", PostgreSQLUUID(as_uuid=True)),
    Column("accepted_generation", Integer, nullable=False),
    Column("ordering", Text, nullable=False),
    Column("ordering_scheme", Text),
    Column("ordering_token", Text),
    Column("admission_decision_id", PostgreSQLUUID(as_uuid=True)),
    Column("access_policy_version_id", PostgreSQLUUID(as_uuid=True)),
    Column("provenance_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("idempotency_receipt_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("audit_event_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("outbox_event_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("accepted_at", DateTime(timezone=True), nullable=False),
)

current_source_heads = Table(
    "current_source_heads", metadata,
    Column("source_object_id", PostgreSQLUUID(as_uuid=True), primary_key=True),
    Column("workspace_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("environment_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("current_revision_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("current_revision_kind", Text, nullable=False),
    Column("acceptance_id", PostgreSQLUUID(as_uuid=True), nullable=False),
    Column("accepted_generation", Integer, nullable=False),
    Column("cas_version", Integer, nullable=False),
    Column("ordering_scheme", Text),
    Column("ordering_token", Text),
    Column("changed_at", DateTime(timezone=True), nullable=False),
)

__all__ = [
    "access_policy_grants",
    "access_policy_versions",
    "audit_events",
    "environments",
    "idempotency_receipts",
    "metadata",
    "outbox_intents",
    "outbox_deliveries",
    "workspaces",
    "source_objects",
    "source_revisions",
    "source_revision_provenance",
    "source_revision_receipt_associations",
    "ingestion_runs",
    "ingestion_run_sources",
    "ingestion_run_source_bindings",
    "ingestion_transitions",
    "ingestion_warnings",
    "ingestion_failures",
    "processing_receipts",
    "admission_decisions",
    "current_access_policies",
    "policy_activations",
    "policy_approvals",
    "policy_proposals",
    "backup_fence_leases",
    "fence_operation_tickets",
    "recovery_quiescence_evidence",
    "storage_recovery_manifests",
    "recovery_evidence_invalidations",
    "canonical_generation_counters",
    "canonical_acceptances",
    "current_source_heads",
]
