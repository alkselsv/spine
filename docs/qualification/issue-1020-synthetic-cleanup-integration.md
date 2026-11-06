# Issue #1020 synthetic cleanup integration qualification

**Qualification boundary:** offline synthetic domain and fake-store tests only.
No provider, mailbox, production cleanup, physical deletion, or erasure claim.

**Tested code head:** `371fd4a7c0392b54e175ade0698c8a1475c8af30`.
The qualification command below was run against this exact commit. The report
is recorded in a later documentation commit; CI must also pass for the final PR
head before merge.

## State-machine mapping

All applicable transitions call `transition_synthetic_cleanup` from
`spine.domain.synthetic_cleanup`; transition logic is not copied into the tests.
The imported #1002 catalog stays at `NOT_RUN` because it describes the earlier
standalone qualification. Results for this integration are recorded here.

| #1002 vector | Executed mapping and result | Qualification |
|---|---|---|
| `partial_delete` | `STORE_RESULT(STORE_A, VERIFIED)` then `STORE_RESULT(STORE_B, FAILED)` → `CLEANUP_INCOMPLETE`, `STORE_FAILED`; fence stays active. | PASS |
| `store_outage` | The state machine has no outage status. Mapped to `STORE_RESULT(STORE_A, FAILED)` → `CLEANUP_INCOMPLETE`, `STORE_FAILED`; no verification. The vector's `STORE_UNAVAILABLE` code is not available at this boundary. | PASS with safe-code mapping |
| `missing_authority` | `CHECK_AUTHORITY(MISSING)` → `BLOCKED`, `AUTHORITY_MISSING`; no dispatch. | PASS |
| `stale_authority` | `CHECK_AUTHORITY(STALE)` → `BLOCKED`, `AUTHORITY_STALE`; no dispatch. | PASS |
| `revoked_authority` | `CHECK_AUTHORITY(REVOKED)` → `BLOCKED`, `AUTHORITY_REVOKED`; no dispatch. | PASS |
| `cross_workspace` | No workspace-mismatch event exists in the pure state machine. The #1002 fake store checks workspace before duplicate replay and returns generic `DENIED`; no second effect or duplicate result is disclosed. Mapping this outcome to a canonical `BLOCKED` job is not applicable at the domain boundary. | PASS for fake-store boundary; state-machine transition NOT_APPLICABLE |
| `duplicate_retry` | Failed store → `RETRY(CURRENT_TEST_ONLY)` → same operation key → verified store receipts. Duplicate receipt replay is unchanged; the fake-store command replay returns `DUPLICATE` after one effect. | PASS |
| `cancellation` | `CANCEL` → `CANCELLED`, `CANCELLED`; late store result preserves `CANCELLED` and returns `INVALID_TRANSITION`. Fence stays active. | PASS with safe-code mapping |
| `unknown_commit` | `COMMIT_UNKNOWN` → `UNKNOWN`, `COMMIT_UNKNOWN`; incomplete receipt stays `UNKNOWN`; revoked authority does not resolve it; complete receipts under current test authority resolve to `VERIFIED_SYNTHETIC`. | PASS |
| `restore_quarantine_replay` | `RESTORE_REQUESTED` keeps quarantine; `RESTORE_REPLAY_SUCCEEDED` clears the model quarantine only after successful replay. Fake restored data remains unreadable because its deletion restriction remains applied. | PASS for synthetic model/fake only |
| `audit_log_redaction` | The audit schema rejects an added `body` field. The state machine has no audit/log event or diagnostic boundary. | PASS for schema; state-machine transition NOT_APPLICABLE |
| `missing_uncovered_store` | The state machine has no uncovered-store event. A result for `STORE_UNCOVERED` is rejected as `INVALID_TRANSITION`; state remains fenced in `CLEANUP_PENDING` and cannot verify. The vector's `CLEANUP_INCOMPLETE` / `STORE_UNCOVERED` pair is unavailable at this boundary. | PASS with safe-code/state mapping |

