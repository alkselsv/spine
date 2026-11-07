# Issue #1042 T5 PostgreSQL qualification v1

## Current-head addendum — bounded attachment/processing slice

Current inventory: [migration-115 lineage matrix](issue-1042-canonical-lineage-dependency-matrix-115.md)
and [catalog evidence](issue-1042-canonical-lineage-dependency-matrix-115.json).
The catalog was captured from a newly provisioned PostgreSQL 17.6 database
migrated from zero to `20270116_115`; it supersedes migration 112 as current
structural evidence without rewriting that historical snapshot. It enumerates
175 tables, 2,240 columns, 1,237 constraints, 176 triggers and 381 RLS policies.

Migration 115 adds governed deletion for message artifacts, attachment
artifacts and receipts, parsed documents and segments, source-processing
admissions, processing receipts and process-only operation lineage. It keeps
the existing authority/fence/CAS executor contract, adds executor-scoped RLS
and exact transaction-bound immutable-trigger exceptions, and denies runtime
DELETE on the added stores. It does not authorize new attachment-derived
structured artifacts, and it does not complete PostgreSQL ingestion-run
binding/item cleanup.

The new synthetic PostgreSQL A/B/C test directly checks persisted rows after
disposing A: message artifacts, attachment artifacts and receipts, parsed
documents and segments for A are absent; B's independent attachment, receipt
and parsed document remain; C in another workspace remains; application source
resolution denies A and still resolves B. Parent/child cross-source attachment
receipts are removed without deleting the child's independent artifact. This
is selected-store evidence, not a complete catalog absence proof.

Exact executable code tested: `5c14d96f` (`feat: govern attachment lineage
erasure for issue 1042`), on the local rebased PR branch. Tests ran against
disposable PostgreSQL 17.6 with synthetic data only.

| Command | Result |
|---|---:|
| `uv run pytest -q --tb=short --log-level=ERROR tests/integration/postgresql/test_postgresql_source_disposition.py` | 20 passed |
| `uv run pytest -q --tb=short --log-level=ERROR tests/integration/postgresql/test_initial_tenancy_migration.py::test_every_retained_revision_upgrades_to_one_final_head tests/integration/postgresql/test_initial_tenancy_migration.py::test_gmail_message_state_catalog_is_tenant_scoped_and_evidence_is_immutable tests/integration/postgresql/test_initial_tenancy_migration.py::test_runtime_role_is_restricted_non_owner_with_rls_guarded_dml tests/integration/postgresql/test_postgresql_unit_of_work.py::test_postgresql_email_attachment_replay_is_tenant_scoped_and_immutable tests/integration/postgresql/test_postgresql_unit_of_work.py::test_postgresql_parsed_artifacts_use_transaction_local_tenant_scope` | 5 passed |
| `uv run python scripts/qualification/issue1042_postgresql_catalog.py --output /tmp/issue1042-postgresql-catalog-115.json` | Clean disposable migration to `20270116_115`; catalog captured |
| `uv run alembic heads` | `20270116_115` (one head) |
| `uv run python -m compileall -q src tests scripts/qualification` | passed |
| `git diff --check` | passed |

The migration-history test took 12:04 and upgraded from every retained
revision to one final head. The current catalog command ran after the FK-safe
executor target ordering was finalized. GitHub CI is not yet run on this
rebased/new PR head; the prior CI at `cef6678c` failed its R1 job and is stale.

The result remains **PARTIAL / NOT_VERIFIED**: executor membership, current
catalog evidence and selected physical-absence tests do not establish complete
T5 coverage. `processing_receipts`, `process_only_operation_lineages` and
`source_processing_admissions` still lack populated-row absence tests;
`ingestion_run_source_bindings` and per-item ingestion records, other
source-keyed policy/projection/provider records, audit/outbox families,
replay/audit expiry and deterministic concurrency remain unqualified.

Status: **PARTIAL / NOT_VERIFIED**. The disposition/email-sync behavior tests
below ran against code commit `e949b79c1c99392cfe71e013bc7b6eed9c974a03`;
migration/readiness follow-up tests ran against `7880ed2d`, and the page
reconstruction follow-up ran against `99fc73d1` on PR #1057. This report does
not qualify complete PostgreSQL lineage erasure and does not authorize merge or
Gmail activation.

## Tested boundary and commands

The PostgreSQL integration tests used disposable PostgreSQL 17.6 and synthetic
records. No Gmail API, live mailbox, or customer data was used.

