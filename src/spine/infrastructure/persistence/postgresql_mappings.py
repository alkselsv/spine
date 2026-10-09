"""Infrastructure-only SQLAlchemy mappings for canonical tenant records."""

from sqlalchemy import Column, MetaData, Table, Text
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


__all__ = ["environments", "metadata", "workspaces"]
