# Issue #531 — security-owned qualification identity binding

Status: implementation contracts are present; live SOURCE/RESTORE provisioning
has not been run or qualified. The bounded architecture contract was approved
by a project participant in [PR #540](https://github.com/alkselsv/spine/pull/540).
That record is not a formal GitHub review by the Security Owner. No concrete
identity, membership, grant, credential, database role, secret, or target was
created for this evidence package.

## Acceptance traceability

| Criterion | Repository evidence | Boundary |
| --- | --- | --- |
| Bounded operator-owned interface and lifecycle | `src/spine/application/qualification_provisioning.py`; `tests/application/test_qualification_provisioning.py`; approved contract in `docs/specs/issue-531-security-owner-approval-package.md` | Requires an explicitly supplied and independently validated operator channel. It is not an API, startup hook, or live provisioning command. |
| Hard expiry and revocation for human and service authority | ADR-0041; migration `20261119_56_qualification_authority_expiry.py`; `tests/auth/test_qualification_binding_expiry.py`; `tests/integration/postgresql/test_authorization_directory.py` | Database time is authoritative; equality with the deadline denies. |
| Separate SOURCE and RESTORE principals, references, roles, and scopes | ADR-0042; migration `20261120_57_qualification_target_bindings.py`; `src/spine/auth/qualification_targets.py`; `tests/auth/test_qualification_target_bindings.py::test_target_resolver_denies_source_binding_against_restore`; `tests/integration/postgresql/test_qualification_target_bindings.py::test_postgresql_rejects_cross_target_secret_reuse`; `::test_postgresql_rejects_verifier_binding_to_source` | References only; secret values and concrete target inventory are run-specific. The verifier is RESTORE-only and read-only. |
| Positive scope and denial for wrong workspace/environment, role, revoked or foreign authority | `tests/auth/test_in_memory_directory.py::test_in_memory_directory_denies_without_disclosing_failed_check`; `tests/integration/postgresql/test_authorization_directory.py::test_postgresql_same_identity_is_strictly_tenant_local`; `tests/qualification/test_issue544_qualification.py::test_issue544_target_resolution_fails_closed_for_scope_and_authority` | Synthetic identity and disposable PostgreSQL evidence only. |
| Disabled alias, stale generation, and request-selector authority are denied | `tests/auth/test_in_memory_directory.py::test_in_memory_directory_denies_without_disclosing_failed_check`; `tests/integration/postgresql/test_authorization_directory.py::test_active_successor_makes_prior_resolution_generation_stale`; `tests/api/test_authorization.py::test_noncanonical_workspace_selector_is_denied_before_handler` | Token/request selectors do not create canonical authority. |
| AccessPolicy and RLS remain independent; `read_content` and `process_content` stay separate | `tests/qualification/test_issue544_qualification.py::test_issue544_allowed_binding_is_still_limited_by_access_policy`; `::test_issue544_read_and_process_grants_are_independent`; `tests/integration/postgresql/test_qualification_target_bindings.py::test_postgresql_issue544_composition_keeps_policy_and_authorization_tables_restricted`; `docs/qualification/issue-544-authorization.md` | Disposable PostgreSQL verifies forced RLS and restricted table access; policy decisions remain separate from target bindings. |
| Database-time expiry, revocation, idempotency, audit failure, safe retries, and audit correlation/redaction | `tests/integration/postgresql/test_authorization_directory.py::test_database_time_is_the_hard_qualification_deadline` covers human and service at/before/after boundaries; ADR-0041 inventories resolver revalidation for later protected activity/action boundaries; `tests/qualification/test_issue544_qualification.py::test_issue544_revocation_after_admission_denies_the_next_protected_action`; `tests/application/test_qualification_provisioning.py::test_qualification_audit_is_content_free_and_correlated` | Deterministic and disposable PostgreSQL evidence. Secret mismatch and unavailable-secret denial are redacted by `tests/integration/postgresql/test_qualification_target_bindings.py::test_postgresql_target_resolver_redacts_secret_reference_mismatch` and `::test_postgresql_binding_with_missing_secret_denies_without_disclosure`. |
| No unauthorized identities or credentials are created | The service rejects non-operator invocation and validates operator, profile, policy, and approval references before mutation; application tests cover rejection and partial failure | No operation was invoked against a real target. A separate target-specific approval is still required. |

## Verification

Hosted PR CI is the verification gate for repository evidence; its exact commit
and result are available in the PR checks. CI does not establish live target
authorization. Local targeted tests passed (51 deterministic and 55 PostgreSQL integration tests).
The full local suite was interrupted after 3,281 passed and 6 skipped because a
later migration/integration test stopped producing output; that run is not a
full-suite PASS. The PostgreSQL tests use disposable targets and do not prove
live deployment authorization. Live readiness still requires approved SOURCE
and RESTORE inventories, operator and change references, secret-provider
bindings, and independent review of target-bound evidence tied to the exact
target, profile, commit, and evidence digest. This package does not claim those
prerequisites or physical backup/restore qualification.
