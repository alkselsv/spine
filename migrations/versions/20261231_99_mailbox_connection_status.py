"""Expose refs-only mailbox authority through provider connections."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20261231_99"
down_revision = "20261230_98"
branch_labels = None
depends_on = None
SCHEMA = "spine"


def upgrade() -> None:
    empty_operations = sa.text("'{}'::varchar[]")
    for table in ("provider_connection_approvals", "provider_connection_versions"):
        op.add_column(
            table,
            sa.Column(
                "permitted_operations",
                postgresql.ARRAY(sa.String(64)),
                nullable=False,
                server_default=empty_operations,
            ),
            schema=SCHEMA,
        )
        op.create_check_constraint(
            f"ck_{table}_permitted_operations",
            table,
            "array_position(permitted_operations, NULL) IS NULL AND "
            "permitted_operations <@ ARRAY["
            "'enumerate_mailbox', 'enumerate_folder_uids', "
            "'read_message_metadata', 'read_raw_message', 'read_attachment', "
            "'synchronize', 'observe_folder_generation'"
            "]::varchar[] AND (cardinality(permitted_operations) = 0 OR "
            "provider_kind IN ('google', 'yandex_mail'))",
            schema=SCHEMA,
        )

    op.drop_constraint(
        "ck_provider_connection_approval_kind",
        "provider_connection_approvals",
        schema=SCHEMA,
        type_="check",
    )
    op.create_check_constraint(
        "ck_provider_connection_approval_kind",
        "provider_connection_approvals",
        "provider_kind IN ('google', 'yandex_disk', 'bitrix24', "
        "'mounted_filesystem', 'yandex_mail')",
        schema=SCHEMA,
    )
    op.drop_constraint(
        "ck_provider_connection_kind",
        "provider_connection_versions",
        schema=SCHEMA,
        type_="check",
    )
    op.create_check_constraint(
        "ck_provider_connection_kind",
        "provider_connection_versions",
        "provider_kind IN ('google', 'yandex_disk', 'bitrix24', "
        "'mounted_filesystem', 'yandex_mail')",
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_provider_connection_kind",
        "provider_connection_versions",
        schema=SCHEMA,
        type_="check",
    )
    op.create_check_constraint(
        "ck_provider_connection_kind",
        "provider_connection_versions",
        "provider_kind IN ('google', 'yandex_disk', 'bitrix24', 'mounted_filesystem')",
        schema=SCHEMA,
    )
    op.drop_constraint(
        "ck_provider_connection_approval_kind",
        "provider_connection_approvals",
        schema=SCHEMA,
        type_="check",
    )
    op.create_check_constraint(
        "ck_provider_connection_approval_kind",
        "provider_connection_approvals",
        "provider_kind IN ('google', 'yandex_disk', 'bitrix24', 'mounted_filesystem')",
        schema=SCHEMA,
    )
    for table in ("provider_connection_versions", "provider_connection_approvals"):
        op.drop_constraint(
            f"ck_{table}_permitted_operations",
            table,
            schema=SCHEMA,
            type_="check",
        )
        op.drop_column(table, "permitted_operations", schema=SCHEMA)
