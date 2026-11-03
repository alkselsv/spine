# R2 Bitrix24 source-admission offline qualification

Issue #729 qualifies the merged #723–#728 slices at their public domain,
application, persistence and disclosure seams. The qualification is deterministic
and synthetic except for the isolated PostgreSQL 17.6 container. It performs no
Bitrix24 calls and does not activate production admission.

Run from the repository root at the `qualification_commit` recorded in
`docs/qualification/issue-729-bitrix24-source-admission.json`. The separate
`qualified_base_commit` identifies the merged product revision under qualification;
the qualification commit contains this runbook, its executable traceability test,
and the strengthened cross-slice evidence.

```bash
UV_CACHE_DIR=/private/tmp/spine-uv-cache uv sync --extra dev
UV_CACHE_DIR=/private/tmp/spine-uv-cache uv run pytest -q \
  tests/application/test_bitrix24_disk_scope_admission.py \
  tests/connectors/test_bitrix24_disk_traversal.py \
  tests/application/storage/test_trusted_content.py \
  tests/application/test_canonical_acceptance_selectors.py \
  tests/application/persistence/test_canonical_acceptance_retry.py \
  tests/domain/sources/test_processing_admission.py \
  tests/application/test_projection_request_resolution.py \
  tests/application/test_projection_request_execution.py \
  tests/application/test_authorized_context_broker.py \
  tests/application/test_disclosure_revalidation.py \
  tests/application/test_processing_commands.py \
  tests/qualification/test_issue598_google_qualification.py \
  tests/qualification/test_issue665_yandex_disk.py \
  tests/qualification/test_issue680_mounted_filesystem.py
UV_CACHE_DIR=/private/tmp/spine-uv-cache uv run pytest -q \
  tests/integration/postgresql/test_bitrix24_disk_policy_persistence.py::test_postgresql_policy_heads_are_tenant_scoped_immutable_and_cas_fenced \
  tests/integration/postgresql/test_initial_tenancy_migration.py::test_initial_schema_has_named_tenancy_constraints_and_postgresql_types \
  tests/integration/postgresql/test_initial_tenancy_migration.py::test_governed_reprocessing_catalog_has_exact_tenant_foreign_keys \
  tests/integration/postgresql/test_initial_tenancy_migration.py::test_processing_admission_and_selector_history_are_immutable \
  tests/integration/postgresql/test_initial_tenancy_migration.py::test_every_retained_revision_upgrades_to_one_final_head \
  tests/integration/postgresql/test_postgresql_unit_of_work.py::test_selector_postgresql_publish_replays_and_serializes_cas \
  tests/integration/postgresql/test_postgresql_unit_of_work.py::test_selector_migration_rollback_preserves_existing_v1_acceptance \
  tests/integration/postgresql/test_postgresql_unit_of_work.py::test_postgresql_policy_change_wins_before_acceptance_and_stale_evidence_fails_closed \
  tests/integration/postgresql/test_postgresql_unit_of_work.py::test_postgresql_acceptance_locks_authoritative_policy_until_commit \
  tests/integration/postgresql/test_postgresql_unit_of_work.py::test_postgresql_admission_change_wins_before_acceptance_and_fails_closed
UV_CACHE_DIR=/private/tmp/spine-uv-cache uv run pytest -q
UV_CACHE_DIR=/private/tmp/spine-uv-cache uv run python -m compileall -q src tests
```

The PostgreSQL command must use the repository test harness. Failure to start its
isolated database is a failed prerequisite, not a skip or a passing result.

## Qualification boundary

The evidence covers root/scope admission, S1 grant revalidation, trusted receipt
verification, selector compatibility, v1/v2 interpretation, canonical retries
and races, PostgreSQL constraints and migration history, opaque exclusion, and
process-only reprocessing. The exact node mapping is executable in
`tests/qualification/test_issue729_bitrix24_source_admission.py`.

The following remain outside this offline boundary: live provider behavior,
production secret resolution, concrete production roots and formats, deployment
activation, physical deletion, and execution of #679. The reader-first v2 writer
fence remains in place; this qualification does not authorize its removal.
