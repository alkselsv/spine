# Issue #1042 T5 PostgreSQL qualification v1

Status: **PARTIAL / NOT_VERIFIED**. The disposition/email-sync behavior tests
below ran against code commit `e949b79c1c99392cfe71e013bc7b6eed9c974a03`;
migration/readiness follow-up tests ran against `7880ed2d` on PR #1057. This
report does not qualify complete PostgreSQL lineage erasure and does not
authorize merge or Gmail activation.

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
| `email_message_artifacts`, `email_attachment_artifacts`, `email_attachment_processing_receipts` | NOT_VERIFIED | These artifact and processing rows are not in migration 113's direct deletion set; no physical-absence scenario qualifies them. |
| `parsed_documents`, `processing_receipts`, `source_processing_admissions`, process-only lineage | NOT_VERIFIED | No complete cleanup/absence test. |
| `source_projection_references`, projection receipts/runs, snapshot members, current projection pointers, citation/context evidence | NOT_VERIFIED | PostgreSQL publication and retrieval lineage cleanup is not implemented/qualified. External projection/index/cache cleanup belongs to T7. |
| `audit_events`, `operational_correlation_records`, `operational_metric_events`, diagnostics | NOT_VERIFIED | Exact persisted-field allowlist, source-linked removal, and 365-day audit expiry are not qualified. |
| General `outbox_intents`, `outbox_deliveries`, idempotency receipts, ingestion command/retry records | NOT_VERIFIED | Selected email state-observation outbox events are covered; source-bound general producer/consumer identities and leased deliveries are not. |
| Other source-keyed tables in the catalog snapshot | NOT_VERIFIED | The minimum uncovered list is below; ownership and exact Gmail reachability require current catalog and payload-contract review. |

The reused matrix, `issue-1042-canonical-lineage-dependency-matrix.json`, is a
catalog snapshot from PostgreSQL 17.6 at migration `20270113_112`, not the
current head. Its 348 selected fields and 193 FK edges are an inventory input,
not evidence for migrations 113–114 or an exhaustive arbitrary-JSONB review.
Comparing its source-object/revision-keyed tables with migration 113's direct
target list identifies at least these uncovered tables:

```text
canonical_boundary_members
crm_relationship_claims
current_disclosure_holds
disclosure_hold_releases
disclosure_hold_source_revisions
disclosure_hold_targets
disclosure_holds
drive_lifecycle_decisions
email_attachment_artifacts
email_attachment_processing_receipts
email_message_artifacts
google_sheets_absence_candidates
google_sheets_removal_decisions
ingestion_run_source_bindings
parsed_documents
private_asr_artifacts
private_asr_report_sources
private_asr_source_fences
process_only_operation_lineages
processing_receipts
projection_drifts
projection_snapshot_members
source_processing_admissions
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
  `7880ed2d` follow-up and report commits is NOT_VERIFIED until their exact-head
  checks finish.
- External object storage/staging (T6), projections/indexes/caches (T7), keys,
  WAL, replicas, backups and restore (T8–T11) are NOT_VERIFIED here and must not
  be represented as PostgreSQL PASS.
- Legal/Data/Security decisions and external Google qualification remain
  unresolved. Issue #876 Option C NO-GO remains effective.

## Result

T5 PostgreSQL is **PARTIAL / NOT_VERIFIED**. Keep PR #1057 Draft and issue #1042
open. Keep parent #995 open. This report does not assert that prohibited
message-resolving references are absent across PostgreSQL.
