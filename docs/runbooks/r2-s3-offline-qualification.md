# R2 S3 offline qualification

This runbook recomputes the synthetic offline evidence for issue #738. It never
contacts a live provider, changes production policy, activates a production
writer, creates a production tombstone, performs physical cleanup, or executes
#679.

## Prerequisites

- a disposable PostgreSQL database owned by the migration test role;
- the restricted runtime and dispatcher roles created by the test harness;
- `SPINE_POSTGRESQL_TEST_URL` pointing only at that isolated target;
- no provider credentials or customer data in the environment.

Install the declared development dependencies:

```bash
uv sync --extra dev
```

Run the executable traceability contract and deterministic domain/application
matrix:

```bash
uv run pytest -q \
  tests/qualification/test_issue738_s3_offline_qualification.py \
  tests/domain/sources/test_lifecycle.py \
  tests/domain/sources/test_canonical_tombstone_v3.py \
  tests/application/test_connector_ingestion.py \
  tests/application/test_disclosure_holds.py \
  tests/application/test_retention_applicability.py \
  tests/application/test_tombstone_authorization.py \
  tests/application/persistence/test_canonical_acceptance_retry.py \
  tests/application/test_projection_request_resolution.py \
  tests/application/test_authorized_retrieval_scope.py \
  tests/application/test_citation_validation.py \
  tests/application/test_disclosure_revalidation.py \
  tests/application/test_answer_disclosure.py \
  tests/application/test_source_lifecycle.py \
  tests/application/test_yandex_disk_reconciliation.py \
  tests/application/test_google_sheets_lifecycle.py \
  tests/infrastructure/test_source_usage_guard.py \
  tests/infrastructure/persistence/test_canonical_tombstone_v3_schema.py
```

Run the transactional PostgreSQL evidence against the isolated target. These
tests own the RLS, composite-FK, immutable-history, CAS, serialization,
audit/outbox, idempotency, unknown-commit and migration/rollback claims:

```bash
uv run pytest -q \
  tests/integration/postgresql/test_initial_tenancy_migration.py \
  tests/integration/postgresql/test_postgresql_lifecycle_authority.py \
  tests/integration/postgresql/test_postgresql_disclosure_holds.py \
  tests/integration/postgresql/test_postgresql_retention_applicability.py \
  tests/integration/postgresql/test_postgresql_tombstone_authorization.py \
  tests/integration/postgresql/test_postgresql_canonical_tombstone_v3.py \
  tests/integration/postgresql/test_postgresql_unit_of_work.py
```

Run repository gates:

```bash
uv run pytest -q
uv run python -m compileall -q src tests
```

Record exact pass/skip/fail counts and the qualified commit in
`docs/qualification/issue-738-s3-offline-qualification.json`. Any unavailable
isolated-PostgreSQL prerequisite is a failed qualification claim, not a fake
substitute. Live providers and production activation remain explicitly
unverified.
