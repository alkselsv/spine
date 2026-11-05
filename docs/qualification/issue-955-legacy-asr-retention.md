# Issue #955: pre-v2 single-source ASR retention qualification

## Result

PASS for the synthetic PostgreSQL qualification. The historical report row uses
the persisted v1 JSONB retention shape with exactly `source_revision_id` and
`disposition`; source identity is resolved through the immutable report binding
and canonical `SourceObject`/`SourceRevision` records. Reads and authorized
expiration preserve the historical JSONB value. Stale access policy, retention
terms, lifecycle withdrawal, ambiguous or malformed lineage, forged deletion
lineage, and cross-workspace read/delete attempts fail closed.

## Environment

- PostgreSQL: `17.6` (`postgres:17.6-bookworm` test image).
- Runtime role: `spine_runtime`; the qualification asserts `NOBYPASSRLS`.
- Data: synthetic only.
- Tested code commit: `50cd7fdb`.

## Qualification command

Command:

```text
/home/dima55279/.pyenv/versions/3.14.4/bin/uv run pytest -q tests/integration/postgresql/test_postgresql_private_asr_storage.py
```

Result: exit code `0`; `128 passed`.

Focused issue nodes: exit code `0`; `9 passed`.

```text
tests/integration/postgresql/test_postgresql_private_asr_storage.py::test_postgresql_private_asr_legacy_single_source_retention_row_reads_and_expires_without_rewrite
tests/integration/postgresql/test_postgresql_private_asr_storage.py::test_postgresql_private_asr_legacy_single_source_row_denies_stale_current_retention
tests/integration/postgresql/test_postgresql_private_asr_storage.py::test_postgresql_private_asr_legacy_single_source_row_denies_stale_current_access_policy
tests/integration/postgresql/test_postgresql_private_asr_storage.py::test_postgresql_private_asr_legacy_single_source_row_denies_current_lifecycle_withdrawal
tests/integration/postgresql/test_postgresql_private_asr_storage.py::test_postgresql_private_asr_legacy_single_source_row_denies_ambiguous_source_lineage
tests/integration/postgresql/test_postgresql_private_asr_storage.py::test_postgresql_private_asr_legacy_single_source_row_denies_forged_deletion_lineage
tests/integration/postgresql/test_postgresql_private_asr_storage.py::test_postgresql_private_asr_legacy_retention_jsonb_malformed_lineage_is_denied[incomplete-95595500]
tests/integration/postgresql/test_postgresql_private_asr_storage.py::test_postgresql_private_asr_legacy_retention_jsonb_malformed_lineage_is_denied[substituted-95595510]
tests/integration/postgresql/test_postgresql_private_asr_storage.py::test_postgresql_private_asr_legacy_retention_jsonb_malformed_lineage_is_denied[mixed_version-95595520]
```

The malformed fixtures use the existing staging-to-committed transition and
schema constraints. No migration or production authorization change was made.
