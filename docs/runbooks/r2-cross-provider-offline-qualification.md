# R2 cross-provider offline qualification

This qualification runs one synthetic, fake-backed conformance boundary across
Google compatibility, Yandex Disk, Bitrix24 and a temporary mounted filesystem:

`provider admission → normalized observation/relationship → canonical ingestion
→ immutable SourceRevision → transactional projection outbox → authorized
Context Broker retrieval`.

It uses no live provider, credential, customer payload or production database.
The executable reusable harness is
`tests/qualification/cross_provider_conformance.py`; the scenario gate is
`tests/qualification/test_issue677_cross_provider_offline.py`; and the
machine-readable provider/capability/scenario matrix is
`docs/qualification/issue-677-cross-provider-offline.json`.

## Run

From the repository root, run:

```bash
uv run pytest -q \
  tests/qualification/test_issue677_cross_provider_offline.py \
  tests/qualification/test_issue614_google_offline_qualification.py \
  tests/application/test_connector_ingestion.py \
  tests/application/test_provider_read_admission.py \
  tests/application/test_authorized_context_broker.py \
  tests/connectors/test_yandex_disk.py \
  tests/application/test_yandex_disk_reconciliation.py \
  tests/connectors/test_bitrix24_disk.py \
  tests/connectors/test_bitrix24_crm_material.py \
  tests/application/test_bitrix24_crm_material_service.py \
  tests/connectors/files/test_mounted_filesystem.py \
  tests/application/test_mounted_filesystem_reconciliation.py \
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
  tests/application/test_register_associations.py
```

Then run the repository gates from the root `AGENTS.md`.

## Accept

Accept the offline qualification only when the focused command and repository
gates exit zero, every matrix evidence node exists, the Google offline
qualification remains green, and the report contains no credential, signed URL,
protected name, content excerpt or raw provider error.

`PASS` is limited to deterministic offline contracts. Every live integration
remains `NOT_VERIFIED` until the exact per-provider prerequisites in the matrix
are approved and exercised. A missing prerequisite or skipped required test is
`NOT_VERIFIED`, not a pass. A failing contract is `FAIL`.

The Bitrix24 bounded slice has no durable scan/cursor contract, so that matrix
cell is `NOT_APPLICABLE`; it is not represented as an unsupported guarantee.
Structured CRM relationship evidence remains a separately accepted association
to the exact canonical Disk revision and does not grant disclosure or pricing
authority.