The state machine emits no logs or protected diagnostic payloads. Fake audit
schema checks reject protected fields. Production logging, workspace
authorization, access-path denial, backup/restore topology and storage coverage
remain `NOT_VERIFIED`; no such runtime boundary is part of these tests.

## Exact pytest nodes

Tested at `371fd4a7c0392b54e175ade0698c8a1475c8af30` with:

```text
uv run pytest -q tests/qualification/test_issue1020_synthetic_cleanup_integration.py tests/qualification/test_issue1002_synthetic_recovery_vectors.py tests/domain/test_synthetic_cleanup.py tests/test_architecture_scaffold.py::ArchitectureScaffoldTests::test_domain_layer_has_no_framework_imports tests/test_architecture_scaffold.py::ArchitectureScaffoldTests::test_domain_layer_has_no_application_or_infrastructure_imports
72 passed in 1.54s
```

The 16 collected #1020 integration nodes were:

```text
tests/qualification/test_issue1020_synthetic_cleanup_integration.py::test_issue1002_authority_vectors_block_at_canonical_state_machine[missing_authority-MISSING-AUTHORITY_MISSING]
tests/qualification/test_issue1020_synthetic_cleanup_integration.py::test_issue1002_authority_vectors_block_at_canonical_state_machine[stale_authority-STALE-AUTHORITY_STALE]
tests/qualification/test_issue1020_synthetic_cleanup_integration.py::test_issue1002_authority_vectors_block_at_canonical_state_machine[revoked_authority-REVOKED-AUTHORITY_REVOKED]
tests/qualification/test_issue1020_synthetic_cleanup_integration.py::test_issue1002_partial_delete_vector_maps_store_failure_to_canonical_code
tests/qualification/test_issue1020_synthetic_cleanup_integration.py::test_issue1002_store_outage_maps_to_failed_and_never_verifies
tests/qualification/test_issue1020_synthetic_cleanup_integration.py::test_issue1002_uncovered_store_vector_is_rejected_by_canonical_machine
tests/qualification/test_issue1020_synthetic_cleanup_integration.py::test_issue1002_retry_and_duplicate_vector_reuses_job_key_and_receipts
tests/qualification/test_issue1020_synthetic_cleanup_integration.py::test_issue1002_duplicate_store_replay_has_one_effect_after_state_machine_retry
tests/qualification/test_issue1020_synthetic_cleanup_integration.py::test_issue1002_cancelled_vector_rejects_late_store_result
tests/qualification/test_issue1020_synthetic_cleanup_integration.py::test_issue1002_unknown_commit_requires_complete_current_authority_receipt
tests/qualification/test_issue1020_synthetic_cleanup_integration.py::test_issue1002_restore_vector_releases_only_after_successful_machine_replay
tests/qualification/test_issue1020_synthetic_cleanup_integration.py::test_issue1002_cross_workspace_duplicate_stays_denied_before_replay_disclosure
tests/qualification/test_issue1020_synthetic_cleanup_integration.py::test_issue1002_audit_redaction_vector_uses_closed_fake_schema
tests/qualification/test_issue1020_synthetic_cleanup_integration.py::test_issue1020_state_machine_and_denied_fake_store_emit_no_diagnostics
tests/qualification/test_issue1020_synthetic_cleanup_integration.py::test_issue1002_vector_catalog_has_explicit_status_and_deny_fences
tests/qualification/test_issue1020_synthetic_cleanup_integration.py::test_issue1002_restore_fake_stays_unreadable_after_restriction_replay
```

Other executed checks:

```text
uv run python -m compileall -q src tests
PASS
git diff --check
PASS
```

The repository-wide `uv run pytest -q` was attempted against the initial test
commit `0fd86122d9462c75b6c894bce660eb1713273090` and interrupted after 10:19:
5,785 passed, 6 skipped, 6 failed. All six failures require the missing
`ffprobe` binary in unrelated ASR v2 fixture validation. One subsequent test was
still running and was interrupted. This is an environment limitation, not a
passing repository-wide suite; the final PR CI result is required before merge.
