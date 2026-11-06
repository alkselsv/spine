# Issue #1042 PostgreSQL canonical lineage dependency matrix

Status: **catalog snapshot captured; field-level owner/action review and disposition qualification remain incomplete**.

This inventory was collected from a disposable PostgreSQL 17.6 database after
applying the repository migration chain through `20270113_112`. The companion
JSON file records the exact catalog constraint definitions, selected identity
and provenance columns with PostgreSQL types/nullability, and matching
immutable/source/canonical trigger definitions. It contains schema metadata only;
the database held synthetic setup data and no mailbox data.

## Catalog result

- 193 distinct FK constraints are reachable from the source/canonical lineage
  roots or belong to their reachable referencing tables.
- All 193 report `ON DELETE NO ACTION`; none provides automatic lineage cleanup.
- The closure spans 64 referencing tables. Candidate identity/provenance columns
  cover 114 tables and 348 fields, including JSON records in email tables.
- 125 trigger definitions matching the source/canonical/immutable filter were
  captured; a full trigger inventory for every reachable table still needs
  review.
- Therefore deletion must enumerate dependent rows explicitly and preserve FK,
  RLS and trigger enforcement. A root-row delete or unrestricted cascade cannot
  satisfy the contract.

Exact catalog snapshot: [issue-1042-canonical-lineage-dependency-matrix.json](issue-1042-canonical-lineage-dependency-matrix.json).

## Disposition action and ownership matrix

| Record family | Representative records/fields | Owner / boundary | Required action at Gmail disposition | May survive? |
|---|---|---|---|---|
| Canonical identity/history | `source_objects.external_*`, `source_revisions.*`, reappearance links, `source_revision_provenance.*`, `source_revision_receipt_associations.*` | T5 PostgreSQL canonical store | Delete all exact source/revision rows after cleanup handlers; remove identity, digests, original references and joins | No |
| Acceptance and current selection | `canonical_acceptances` (including predecessors, audit/outbox/idempotency/tombstone refs), `current_source_heads`, `canonical_boundary_members` | T5 canonical; boundary/snapshot data is shared with T7 | Remove exact source membership, acceptance history and current head. A shared immutable boundary/snapshot must be retired/rebuilt by T7 before any related rows can be removed. Keep the workspace/environment generation counter because it is shared and does not identify the source | No source-resolving row; shared non-identifying definitions may remain |
| Admission and source authority | admissions, processing admissions/receipts, access policy versions/grants/heads, lifecycle observations/decisions/heads, retention applicability, disclosure holds, deletion evidence/approvals, tombstone selectors/authorizations/decisions | T2/T4 contracts; T5 erasure executor | Delete source-specific authorities/evidence and their source joins; retain shared policy/issuer/connection definitions only when no deleted-source link or identifying record remains | No source-specific link or evidence |
| Ingestion and operation receipts | ingestion runs/source bindings/transitions, process-only lineages, idempotency receipts, command/result references | T5 canonical PostgreSQL; operation state is coordinator-owned | Delete exact source-bearing receipt and retry mappings after external handlers and canonical erasure; do not retain source ID, request digest, provider identity or resolvable idempotency mapping | No, except bounded open retry state and ≤24h acknowledged response replay |
| Gmail synchronization state | `email_sync_attempts.record`, `email_sync_pages.record/page_digest`, `email_sync_effect_receipts.effect_key`, `email_sync_state_observations.provider_event_digest/subject_reference_digest/record`, Gmail label and relationship evidence | T5 PostgreSQL; checkpoint effect ordering is ADR-0059/T10 | Remove per-message identity and digest. Pages/attempts that combine effects require a qualified whole-page/attempt cleanup or a source-bound schema; never retain hashed effect keys as a substitute identity | No message-resolving fields/digests; checkpoint advancement stays fenced until reconciliation |
| Email and attachment artifacts | `email_message_artifacts`, `email_attachment_artifacts`, `email_attachment_processing_receipts`, child revisions and parse receipts | T5 metadata; T6 object bytes/staging | Delete relational metadata and lineage after T6 confirms original/transient bytes are cleaned | No relational message/part/child join |
| Other source-linked relations | CRM material, register-link claims/acceptances, transcription jobs/artifacts/publications/reviews, process-only records | Owning feature plus T5 relational erasure | Delete only the row whose exact source/revision endpoint is the disposed Gmail source. Remove a link row without deleting the independently active endpoint | Other active source records may remain; no join to disposed source |
| Projections and retrieval boundaries | `source_projection_references`, snapshot members, projection drifts, cached/retrieval evidence and summaries | T7 | T7 removes/rebuilds shared projection state first; T5 then removes all remaining PostgreSQL source joins and current selection references | No source-resolving projection member; unrelated active members may be rebuilt |
| Outbox and delivery | `canonical_acceptances.outbox_event_id`, `outbox_intents.payload/aggregate_id/correlation_id`, `outbox_deliveries` | T5 PostgreSQL | Delete source-specific intent/delivery rows and mapping after dispatch/reconciliation; disposition outbox may carry only random operation ID and safe event type | No payload or correlation join to source |
| Audit and diagnostics | `audit_events.target_id/payload/correlation_id`, source-linked audit FKs, operational correlation/metric records, diagnostics | T5 audit boundary | Remove source-identifying audit/diagnostic rows. Write only the HD1 allowlist in a non-joinable minimal audit record; expire ≤365 days after completion | Only exact HD1 allowlist; no source ID, digest or join key |
| Shared connection/workspace policy | workspace/environment, provider connection and shared approval/issuer/profile definitions | Existing authority owners | Preserve if still shared and not itself message-identifying; do not delete a workspace, mailbox binding or connection just because one message is disposed | Yes, if non-identifying and still referenced by active sources |
| External bytes and recovery copies | object versions, staging/orphans, indexes/cache, keys, WAL/replicas/backups/restore | T6/T7/T8 | Separate verified handlers. T5 must retain `DELETION_PENDING`/incomplete state when their status is unknown | May exist only under their approved cleanup/recovery contracts; never makes overall operation VERIFIED |

## Required transactional order

1. Lock and revalidate current exact deletion authority, workspace/environment,
   source identity and current canonical generation.
2. Commit durable `DELETION_PENDING` fence plus operation/outbox state.
3. Reconcile each registered external store under the same operation identity;
   unknown store or authority revocation leaves the fence active.
4. In a separate authorized PostgreSQL transaction, remove the explicit
   source-specific dependency closure, including all joins and database-side
   copies, with ordinary constraints and RLS enabled.
5. Verify direct table absence and permitted read-path unavailability; commit
   content-free HD1 audit separately from all source identity.
6. Keep response replay state for at most 24 hours after verification, then
   delete operation ID/request digest/mapping. Audit expires within 365 days.

The current schema does not yet implement steps 4–6. The identity-column
selection is a schema-name filter and does not expand every arbitrary JSONB
shape in every reachable table; table owners must review those payload
contracts and complete the per-field action/active-source impact. The inventory
is an implementation input, not a completed dependency matrix, deletion
evidence or a qualification PASS.
