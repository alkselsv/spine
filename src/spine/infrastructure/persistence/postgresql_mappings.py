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


__all__ = [
    "audit_events",
    "environments",
    "idempotency_receipts",
    "metadata",
    "outbox_intents",
    "workspaces",
]
