"""Infrastructure-only SQLAlchemy mappings for canonical tenant records."""

from sqlalchemy import Column, DateTime, Integer, MetaData, String, Table, Text, text
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


__all__ = [
    "audit_events",
    "environments",
    "idempotency_receipts",
    "metadata",
    "outbox_intents",
    "workspaces",
    "source_objects",
    "source_revisions",
    "source_revision_provenance",
]
