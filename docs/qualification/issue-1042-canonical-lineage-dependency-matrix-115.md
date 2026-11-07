# Issue #1042 current PostgreSQL lineage inventory — migration 115

Status: **INVENTORY CURRENT; T5 DISPOSITION PARTIAL / NOT_VERIFIED**.

Captured from a newly provisioned disposable PostgreSQL 17.6 database after a
clean migration to Alembic head `20270116_115`. The capture contains schema
metadata only; test rows and credentials were synthetic. It is not based on the
historical migration-112 snapshot. The complete current catalog, field names and
types, constraints, RLS policies, triggers, executor targets, privileges and
function ownership are in
[issue-1042-canonical-lineage-dependency-matrix-115.json](issue-1042-canonical-lineage-dependency-matrix-115.json).

## Catalog dimensions

- 175 PostgreSQL tables, 2,240 columns, 1,237 constraints, 176 triggers and 381
  RLS policies were captured.
- 139 tables are marked candidate/relevant because of a source/revision key,
  JSONB payload, message/digest/correlation/operation field, operational role or
  current executor target. The JSON snapshot lists every table; it includes all
  fields for the candidates and all catalog constraints.
- The executor currently has 35 closed target tables. A target-list entry is not
  by itself a PASS: physical absence is explicitly tested only for selected
  stores.
- Tables and data outside PostgreSQL (object versions/staging, external
  projections and caches, WAL, replicas and backups) are excluded.

## Attachment and processing field classification

| Table | Source-bearing fields and indirect edges | Current executor behavior | Evidence/status |
|---|---|---|---|
| `email_message_artifacts` | `source_object_id`, `source_revision_id`, `canonical_acceptance_id`, `artifact_digest`, command identities/digests, `record` JSONB | Exact source-scoped delete before canonical acceptance | Synthetic direct SQL absence tested; PARTIAL |
| `email_attachment_artifacts` | Source/revision and acceptance IDs, MIME part, content/artifact hashes, `record` JSONB; FK to revision and acceptance | Exact source-scoped delete after attachment processing receipts | A preserved B and other-workspace C tested; PARTIAL |
| `email_attachment_processing_receipts` | Parent attachment FK; optional child source/revision, child processing receipt and parsed artifact IDs; hashes and `record` JSONB | Delete receipt when parent artifact or child source is the target; preserve other source's artifact | Cross-source A→B and B→A references tested; PARTIAL |
| `parsed_documents` | Source/revision, source/original/artifact digests, parser config, command identities; FK to source revision | Exact source-scoped delete after segments | Synthetic direct SQL absence tested; PARTIAL |
| `parsed_document_segments` | Indirect `artifact_id` FK, segment text/digests, locator and heading JSONB | Delete through source-owned parsed document before parent | Synthetic direct SQL absence tested; PARTIAL |
| `processing_receipts` | Source/revision, input/output digests, warning/failure UUID arrays, command/idempotency IDs; FKs to run item, binding, optional process-only lineage and source revision | In executor target list; ordered before process-only lineage | Not yet directly populated/absenced in integration test; NOT_VERIFIED |
| `process_only_operation_lineages` | Source/revision; operation, admission, acceptance, lifecycle, retention, receipt, audit and idempotency references | Exact source-scoped delete after child processing receipts | No direct populated-row absence test yet; NOT_VERIFIED |
| `source_processing_admissions` | Source/revision, provider authority generations, policy IDs/digests, original receipt/verifier refs, audit/idempotency refs and JSONB | Exact source-scoped delete after process-only rows | No direct populated-row absence test yet; NOT_VERIFIED |
| `ingestion_run_source_bindings` | Source/revision, original receipt and idempotency refs, provider-bound `original_reference` JSONB; FK to source revision | Not an executor target. Source FK can prevent canonical deletion | NOT_VERIFIED; must resolve safely with shared ingestion-run item semantics |
| `ingestion_run_sources` and per-item warnings/failures/transitions | Per-item input identity/digest and indirect binding/receipt/diagnostic references | Not in target list | NOT_VERIFIED; deleting a shared run is prohibited; exact per-source item handling needed |

Attachment-derived structured artifacts remain **not approved by default** under
ADR-0061. The change removes message-bound PostgreSQL records as part of source
disposition; it does not authorize storing or processing new structured
attachment artifacts. Attachment bytes and staging are T6. External index/cache
cleanup is T7.

## Other direct source-keyed records

The JSON field inventory assigns every candidate table `PARTIAL` when it is in
the current executor target set and `NOT_VERIFIED` otherwise, with exact fields,
FKs, triggers, policies and table scope. Direct source-keyed tables outside the
35-target executor list include:

- shared/publication boundary and projection records:
  `canonical_boundary_members`, `source_projection_references`,
  `projection_snapshot_members`, `projection_drifts`;
- operational ingestion references:
  `ingestion_run_source_bindings` and its indirectly linked run-item records;
- source policy/lifecycle/legal-hold evidence whose ownership and approved
  disposition require explicit contract review:
  `current_disclosure_holds`, `disclosure_hold_releases`,
  `disclosure_hold_source_revisions`, `disclosure_hold_targets`, `disclosure_holds`;
- other-provider records that must not be deleted as Gmail:
  CRM relationships, Drive and Sheets lifecycle records, private ASR records,
  transcription jobs/artifacts/publications/reviews;
- additional source-bound security and verification records:
  `source_security_domain_bindings` and
  `trusted_content_verification_evidence`.

These rows remain NOT_VERIFIED until an exact source-ownership action, retention
basis, cleanup order and negative test are recorded. A provider-specific row is
not Gmail data merely because it has a source ID.

## Security and migration observation

Migration 115 extends the existing transaction-bound executor without changing
its authority/fence/CAS body. It extends executor-only RLS and DML grants for
exact attachment/processing rows; runtime DELETE is revoked. Existing immutable
triggers are retained and permit only executor DELETE with an exact row image in
the transaction-bound erasure context; UPDATE stays rejected. Migration 115 is
irreversible. Full trigger/function security approval remains outstanding.

## Qualification boundary

The new A/B/C test verifies direct SQL absence for selected message, attachment,
attachment receipt, parsed document and segment rows; it confirms B's independent
artifact/receipt/parsed document survive when A is disposed, confirms C's
workspace data survives, and checks application source resolution for A and B.
It does not yet verify all current target tables, JSONB outside these fixtures,
all reader paths, general processing admission/receipt rows, ingestion run
binding/item cleanup, audit allowlist/expiry, replay expiry or deterministic
concurrency interleavings. Overall PostgreSQL T5 remains **PARTIAL /
NOT_VERIFIED**.
