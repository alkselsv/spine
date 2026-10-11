# Issue #5 identity and request-authorization qualification

Status: implemented (repository qualification; parent issue closes after the
ticket branch is merged)

Issue #74 qualifies the contracts and adapters delivered by #69–#73 against the
canonical AC1–AC30 specification in GitHub Issue #5. The qualification does not
add browser login, identity provisioning, document `AccessPolicy`, Q&A behavior,
or a content-superuser role.

## Qualification outcome

- A synthetic signed RFC 9068 token now has an executable path through OIDC
  discovery/JWKS, FastAPI, the PostgreSQL authorization directory, durable audit,
  trusted persistence context validation, and a protected handler.
- The success-path test proves that audit is committed before handler release.
  Separate denial-path cases prove that foreign Workspace/Environment selectors
  stop before handler execution with one disclosure-safe response.
- In-memory and PostgreSQL directories continue to run the shared contract suite.
- Real PostgreSQL tests cover retained-revision migration, named constraints and
  indexes, forced RLS, least-privilege grants, concurrency, generation changes,
  pool reuse, and two-tenant isolation.
- OIDC discovery, token validation, key rotation, bounded cache behavior, and
  provider failure handling remain deterministic and offline by default.
- FastAPI route inventory, OpenAPI security, explicit public exemptions, local
  development guards, negative disclosure, and legacy response compatibility
  remain executable contracts.

Live-provider qualification: not run. It requires an operator-controlled issuer,
audience, interactive client registration, RFC 9068 access token, and evidence
that client credentials are disabled for that audience. Follow
[`../runbooks/oidc-provider-qualification.md`](../runbooks/oidc-provider-qualification.md).

## AC1–AC30 traceability

Each row names executable evidence rather than an implementation file. PostgreSQL
tests carry the mandatory `postgresql` marker and run against the pinned real
PostgreSQL major.

