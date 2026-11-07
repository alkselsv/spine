from sqlalchemy import ForeignKeyConstraint, UniqueConstraint

from spine.infrastructure.persistence.postgresql_mappings import metadata


def _constraint_names(table, constraint_type):
    return {
        constraint.name
        for constraint in table.constraints
        if isinstance(constraint, constraint_type)
    }


def test_disposition_state_is_scoped_versioned_and_bounded() -> None:
    operations = metadata.tables["spine.source_disposition_operations"]
    outbox = metadata.tables["spine.source_disposition_outbox"]

    assert list(operations.primary_key.columns.keys()) == [
        "workspace_id",
        "environment_id",
        "operation_id",
    ]
    assert {
        "request_digest",
        "state_version",
        "status",
        "fence_active",
        "operation_data",
        "completion_ack_expires_at",
    }.issubset(operations.c.keys())
    assert not {
        "message_id",
        "email_address",
        "content",
        "filename",
        "source_revision_id",
        "provider_payload",
    }.intersection(operations.c.keys())
    assert "fk_source_disposition_operations_environment" in _constraint_names(
        operations, ForeignKeyConstraint
    )

    assert "fk_source_disposition_outbox_operation" in _constraint_names(
        outbox, ForeignKeyConstraint
    )
    assert "uq_source_disposition_outbox_operation_event" in _constraint_names(
        outbox, UniqueConstraint
    )
    assert "event_id" in outbox.c and "operation_id" in outbox.c
    assert not {"payload", "message_id", "content", "source_revision_id"}.intersection(
        outbox.c.keys()
    )
