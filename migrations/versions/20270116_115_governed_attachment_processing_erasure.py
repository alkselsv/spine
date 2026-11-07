"""Extend governed PostgreSQL erasure to message and processing lineage."""

from alembic import op
import sqlalchemy as sa

from spine.infrastructure.db.settings import MigrationDatabaseSettings


revision = "20270116_115"
down_revision = "20270115_114"
branch_labels = None
depends_on = None

SCHEMA = "spine"

# These rows contain source/revision identity or derived attachment evidence.
# They are deleted only by the existing operation-scoped executor after its
# current-authority, fence and canonical-head checks succeed.
SOURCE_OBJECT_TABLES = (
    "email_message_artifacts",
    "email_attachment_artifacts",
    "process_only_operation_lineages",
    "source_processing_admissions",
    "processing_receipts",
    "parsed_documents",
)
SPECIALIZED_TABLES = (
    "email_attachment_processing_receipts",
    "parsed_document_segments",
)
IMMUTABLE_GUARDS = (
    ("email_message_artifacts", "reject_email_message_artifact_mutation"),
    ("email_attachment_artifacts", "reject_email_attachment_artifact_mutation"),
    (
        "email_attachment_processing_receipts",
        "reject_email_attachment_processing_receipt_mutation",
    ),
    ("parsed_documents", "reject_parsed_documents_mutation"),
    ("parsed_document_segments", "reject_parsed_document_segments_mutation"),
    ("process_only_operation_lineages", "reject_process_only_operation_lineages_mutation"),
    ("processing_receipts", "reject_processing_receipts_mutation"),
    ("source_processing_admissions", "reject_source_processing_admission_mutation"),
)


def _role(attribute: str) -> str:
    settings = op.get_context().config.attributes.get("migration_settings")
    if not isinstance(settings, MigrationDatabaseSettings):
        raise RuntimeError("migration_settings must be MigrationDatabaseSettings")
    return op.get_bind().dialect.identifier_preparer.quote(getattr(settings, attribute))