| Criterion | Executable evidence | Qualified behavior |
| --- | --- | --- |
| AC1 | `tests/auth/test_auth_contracts.py::test_authorized_request_context_has_closed_shape` | Typed contracts are frozen/closed and reject authority-bearing extras. |
| AC2 | `tests/auth/test_architecture.py::test_auth_seam_has_only_inward_framework_independent_dependencies` | Domain/application auth contracts have no framework or adapter imports. |
| AC3 | `tests/contracts/auth/test_authorization_directory_contract.py::test_current_records_resolve_exact_authorization_contract` | Exact alias mapping plus current tenant memberships and role produce authority. |
| AC4 | `tests/integration/postgresql/test_authorization_directory.py::test_concurrent_duplicate_alias_version_has_one_logical_effect` | One current alias cannot atomically map to two canonical identities. |
| AC5 | `tests/integration/postgresql/test_authorization_directory.py::test_postgresql_current_records_resolve_exact_contract` | Active identity, alias, ownership, memberships, role, and generation resolve together. |
| AC6 | `tests/integration/postgresql/test_authorization_directory.py::test_effective_change_advances_generation_and_retains_history` | Revocation fails closed while immutable history and generation advance are retained. |
| AC7 | `tests/contracts/auth/test_authorization_directory_contract.py::test_same_identity_has_strictly_workspace_local_authority`<br>`tests/integration/postgresql/test_authorization_directory.py::test_postgresql_same_identity_is_strictly_tenant_local` | The same contract runs for in-memory and PostgreSQL adapters with strict isolation. |
| AC8 | `tests/integration/postgresql/test_authorization_directory.py::test_authorization_catalog_is_classified_forced_rls_and_least_privilege` | Catalog classification, forced RLS, grants, and resolver privileges are exact. |
| AC9 | `tests/infrastructure/auth/test_readiness.py::test_readiness_rejects_inexact_issuer_or_ineligible_jwks_origin` | Discovery requires the exact issuer and eligible same-origin HTTPS JWKS. |
| AC10 | `tests/infrastructure/auth/test_token_validation.py::test_wrong_issuer_audience_or_client_fails_as_invalid_credential`<br>`tests/infrastructure/auth/test_token_validation.py::test_invalid_time_window_fails_as_invalid_credential` | Header, signature, issuer, audience, client, subject, and time validation fail closed. |
| AC11 | `tests/infrastructure/auth/test_token_validation.py::test_none_and_hmac_algorithms_fail_before_provider_io`<br>`tests/infrastructure/auth/test_hardening.py::test_oversized_header_and_claim_parts_fail_before_provider_io` | ID-token typing, none/HMAC confusion, malformed keys, and oversized inputs are rejected. |
| AC12 | `tests/infrastructure/auth/test_key_cache.py::test_concurrent_unknown_kid_rotation_uses_one_shared_refresh`<br>`tests/infrastructure/auth/test_key_cache.py::test_matching_stale_key_survives_bounded_outage_then_expires` | Rotation performs one bounded refresh and cache freshness/staleness is deterministic. |
| AC13 | `tests/infrastructure/auth/test_readiness.py::test_readiness_fetches_and_validates_discovery_and_jwks` | Provider transport and clock are injected; the default suite needs no network. |
| AC14 | `tests/infrastructure/auth/test_token_validation.py::test_valid_access_token_returns_only_provider_alias_and_configuration_version` | Token roles, groups, scopes, and display claims cannot change authority. |
| AC15 | `tests/api/test_authorization.py::test_missing_or_foreign_scope_returns_safe_403_before_handler` | Workspace/Environment and role resolve before handler or business work. |
| AC16 | `tests/api/test_authorization.py::test_scope_denial_states_share_schema_and_exclude_negative_leak_corpus` | Missing, cross-tenant, disabled, and unprivileged states share one safe schema without fixture leaks. |
| AC17 | `tests/infrastructure/auth/test_request_authorization_composition.py::test_allowed_request_maps_one_exact_request_and_persistence_snapshot` | Request and persistence contexts agree exactly on actor, scope, policy, and trace. |
| AC18 | `tests/infrastructure/auth/test_request_authorization_composition.py::test_request_context_fails_closed_in_another_async_task`<br>`tests/infrastructure/auth/test_request_authorization_composition.py::test_persistence_field_substitution_fails_before_repository_access` | Substitution, foreign issuer, cross-task use, mutation, and reuse fail deterministically. |
| AC19 | `tests/api/test_authorization.py::test_startup_rejects_unclassified_versioned_api_route` | Every protected `/api/v1` route requires one static registered policy. |
| AC20 | `tests/api/test_authorization.py::test_public_exemption_is_not_inherited_by_a_new_route_with_same_name` | Public health exemptions bind exact route objects and cannot be inherited. |
| AC21 | `tests/api/test_authorization.py::test_valid_bearer_resolves_trusted_actor_scope_and_trace` | In-process ASGI success/denial preserves response schema and trace. |
| AC22 | `tests/api/test_authorization.py::test_authentication_and_authorization_failures_are_safe_and_stop_handler` | Auth failures map to stable Structured Errors without raw provider/database detail. |
| AC23 | `tests/integration/postgresql/test_authorization_directory.py::test_signed_oidc_token_executes_handler_after_postgresql_audit` | One trace crosses middleware, OIDC, PostgreSQL authority, durable audit, trusted context, and handler. |
| AC24 | `tests/infrastructure/auth/test_request_authorization_composition.py::test_required_allow_audit_failure_prevents_handler_execution`<br>`tests/infrastructure/auth/test_request_authorization_composition.py::test_required_deny_audit_failure_never_converts_denial_to_allowance` | Required allow-audit failure blocks release; deny-audit failure never permits work. |
| AC25 | `tests/infrastructure/auth/test_local.py::test_local_settings_reject_non_development_deployment_modes`<br>`tests/integration/postgresql/test_authorization_directory.py::test_local_api_uses_postgresql_directory_and_rejects_foreign_scope_before_handler` | Local bearer mode is development-only and uses canonical directory resolution. |
| AC26 | `tests/api/test_authorization.py::test_dependency_override_cannot_supply_actor_role_scope_or_policy` | Headers, body/query data, claims, and dependency overrides cannot mint authority. |
| AC27 | `tests/api/test_diagnostics.py::test_legacy_ask_bodies_remain_compatible_with_trace_headers`<br>`tests/api/test_diagnostics.py::test_legacy_ingest_body_remains_compatible_with_trace_header` | Legacy `/ask` and `/ingest` response bodies remain compatible. |
| AC28 | `tests/infrastructure/auth/test_dependency_contract.py::test_oidc_stack_is_declared_as_direct_runtime_dependencies` | PyJWT/httpx are direct locked dependencies and imports remain offline-safe. |
| AC29 | `tests/integration/postgresql/test_initial_tenancy_migration.py::test_every_retained_revision_upgrades_to_one_final_head`<br>`tests/integration/postgresql/test_authorization_directory.py::test_authorization_catalog_is_classified_forced_rls_and_least_privilege` | Every retained revision reaches one head and catalog/RLS/grant tests run on PostgreSQL. |
| AC30 | `tests/test_identity_authorization_qualification_docs.py::test_live_oidc_runbook_declares_opt_in_command_and_exact_prerequisites`<br>`tests/test_architecture_scaffold.py::TestArchitectureScaffold::test_domain_layer_has_no_framework_imports` | Full gates plus the provider-neutral live handoff and unverified prerequisites are explicit. |

## Negative-disclosure evidence

The shared synthetic corpus is checked independently at every serialization
surface:

- HTTP responses and Diagnostic Events:
  `tests/api/test_authorization.py::test_auth_failure_responses_and_diagnostics_exclude_negative_leak_corpus`;
- Audit Events and trusted contexts:
  `tests/infrastructure/auth/test_request_authorization_composition.py::test_sensitive_request_inputs_do_not_cross_context_or_audit_captures`;
- provider errors, token claims, exceptions, and captured logs:
  `tests/infrastructure/auth/test_hardening.py::test_provider_and_token_secrets_never_escape_errors_or_logging`;
- PostgreSQL schema:
  `tests/integration/postgresql/test_authorization_directory.py::test_schema_contains_no_raw_token_or_arbitrary_claim_storage`.

## Rollout and rollback

Production enables protected `/api/v1` routes only after the exact authentication
configuration version is qualified, identity records are provisioned through the
trusted procedure, migrations are at the supported head, and readiness succeeds.
Rollback disables the new protected routes and returns to the prior application
version without deleting identity or audit history. Database rollback uses a
verified downgrade only; otherwise use a corrective forward migration or restore
a backup. Legacy endpoints remain compatibility paths, not production R1
authorization substitutes.

## Downstream handoff

The identity/request-authorization foundation is ready for the R1 business routes
owned by #3, #24, #26, and #27 after merge. This qualification does not authorize
protected document disclosure: Issue #7 and ADR 0011 still own current
`AccessPolicy`, `read_content`, and `process_content` decisions.
