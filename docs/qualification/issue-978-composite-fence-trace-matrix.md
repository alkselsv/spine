# #978 Composite deletion fence trace matrix

This matrix records the PostgreSQL-owned evidence for coordinating deletion of
private ASR artifacts with multiple source contributors. The consumer uses the
existing immutable `PrivateArtifactDeletionRequestV2`, which binds the complete
source lineage and one whole-artifact authorization. Source paths and retention
resolution are acquired in canonical source-object/revision order inside the
same PostgreSQL transaction. One audit record and one artifact tombstone commit
with that transaction. Each contributor outcome binds the request digest,
authorization reference, source binding digest and complete source lineage.
External F2 permits remain valid through set acquisition and expire no later
than the request and authorization. Adapters validate permits before commit,
abort them after rollback, and finish them after a known commit. Unknown commits
and post-commit finish failures leave provider permits unresolved for expiry and
provider reconciliation.

| Trace | Order / injected event | Expected durable result | Evidence |
|---|---|---|---|
| Two-source lock order | Reverse the request's source-binding order | Lifecycle locks still use canonical source identity order | `test_postgresql_deletion_fence_acquires_source_locks_in_canonical_order` |
| Source revocation races with deletion | Pause whole-artifact authorization after both source paths are locked; revoke contributor 1 or 2; test normal and reversed input order | Deletion commits before revocation can acquire the source lock; one deletion audit is written; the revocation then completes and tombstones its source and derived report | `test_postgresql_private_asr_composite_deletion_serializes_each_source_revocation` |
| Missing or unsupported authority | Approval/terms source is not explicitly qualified for PostgreSQL F1 and no qualified F2 provider is configured | Deny the whole operation; no deletion audit, payload clear or tombstone from deletion | `test_postgresql_private_asr_expiration_denies_without_deletion_authority`; `test_retention_resolver_requires_explicit_owner_or_synthetic_test_source` |
| Stale contributor | Replace one source's current retention applicability before deletion | Deny before deletion authorization; preserve artifact payload and omit deletion audit | `test_postgresql_private_asr_composite_report_deletion_denies_stale_second_source` |
| Partial audit failure | Fail the audit writer once, then retry with a fresh request | First transaction rolls back; retry writes one audit and tombstones the artifact once | `test_postgresql_private_asr_composite_deletion_audit_failure_rolls_back_and_retries_once` |
| External permit lifecycle | Use a two-source F2 port fixture; fail second acquisition, validation, or post-commit finish | Acquire/validate failure rolls back the audit/tombstone and aborts acquired permits in reverse order; finish failure preserves the known committed deletion for provider expiry and reconciliation | `test_postgresql_private_asr_composite_fence_completes_all_permits_with_transaction` |

F1 is supported only for the named PostgreSQL-owned source lifecycle,
AccessPolicy and retention applicability state described in ADR-060. F2 remains
unavailable in production until a provider has qualification evidence; missing,
external or ambiguous authority therefore keeps deletion denied. The synthetic
F2 fixture proves port behavior only and is not production qualification.
