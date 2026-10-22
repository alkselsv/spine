# Issue #544 authorization and RLS qualification

Status: **repository evidence complete; disposable PostgreSQL evidence is a
mandatory CI gate; no live deployment qualification is claimed**.

This package verifies the composition boundary for canonical authorization,
qualification target bindings, AccessPolicy decisions and PostgreSQL RLS. It
uses synthetic identities, opaque references and disposable database targets.
It does not provision a live target, resolve a real secret, call an external
provider or exercise backup/restore.

## Acceptance traceability

| Criterion | Evidence | Result |
| --- | --- | --- |
| Allowed identity/scope and wrong workspace/environment, foreign identity, role and alias denial | `tests/integration/postgresql/test_authorization_directory.py`; `tests/contracts/auth/test_authorization_directory_contract.py` | Deterministic and PostgreSQL contract coverage |
| Revocation, stale generation and database-time expiry | `tests/auth/test_qualification_binding_expiry.py`; `tests/integration/postgresql/test_authorization_directory.py` | Deterministic and PostgreSQL contract coverage |
| Independent `read_content` and `process_content` grants | `tests/qualification/test_issue544_qualification.py::test_issue544_read_and_process_grants_are_independent`; `tests/application/test_content_authorization.py` | Passes in the offline suite |
| SOURCE/RESTORE separation and cross-environment isolation | `tests/auth/test_qualification_target_bindings.py`; `tests/integration/postgresql/test_qualification_target_bindings.py` | Deterministic and PostgreSQL contract coverage |
| Independent operator authority, idempotency and changed-input conflict | `tests/application/test_qualification_provisioning.py` | Passes in the offline suite |
| Partial provider/audit failure and safe retry/revoke/cleanup | `tests/application/test_qualification_provisioning.py` | Passes in the offline suite |
| Secret and protected-data disclosure safety | `tests/auth/test_qualification_target_bindings.py`; `tests/application/test_content_authorization.py` | Passes without exposing secret values or protected text |
| Revalidation after revocation | `tests/application/test_content_authorization.py::test_revoked_current_policy_is_denied_on_next_decision_without_cache`; target resolver revocation tests | Passes in the offline suite and PostgreSQL target contract |
| Restricted PostgreSQL composition | `tests/integration/postgresql/test_qualification_target_bindings.py::test_postgresql_issue544_composition_keeps_policy_and_authorization_tables_restricted` | Mandatory CI PostgreSQL gate |

## Verification boundary

The new deterministic composition test proves that an approved target binding
and secret reference do not grant content access by themselves: the current
AccessPolicy must independently grant the requested operation. The PostgreSQL
gate checks, under the non-superuser runtime role, that canonical authorization
and target-binding tables have no direct `SELECT` privilege, while AccessPolicy
tables expose only the current tenant's rows through forced RLS. The exact
authorization and target-binding resolver functions are executable; no generic
qualification-record enumeration is exposed.

The test suite must be run with the repository's pinned PostgreSQL 17
disposable profile. A successful test run is integration evidence only; it is
not approval for a live target or production deployment.