| Command | Result |
|---|---:|
| `.venv/bin/pytest -q tests/integration/postgresql/test_postgresql_source_disposition.py` | 19 passed |
| `.venv/bin/pytest -q tests/integration/postgresql/test_email_sync_durability.py` | 38 passed |
| `.venv/bin/pytest -q tests/application/test_email_sync.py` | 18 passed |
| `.venv/bin/pytest -q tests/infrastructure/db/test_readiness.py::test_committed_migration_inventory_has_one_linear_head` | 1 passed |
| `.venv/bin/pytest -q --tb=short tests/integration/postgresql/test_initial_tenancy_migration.py::test_k0_migration_downgrades_and_reupgrades_cleanly` | 1 passed |
| `.venv/bin/pytest -q --tb=short tests/integration/postgresql/test_postgresql_source_disposition.py::test_governed_erasure_preserves_canonical_message_b_on_shared_email_sync_page` | 1 passed on `99fc73d1` |
| `.venv/bin/alembic heads` | `20270115_114` (one head) |
| `python3 -m compileall -q src tests` | passed |
| `git diff --check` | passed |

The shared-page A/B integration case verifies physical removal of message A's
canonical source and selected email-sync page/state lineage while retaining B's
canonical acceptance, effect receipt, state observation and outbox intent. The
rewritten internal page identity is accepted by the trusted persistence reader;
an incoming provider page cannot use the reserved `erased.` prefix.

## PostgreSQL coverage

| Store/table family | Status | Evidence or gap |
|---|---|---|
| `source_objects`, `source_revisions`, provenance, receipt associations, canonical acceptance/current head | PARTIAL | Governed physical deletion and stale/current authority cases are exercised. The suite does not prove every catalog dependency or every persisted field absent. |
| Tombstone authorization, deletion approval, retention applicability, lifecycle and Gmail label/relationship evidence | PARTIAL | Current/revoked authorization and selected exact-source evidence are tested; the complete stale-generation and lifecycle race matrix is not. |
| `email_sync_attempts`, `email_sync_pages`, `email_sync_effect_receipts` | PARTIAL | Migration 114 selectively rewrites a committed shared page, removes A's effect receipt and preserves B. Pending/leased and unknown historical schema variants are not fully qualified. |
| `email_sync_state_observations`, linked outbox intent/delivery | PARTIAL | A/B observation and selected outbox cleanup are checked. General delivery, consumer-acknowledgement and concurrent delivery coverage is incomplete. |
| `email_message_artifacts`, `email_attachment_artifacts`, `email_attachment_processing_receipts` | PARTIAL | Migration 115 adds source-scoped deletion. A synthetic PostgreSQL A/B/C case directly verifies A absence, B attachment/receipt preservation, C cross-workspace preservation, and removal of cross-source parent/child receipts. Other-field and full-reader absence are not proved. |
| `parsed_documents`, `parsed_document_segments` | PARTIAL | Migration 115 deletes source-owned documents and segments; direct SQL absence for A and preservation of B are tested. |
| `processing_receipts`, `source_processing_admissions`, process-only lineage | NOT_VERIFIED | Migration 115 includes these executor targets, but this integration fixture does not populate and prove physical absence for them; ingestion-run binding/item lineage is also unresolved. |
| `source_projection_references`, projection receipts/runs, snapshot members, current projection pointers, citation/context evidence | NOT_VERIFIED | PostgreSQL publication and retrieval lineage cleanup is not implemented/qualified. External projection/index/cache cleanup belongs to T7. |
| `audit_events`, `operational_correlation_records`, `operational_metric_events`, diagnostics | NOT_VERIFIED | Exact persisted-field allowlist, source-linked removal, and 365-day audit expiry are not qualified. |
| General `outbox_intents`, `outbox_deliveries`, idempotency receipts, ingestion command/retry records | NOT_VERIFIED | Selected email state-observation outbox events are covered; source-bound general producer/consumer identities and leased deliveries are not. |
| Other source-keyed tables in the catalog snapshot | NOT_VERIFIED | The minimum uncovered list is below; ownership and exact Gmail reachability require current catalog and payload-contract review. |

The historical matrix, `issue-1042-canonical-lineage-dependency-matrix.json`,
is a PostgreSQL 17.6 catalog snapshot at migration `20270113_112`; it retains
348 selected fields and 193 FK edges for historical reference. Current schema
evidence is `issue-1042-canonical-lineage-dependency-matrix-115.json` and its
Markdown classification. The following current direct source-keyed tables are
not in the executor target set:

