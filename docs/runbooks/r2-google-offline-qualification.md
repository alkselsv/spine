# R2 Google connector offline qualification

This qualification exercises the governed Google Drive and Sheets connector
contracts with synthetic provider fakes. It does not use Google credentials,
network calls, customer data or Cognee. The executable traceability matrix is
`tests/qualification/test_issue614_google_offline_qualification.py`; the
metadata-only qualification record is
`docs/qualification/issue-614-google-offline-report.json`.

## Run

From the repository root, run the focused offline suite:

```bash
uv run pytest -q \
  tests/application/test_google_drive_reader.py \
  tests/application/test_google_drive_snapshot.py \
  tests/application/test_google_drive_changes.py \
  tests/application/test_google_drive_reconciliation.py \
  tests/application/test_google_drive_lifecycle.py \
  tests/application/test_google_sheets_reader.py \
  tests/application/test_google_sheets_reconciliation.py \
  tests/application/test_google_sheets_lifecycle.py \
  tests/application/test_google_read_admission.py \
  tests/application/test_google_sync_diagnostics.py \
  tests/application/test_register_associations.py \
  tests/application/test_connector_ingestion.py \
  tests/application/persistence/test_canonical_acceptance.py \
  tests/contracts/persistence/test_canonical_acceptance_contract.py \
  tests/integration/postgresql/test_postgresql_unit_of_work.py \
  tests/integration/postgresql/test_initial_tenancy_migration.py::test_every_retained_revision_upgrades_to_one_final_head \
  tests/qualification/test_issue614_google_offline_qualification.py \
  tests/infrastructure/memory/test_cognee_dependency_contract.py
```

The PostgreSQL contract is exercised when the repository's disposable
PostgreSQL prerequisite is available. A skip means that PostgreSQL behavior is
`NOT_VERIFIED` for that run; it is not an offline pass. The integration suite
must never be pointed at a developer or production database.

## Accept

Accept offline qualification only when the focused command exits zero and all
non-PostgreSQL cases pass. Save the command result with the CI run or PR and
retain the versioned report as its scenario index. If PostgreSQL integration
tests skip, identify the exact skipped prerequisite in the handoff. Any failed
contract is `FAIL`; a missing or skipped prerequisite is `NOT_VERIFIED`.

The offline suite proves reader, reconciliation, canonical persistence,
acceptance and outbox behavior against deterministic fakes and in-memory or
PostgreSQL contract implementations. It does not prove Google service behavior.
The report names the precise live behaviors and owner decisions still required.
Do not start live-provider qualification until Security, Data and Operations
owners approve the scope and isolated target. Never record credential material,
provider request payloads, source content or raw provider errors.