def _replace_executor_body() -> None:
    # Preserve the established authority/fence/CAS implementation verbatim and
    # extend only its closed table list and exact-row deletion dispatch.
    old_table_anchor = (
        "                'current_source_heads',\n"
        "                'canonical_acceptances',"
    )
    new_table_anchor = (
        "                'current_source_heads',\n"
        "                'email_attachment_processing_receipts',\n"
        "                'processing_receipts',\n"
        "                'process_only_operation_lineages',\n"
        "                'source_processing_admissions',\n"
        "                'parsed_document_segments',\n"
        "                'parsed_documents',\n"
        "                'email_message_artifacts',\n"
        "                'email_attachment_artifacts',\n"
        "                'canonical_acceptances',"
    )
    old_dispatch_anchor = "                ELSE\n                    FOR target IN EXECUTE format(\n"
    new_dispatch_anchor = """                ELSIF target_table = 'email_attachment_processing_receipts' THEN
                    FOR target IN SELECT to_jsonb(t) AS row_data
                        FROM {SCHEMA}.email_attachment_processing_receipts t
                        LEFT JOIN {SCHEMA}.email_attachment_artifacts a
                          ON a.workspace_id = t.workspace_id
                         AND a.environment_id = t.environment_id
                         AND a.artifact_id = t.attachment_artifact_id
                       WHERE t.workspace_id = p_workspace_id
                         AND t.environment_id = p_environment_id
                         AND (a.source_object_id = source_id
                              OR t.child_source_object_id = source_id)
                    LOOP
                        INSERT INTO {SCHEMA}.source_erasure_execution_contexts
                            (transaction_id, workspace_id, environment_id, operation_id,
                             source_object_id, target_table, target_record)
                        VALUES (operation_xid, p_workspace_id, p_environment_id, p_operation_id,
                                source_id, target_table, target.row_data);
                    END LOOP;
                    DELETE FROM {SCHEMA}.email_attachment_processing_receipts t
                     WHERE t.workspace_id = p_workspace_id
                       AND t.environment_id = p_environment_id
                       AND (EXISTS (
                            SELECT 1 FROM {SCHEMA}.email_attachment_artifacts a
                             WHERE a.workspace_id = t.workspace_id
                               AND a.environment_id = t.environment_id
                               AND a.artifact_id = t.attachment_artifact_id
                               AND a.source_object_id = source_id
                       ) OR t.child_source_object_id = source_id);
                ELSIF target_table = 'parsed_document_segments' THEN
                    FOR target IN SELECT to_jsonb(segment) AS row_data
                        FROM {SCHEMA}.parsed_document_segments segment
                        JOIN {SCHEMA}.parsed_documents document
                          ON document.workspace_id = segment.workspace_id
                         AND document.environment_id = segment.environment_id
                         AND document.artifact_id = segment.artifact_id
                       WHERE document.workspace_id = p_workspace_id
                         AND document.environment_id = p_environment_id
                         AND document.source_object_id = source_id
                    LOOP
                        INSERT INTO {SCHEMA}.source_erasure_execution_contexts
                            (transaction_id, workspace_id, environment_id, operation_id,
                             source_object_id, target_table, target_record)
                        VALUES (operation_xid, p_workspace_id, p_environment_id, p_operation_id,
                                source_id, target_table, target.row_data);
                    END LOOP;
                    DELETE FROM {SCHEMA}.parsed_document_segments segment
                     USING {SCHEMA}.parsed_documents document
                     WHERE document.workspace_id = segment.workspace_id
                       AND document.environment_id = segment.environment_id
                       AND document.artifact_id = segment.artifact_id
                       AND document.workspace_id = p_workspace_id
                       AND document.environment_id = p_environment_id
                       AND document.source_object_id = source_id;
                ELSE
                    FOR target IN EXECUTE format(
"""
    connection = op.get_bind()
    function_oid = connection.execute(
        sa.text(
            "SELECT 'spine.execute_source_disposition_erasure(uuid, uuid, uuid, integer)'::regprocedure::oid"
        )
    ).scalar_one()
    # regprocedure OID is resolved only after the catalog query; pass it as a
    # bound value to avoid interpolating any identifier or database payload.
    definition = connection.execute(
        sa.text("SELECT pg_get_functiondef(:function_oid)"),
        {"function_oid": function_oid},
    ).scalar_one()
    if definition.count(old_table_anchor) != 1:
        raise RuntimeError("unexpected erasure executor table-list contract")
    if definition.count(old_dispatch_anchor) != 1:
        raise RuntimeError("unexpected erasure executor dispatch contract")
    definition = definition.replace(old_table_anchor, new_table_anchor, 1)
    definition = definition.replace(
        old_dispatch_anchor,
        new_dispatch_anchor.format(SCHEMA=SCHEMA),
        1,
    )
    signature = "CREATE OR REPLACE FUNCTION spine.execute_source_disposition_erasure"
    if definition.count(signature) != 1:
        raise RuntimeError("unexpected erasure executor definition header")
    connection.exec_driver_sql(
        definition,
        execution_options={"no_parameters": True},
    )


def _install_source_object_table_policy(table: str, executor: str, runtime: str) -> None:
    op.execute(
        f"CREATE POLICY pol_{table}_erasure_executor ON {SCHEMA}.{table} "
        f"FOR ALL TO {executor} USING "
        f"({SCHEMA}.source_erasure_scope_allowed(workspace_id, environment_id, source_object_id)) "
        "WITH CHECK (true)"
    )
    op.execute(f"GRANT SELECT, DELETE ON {SCHEMA}.{table} TO {executor}")
    op.execute(f"REVOKE DELETE ON {SCHEMA}.{table} FROM {runtime}")


