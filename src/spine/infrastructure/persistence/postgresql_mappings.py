"""Infrastructure-only SQLAlchemy mappings for canonical tenant records."""

from sqlalchemy import Column, DateTime, Integer, MetaData, String, Table, Text
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


__all__ = ["environments", "idempotency_receipts", "metadata", "workspaces"]