```text
canonical_boundary_members
crm_relationship_claims
current_disclosure_holds
disclosure_hold_releases
disclosure_hold_source_revisions
disclosure_hold_targets
disclosure_holds
drive_lifecycle_decisions
google_sheets_absence_candidates
google_sheets_removal_decisions
ingestion_run_source_bindings
private_asr_artifacts
private_asr_report_sources
private_asr_source_fences
projection_drifts
projection_snapshot_members
source_projection_references
source_security_domain_bindings
transcript_publications
transcript_reviews
transcription_artifacts
transcription_jobs
trusted_content_verification_evidence
```

The list is a minimum gap list based on direct source/revision columns. It does
not classify every UUID, provider ID, digest, correlation key, nested JSONB
value, indirect FK path, or adapter-registered store. Those remain
NOT_VERIFIED. Unknown foreign-key dependencies must continue to fail closed.

## Authority, expiry, security and concurrency

| Requirement | Status | Evidence or gap |
|---|---|---|
| Current authorization, persisted revocation, stale operation and lost commit response | PARTIAL | Existing PostgreSQL tests cover these cases and same-operation reconciliation. |
| Stale approval/config/scope/connection/mailbox/retention generations and lifecycle changes | NOT_VERIFIED | Full requested generation and provider-binding matrix is absent. |
| Concurrent head CAS, two workers on one message, concurrent A/B erasure | NOT_VERIFIED | No complete barrier-driven race matrix at the final head. |
| Disposition versus page persistence, checkpoint advancement, outbox delivery and publication | NOT_VERIFIED | A/B sequential shared-page behavior is covered; deterministic concurrent interleavings are not. |
| Multi-table failure, unknown commit and retry fencing | PARTIAL | Lost-response/idempotent retry and selected failure boundaries exist; required concurrent unknown-outcome cases are incomplete. |
| 24-hour response replay and verified-operation identity purge | PARTIAL | Selected scoped expiry behavior exists. Full exact replay boundary, incomplete-fence retention, per-tenant isolation and audit separation are not all qualified. |
| HD1 content-free audit fields and 365-day expiry | NOT_VERIFIED | No exact persisted-field absence and expiry suite. |
| Executor role, RLS, immutable-history and privilege regression | PARTIAL | Existing migration/catalog privilege tests cover the scoped function and selected rows. No independent security approval is recorded. |

## Reviews, CI and remaining qualification

- Standards review of `origin/main...HEAD`: **0 hard findings; 0 judgement-call
  findings**.
- Spec review: **PARTIAL / NOT_VERIFIED** for the gaps above.
- Independent Security approval: **NOT_VERIFIED**; no approval is recorded.
- Full `.venv/bin/pytest -q` was interrupted after 7:16 at 78%: its partial
  summary was 5,994 passed, 10 failed, 6 skipped, and 23 errors. It is not a
  green full-suite result. The run exposed the stale migration inventory and a
  K0 downgrade that crossed irreversible migration 114; the inventory was
  updated and the K0 rollback now runs in a separate disposable database
  through reversible head 112. Other failures in that partial run, including
  the committed #799 report's now-unreachable historical tested SHA, remain
  unresolved or need a clean-tree rerun.
- The isolated 112→88 downgrade and upgrade to 112 pass on PostgreSQL 17.6.
  The separate direct 114 downgrade refusal test is covered by the disposition
  integration suite. The complete clean-chain and historical migration matrix
  has not been rerun at the latest PR head.
- Latest completed GitHub CI belongs to prior head `81399da6`: Python 3.10–3.14
  and web passed; R1 regression gate was still running. CI for the later
  `7880ed2d`, `99fc73d1`, and this report follow-up is NOT_VERIFIED until the
  final exact-head checks finish. At `99fc73d1`, Python 3.10–3.14, web, and R1
  checks had just been queued.
- External object storage/staging (T6), projections/indexes/caches (T7), keys,
  WAL, replicas, backups and restore (T8–T11) are NOT_VERIFIED here and must not
  be represented as PostgreSQL PASS.
- Legal/Data/Security decisions and external Google qualification remain
  unresolved. Issue #876 Option C NO-GO remains effective.

## Result

T5 PostgreSQL is **PARTIAL / NOT_VERIFIED**. Keep PR #1057 Draft and issue #1042
open. Keep parent #995 open. This report does not assert that prohibited
message-resolving references are absent across PostgreSQL.
