# #981 PostgreSQL transactional race matrix

**Status:** synthetic PostgreSQL qualification evidence for the implemented
authority/fence consumers. This matrix is not provider enrollment, a production
approval, or evidence for an external F2 provider.

**Version:** `postgresql-transactional-race-matrix:v1`

**Tested commit:** `c1386903acde42bd1ffaac9e7b1f2a8c1ad9e877`

**Runtime boundary:** tests use the disposable PostgreSQL harness and the
`spine_runtime` role (`NOBYPASSRLS`). The race cases use two independent
PostgreSQL sessions (a two-session barrier), deterministic barriers/clocks and
synthetic identifiers.
There are no live provider calls. The cited focused tests are the evidence
families; this document does not replace their assertions with a replay of
private implementation details.

## A–J scenarios

| ID | Consumer and race | Required ordering / denial | Focused test family |
|---|---|---|---|
| A | Report publication versus AccessPolicy activation | Both compatible orders are serialized: the operation that owns the PostgreSQL linearization point commits; the losing stale operation is denied without a partial report or audit. | `test_postgresql_private_asr_policy_activation_before_commit_denies_report`; `test_postgresql_private_asr_report_commit_precedes_policy_activation` |
| B | Report publication versus source lifecycle/head replacement | Lifecycle and report paths use the same source fence. A replacement that wins first denies publication; a report that commits first remains durable and the later replacement observes the committed state. | `test_postgresql_private_asr_lifecycle_lock_fences_report_commit`; `test_postgresql_private_asr_report_commit_precedes_source_head_replacement`; `test_postgresql_private_asr_source_head_replacement_before_report_denies` |
| C | Report publication versus retention applicability change | A stale retention pointer cannot authorize a payload insert. A report that commits before the retention CAS remains consistent with its exact current terms. | `test_postgresql_private_asr_report_stale_source_retention_denies_before_payload_insert`; `test_postgresql_private_asr_retention_cas_waits_for_report_commit` |
| D | Composite deletion with two source contributors | Authority is all-or-nothing. A stale, held, revoked or tampered contributor rolls back the whole deletion; canonical source lock order works for reversed input order and does not deadlock. | `test_postgresql_private_asr_composite_report_deletion_denies_stale_second_source`; `test_postgresql_private_asr_composite_deletion_serializes_each_source_revocation`; `test_postgresql_private_asr_composite_deletions_with_opposite_source_orders_do_not_deadlock`; `test_postgresql_private_asr_composite_deletion_audit_failure_rolls_back_and_retries_once` |
| E | Workspace, environment, identity and purpose substitution | Cross-tenant, cross-environment, identity and purpose substitutions deny before protected payload access or mutation; valid RLS context remains isolated. | `test_postgresql_private_asr_deletion_scope_and_identity_denials_preserve_artifact`; `test_postgresql_private_asr_authority_rls_grants_and_manifest_immutability`; `test_postgresql_private_asr_runtime_rls_hides_payload_for_wrong_purpose`; `test_postgresql_trusted_manifest_authority_denies_wrong_scope_or_purpose` |
| F | Timeout and cancellation during a fenced transaction | Acquire/authorization/audit/commit cancellation and provider timeout roll back the database unit of work, close it before settlement, and never expose a partial effect. | `test_postgresql_private_asr_cancellation_during_authorization_rolls_back`; `test_postgresql_private_asr_cancellation_before_commit_rolls_back`; `test_postgresql_private_asr_cancellation_after_authorization_before_audit_rolls_back`; `test_postgresql_private_asr_cancellation_during_audit_append_rolls_back`; `test_provider_timeout_cannot_commit_protected_effects` |
| G | Unknown commit acknowledgement and recovery | An uncertain commit is not guessed from a local return value. The canonical idempotency receipt decides: absent rolls back/retries, present replays the stored result, and unresolved provider settlement remains recovery-pending. | `test_postgresql_private_asr_cancellation_after_commit_reconciles_by_idempotency_key`; `test_postgresql_private_asr_process_death_before_commit_rolls_back_visibility`; `test_postgresql_private_asr_process_restart_reconciles_lost_commit_response`; `test_unknown_commit_outcome_keeps_permit_unresolved_for_reconciliation` |
| H | Duplicate command and retry idempotency | Same key and payload produce one logical durable identity across retry and concurrent sessions; same key with a different payload is rejected without a second effect. | `test_postgresql_private_asr_same_key_retry_returns_stable_identity`; `test_postgresql_private_asr_concurrent_same_key_commits_have_one_identity`; `test_postgresql_private_asr_same_key_different_payload_is_rejected` |
| I | Denial diagnostics and payload secrecy | Denials reveal neither existence nor content; cross-scope and invalid-grant paths do not issue payload reads, and diagnostics remain content- and secret-free. | `test_postgresql_private_asr_read_denial_has_no_existence_oracle_or_payload_select`; `test_postgresql_private_asr_read_rejects_cross_scope_before_payload_select`; `test_postgresql_private_asr_invalid_deletion_grant_denies_without_payload_read_or_mutation` |
| J | Unsupported authority and production composition | Missing, unknown, external or cross-scope authority is DENIED. The synthetic fixture is accepted only by the exact test composition and cannot qualify an F2 provider or production operation. | `test_production_composition_denies_unqualified_or_cross_scope_binding`; `test_production_composition_defaults_to_deny_when_authority_is_missing`; `test_production_composition_rejects_unknown_f1_owner_registration`; `test_synthetic_authority_is_available_only_in_exact_test_composition`; `test_postgresql_private_asr_expiration_denies_without_deletion_authority` |

## Reproduction and evidence rules

Run the focused matrix families with the PostgreSQL gate enabled:

```text
uv run pytest -q \
  tests/integration/postgresql/test_postgresql_private_asr_storage.py \
  tests/infrastructure/persistence/test_postgresql_fenced_unit_of_work.py \
  tests/infrastructure/test_private_asr_deletion_fence.py \
  tests/infrastructure/test_commit_fence_composition.py \
  tests/qualification/test_issue981_postgresql_race_matrix.py
```

The exact durable outcomes asserted by the matrix are: one committed report,
tombstone or receipt when the authority wins; zero payload/audit/tombstone
effects after a denied or rolled-back attempt; one logical effect for an
idempotent retry; canonical reconciliation for an unknown outcome; and RLS
denial for an invalid workspace, environment or purpose. Diagnostic assertions
must contain no content or secrets: no protected content, provider payloads,
credential material or secret values.

The matrix qualifies only the PostgreSQL F1 boundaries already implemented by
the source lifecycle, AccessPolicy, retention-applicability and composite
deletion consumers. It does not qualify an external provider, a real recording,
production deletion, legal/retention approval, provider enrollment or any F2
authority. Those remain DENIED and require separate provider- and
environment-specific evidence.