def _install_immutable_guard(table: str, function: str, executor: str) -> None:
    quoted_function = op.get_bind().dialect.identifier_preparer.quote(function)
    executor_name = executor.strip('"')
    op.execute(
        f"CREATE OR REPLACE FUNCTION {SCHEMA}.{quoted_function}() RETURNS trigger "
        f"LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, {SCHEMA} "
        "AS $function$ BEGIN "
        f"IF TG_OP <> 'DELETE' OR current_user <> '{executor_name}' OR NOT EXISTS ("
        f"SELECT 1 FROM {SCHEMA}.source_erasure_execution_contexts context "
        "WHERE context.transaction_id = pg_current_xact_id() "
        "AND context.workspace_id = OLD.workspace_id "
        "AND context.environment_id = OLD.environment_id "
        "AND context.target_table = TG_TABLE_NAME "
        "AND context.target_record = to_jsonb(OLD)) THEN "
        "RAISE EXCEPTION 'Source history is immutable outside governed erasure.' "
        "USING ERRCODE = '23514', CONSTRAINT = 'ck_governed_source_erasure_only'; "
        "END IF; RETURN OLD; END; $function$"
    )
    op.execute(
        f"REVOKE ALL ON FUNCTION {SCHEMA}.{quoted_function}() FROM PUBLIC, {executor}"
    )


def upgrade() -> None:
    executor = _role("erasure_role")
    runtime = _role("runtime_role")

    _replace_executor_body()

    for table in SOURCE_OBJECT_TABLES:
        _install_source_object_table_policy(table, executor, runtime)

    # The attachment receipt has both parent and child ownership. Disposing
    # either source removes this cross-source receipt; the other source's
    # independently owned artifact remains intact.
    op.execute(
        f"CREATE POLICY pol_email_attachment_processing_receipts_erasure_executor "
        f"ON {SCHEMA}.email_attachment_processing_receipts FOR ALL TO {executor} "
        "USING (EXISTS (SELECT 1 FROM spine.email_attachment_artifacts attachment "
        "WHERE attachment.workspace_id = email_attachment_processing_receipts.workspace_id "
        "AND attachment.environment_id = email_attachment_processing_receipts.environment_id "
        "AND attachment.artifact_id = email_attachment_processing_receipts.attachment_artifact_id "
        "AND spine.source_erasure_scope_allowed(attachment.workspace_id, "
        "attachment.environment_id, attachment.source_object_id)) OR "
        "spine.source_erasure_scope_allowed(workspace_id, environment_id, child_source_object_id)) "
        "WITH CHECK (true)"
    )
    op.execute(
        f"GRANT SELECT, DELETE ON {SCHEMA}.email_attachment_processing_receipts TO {executor}"
    )
    op.execute(
        f"REVOKE DELETE ON {SCHEMA}.email_attachment_processing_receipts FROM {runtime}"
    )

    op.execute(
        f"CREATE POLICY pol_parsed_document_segments_erasure_executor ON "
        f"{SCHEMA}.parsed_document_segments FOR ALL TO {executor} USING (EXISTS ("
        f"SELECT 1 FROM {SCHEMA}.parsed_documents document "
        "WHERE document.workspace_id = parsed_document_segments.workspace_id "
        "AND document.environment_id = parsed_document_segments.environment_id "
        "AND document.artifact_id = parsed_document_segments.artifact_id "
        "AND spine.source_erasure_scope_allowed(document.workspace_id, "
        "document.environment_id, document.source_object_id))) WITH CHECK (true)"
    )
    op.execute(
        f"GRANT SELECT, DELETE ON {SCHEMA}.parsed_document_segments TO {executor}"
    )
    op.execute(f"REVOKE DELETE ON {SCHEMA}.parsed_document_segments FROM {runtime}")

    for table, function in IMMUTABLE_GUARDS:
        _install_immutable_guard(table, function, executor)


def downgrade() -> None:
    raise RuntimeError("attachment and processing lineage erasure migration is irreversible")
