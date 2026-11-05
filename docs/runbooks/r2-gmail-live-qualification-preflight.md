# Gmail live-qualification preflight (offline package)

This package prepares a future, separately authorized isolated Gmail
qualification. It makes no Gmail API calls and does not construct a provider
transport. Current policy is **Option C NO-GO** under #876: any live content
read or retention of Gmail-derived bytes or identifiers is denied. A complete
offline evaluation therefore returns `BLOCKED_TECHNICAL` (or
`NOT_APPROVED` when approval evidence is absent) and always sets
`provider_call_permitted: false`. This package does not authorize a live run,
credentials, consent, or production enrollment.

Executable contract and evaluator:
`src/spine/application/gmail_live_qualification_preflight.py`.
Its schemas reject additional fields, accept only opaque UUID evidence
references, use a fixed Gmail scope and a safe purpose code, and never accept
addresses, message IDs, subjects, bodies, MIME, tokens or provider payloads.

## Synthetic fixture manifest

Replace placeholders only with synthetic UUIDs and opaque evidence UUIDs. The
manifest is immutable and its canonical SHA-256 digest binds the one-time
execution authorization. `evidence` accepts only the criterion keys defined by
the versioned application contract. `state` is `VERIFIED` only after the
referenced external fact has actually been checked; otherwise use
`NOT_VERIFIED`. Missing evidence keys remain missing.

```yaml
schema_version: gmail-live-fixture-manifest:v1
workspace_id: <UUID>
environment_id: <UUID>
connection_id: <UUID>
connection_version_id: <UUID>
configuration_generation: <integer>
authorization_generation: <integer>
scope_generation: <integer>
mailbox_reference: <opaque UUID>
mailbox_organization_owned: true
mailbox_synthetic: true
included_label_references: [<exact opaque UUID>]
excluded_label_references: [<opaque UUID>]
approved_included_label_references: [<exact opaque UUID>]
approved_excluded_label_references: [<opaque UUID>]
scope: https://www.googleapis.com/auth/gmail.readonly
purpose_code: isolated_qualification
execution_window_id: <UUID>
operator_reference: <opaque UUID>
backup_operator_reference: <opaque UUID>
policy_reference: issue-876-option-c:v1
ops_profile: email-sync-ops:v1
ops_limits:
  poll_interval_minutes: 15
  freshness_target_minutes: 30
  reconciliation_days: 7
  initial_snapshot_jitter_hours: 6
  poll_jitter_percent: 20
  max_queue_age_hours: 12
  max_catch_up_age_hours: 24
  manual_resync_interval_minutes: 30
  manual_resyncs_per_day: 3
  deployment_mailboxes: 4
  workspace_mailboxes: 2
  requests_per_mailbox: 1
  deployment_quota_units_per_minute: 60000
  deployment_quota_units_per_day: 2000000
  workspace_quota_units_per_minute: 30000
  workspace_quota_units_per_day: 1500000
  mailbox_quota_units_per_minute: 500
  mailbox_quota_units_per_day: 10000
  attempts_per_operation: 5
  retry_budget_seconds: 1800
  retry_backoff_min_seconds: 1
  retry_backoff_max_seconds: 60
  mailbox_breaker_failures: 5
  mailbox_breaker_window_minutes: 10
  provider_breaker_failure_percent: 20
  provider_breaker_min_attempts: 20
  provider_breaker_window_minutes: 5
  breaker_cooldown_minutes: 5
  connect_timeout_seconds: 5
  gmail_request_timeout_seconds: 15
  page_timeout_seconds: 120
  attempt_timeout_seconds: 600
  initial_snapshot_active_minutes: 30
  worker_heartbeat_seconds: 15
  lease_ttl_seconds: 60
  lease_renewal_seconds: 20
  cancellation_cleanup_seconds: 30
  per_attempt_quota_units: 10000
  messages_per_attempt: 100
  raw_mime_bytes: 0
  parsed_body_bytes: 5242880
  html_bytes: 2097152
  header_bytes: 65536
  mime_parts: 100
  mime_nesting: 10
  attachments_per_message: 10
  attachment_bytes: 10485760
  aggregate_attachment_bytes: 20971520
  decoded_bytes: 31457280
  decompression_ratio: 20
  archive_depth: 2
  extracted_text_bytes: 2097152
  parser_cpu_seconds: 2
  parser_memory_bytes: 134217728
  parser_workers: 2
  attachment_workers: 1
  staging_ttl_seconds: 3600
  staging_scan_seconds: 900
  staging_cleanup_retries: 5
  staging_bytes_per_attempt: 268435456
  staging_bytes_per_environment: 1073741824
  queue_depth_deployment: 500
  queue_depth_workspace: 50
  sync_lag_warning_minutes: 45
  sync_lag_warning_duration_minutes: 15
  sync_lag_critical_minutes: 120
  sync_lag_critical_duration_minutes: 30
  provider_error_warning_percent: 5
  provider_error_warning_minutes: 15
  provider_error_critical_percent: 25
  provider_error_critical_minutes: 10
  mailbox_rate_limit_count: 3
  mailbox_rate_limit_window_minutes: 5
  environment_rate_limit_count: 20
  environment_rate_limit_window_minutes: 10
  reconciliation_warning_mailboxes: 5
  reconciliation_warning_age_hours: 4
  reconciliation_critical_mailboxes: 20
  reconciliation_critical_age_hours: 24
  cleanup_warning_minutes: 15
  cleanup_critical_minutes: 60
plans_to_read_live_content: true
retains_gmail_derived_bytes_or_identifiers: true
evidence:
  mailbox_owner_approval: {reference: <opaque UUID>, state: VERIFIED}
  label_scope_approval: {reference: <opaque UUID>, state: VERIFIED}
  data_owner_approval: {reference: <opaque UUID>, state: VERIFIED}
  security_approval: {reference: <opaque UUID>, state: VERIFIED}
  legal_compliance_approval: {reference: <opaque UUID>, state: VERIFIED}
  operations_approval: {reference: <opaque UUID>, state: VERIFIED}
  oauth_project_ownership: {reference: <opaque UUID>, state: VERIFIED}
  oauth_app_ownership: {reference: <opaque UUID>, state: VERIFIED}
  oauth_grant_scope: {reference: <opaque UUID>, state: VERIFIED}
  restricted_scope_applicability: {reference: <opaque UUID>, state: NOT_VERIFIED}
  restricted_scope_result: {reference: <opaque UUID>, state: NOT_VERIFIED}
  google_quota_regime: {reference: <opaque UUID>, state: NOT_VERIFIED}
  workspace_admin_consent_authority: {reference: <opaque UUID>, state: VERIFIED}
  retention_originals: {reference: <opaque UUID>, state: NOT_VERIFIED}
  retention_parsed: {reference: <opaque UUID>, state: NOT_VERIFIED}
  retention_attachments: {reference: <opaque UUID>, state: NOT_VERIFIED}
  retention_projections: {reference: <opaque UUID>, state: NOT_VERIFIED}
  retention_indexes: {reference: <opaque UUID>, state: NOT_VERIFIED}
  retention_embeddings: {reference: <opaque UUID>, state: NOT_VERIFIED}
  retention_caches: {reference: <opaque UUID>, state: NOT_VERIFIED}
  retention_staging: {reference: <opaque UUID>, state: NOT_VERIFIED}
  retention_replicas: {reference: <opaque UUID>, state: NOT_VERIFIED}
  retention_backups: {reference: <opaque UUID>, state: NOT_VERIFIED}
  retention_diagnostics: {reference: <opaque UUID>, state: NOT_VERIFIED}
  cleanup_originals: {reference: <opaque UUID>, state: NOT_VERIFIED}
  cleanup_parsed: {reference: <opaque UUID>, state: NOT_VERIFIED}
  cleanup_attachments: {reference: <opaque UUID>, state: NOT_VERIFIED}
  cleanup_projections: {reference: <opaque UUID>, state: NOT_VERIFIED}
  cleanup_indexes: {reference: <opaque UUID>, state: NOT_VERIFIED}
  cleanup_embeddings: {reference: <opaque UUID>, state: NOT_VERIFIED}
  cleanup_caches: {reference: <opaque UUID>, state: NOT_VERIFIED}
  cleanup_staging: {reference: <opaque UUID>, state: NOT_VERIFIED}
  cleanup_replicas: {reference: <opaque UUID>, state: NOT_VERIFIED}
  cleanup_backups: {reference: <opaque UUID>, state: NOT_VERIFIED}
  cleanup_diagnostics: {reference: <opaque UUID>, state: NOT_VERIFIED}
  one_time_execution_authorization: {reference: <opaque UUID>, state: NOT_VERIFIED}
```

## One-time execution authorization template

```yaml
schema_version: gmail-live-execution-authorization:v1
authorization_reference: <opaque UUID>
manifest_digest: <sha256>
workspace_id: <UUID>
environment_id: <UUID>
connection_id: <UUID>
connection_version_id: <UUID>
configuration_generation: <integer>
authorization_generation: <integer>
  scope_generation: <integer>
approved_operation_profile: gmail-isolated-qualification:v1
approved_window_start: <timezone-aware timestamp>
approved_window_end: <timezone-aware timestamp>
approver_role: Project Owner
approver_reference: <opaque UUID>
decision: APPROVED | NOT_APPROVED
conditions: [<safe_condition_code>]
```

The evaluator rejects a missing, expired, mismatched or manifest-unbound
authorization. Even a fully matching authorization cannot override Option C.

## Redacted qualification report

```yaml
schema_version: gmail-live-qualification-report:v1
manifest_digest: <sha256>
authorization_reference: <opaque UUID>
started_at: <timestamp>
finished_at: <timestamp>
criteria: [{id: <safe criterion id>, result: PASS | FAIL | NOT_VERIFIED,
            evidence_reference: <opaque UUID>, safe_reason_code: <code>}]
cleanup_status: PASS | FAIL | NOT_VERIFIED
revocation_status: PASS | FAIL | NOT_VERIFIED
production_generalization: PROHIBITED
contains_mailbox_content: false
```

This is a future report shape, not an output claiming live execution. Preflight
criterion states are `VERIFIED`, `MISSING`, `NOT_APPROVED`, `NOT_VERIFIED` or
`DENY`; none may be promoted to `PASS` without the separately approved live run.

## Cleanup and revocation evidence template

```yaml
schema_version: gmail-live-cleanup-evidence:v1
manifest_digest: <sha256>
connection_suspended_at: <timestamp>
grant_revocation_evidence_reference: <opaque UUID>
stores_checked: [originals, parsed, attachments, projections, indexes,
                 embeddings, caches, staging, replicas, backups, diagnostics]
store_results: [{store: <safe class>, result: CLEAN | NOT_APPLICABLE | FAIL |
                  NOT_VERIFIED, evidence_reference: <opaque UUID>}]
verified_by_reference: <opaque UUID>
verified_at: <timestamp>
```

The runbook records no cleanup as complete until every applicable copy is
covered by verified evidence. Option B stays disabled until a later approved
policy reference and implementation qualification prove complete deletion over
all required storage classes.

## Pinned `email-sync-ops:v1` limits

These are internal approved bounds from #821, not measured deployment controls
or provider quotas. Runtime values may tighten them, never expand them:

- **Cadence:** poll every 15 minutes; p95 freshness objective at most 30 minutes
  (not an SLA); full reconciliation every 7 days; initial snapshot start jitter
  0–6 hours; poll jitter ±20%; normal queue age at most 12 hours; suspend and
  escalate catch-up older than 24 hours; manual resync at most once per 30
  minutes and three times per mailbox per day.
- **Concurrency and budgets:** four active mailboxes/deployment, two/workspace,
  one checkpoint writer and one outstanding Gmail request/mailbox; per attempt
  at most 10,000 quota units and 100 messages; internal deployment budget at
  most 60,000 units/minute and 2,000,000/day, workspace 30,000/minute and
  1,500,000/day, mailbox 500/minute and 10,000/day. These are internal caps,
  not provider limits. Actual project quota/billing regime must be verified;
  token budgets are reduced by the approved 5% deployment and 10% mailbox
  formulas when actual ceilings are known.
- **Retry and time:** five attempts total, at most 30 minutes per logical
  operation across resumes, exponential backoff 1–60 seconds with jitter and
  `Retry-After` as a minimum. Connect/TLS 5 seconds, Gmail request 15 seconds,
  page 120 seconds, attempt 600 seconds, initial snapshot at most 30 minutes
  active time, heartbeat 15 seconds, proposed lease TTL/renewal 60/20 seconds,
  cancellation cleanup 30 seconds. A timeout never proves provider cancellation.
- **MIME and parser:** live Gmail raw MIME accepted/staged bytes are zero under
  Option C; 20 MiB is only a synthetic/future guard. Parsed body/HTML 5/2 MiB;
  headers 64 KiB; at most 100 MIME parts and depth 10; at most 10 attachments,
  each 10 MiB and 20 MiB aggregate; decoded bytes 30 MiB; decompression ratio
  20:1; archive depth 2 and extracted text 2 MiB; parser CPU/memory 2 seconds /
  128 MiB; parser/attachment concurrency 2/1. Unsupported or over-limit input
  is bounded reject/skip.
- **Staging and queue:** offline staging TTL one hour; orphan scan every 15
  minutes; at most five cleanup retries; verify cleanup within 10 minutes;
  warning after 15 minutes and critical after one hour, cap breach or failed
  backup cleanup; 256 MiB/attempt and 1 GiB/environment; queue depth 500
  deployment and 50/workspace. These limits do not authorize Gmail staging.
- **Alerts:** sync lag warning above 45 minutes for 15 minutes, critical above
  120 minutes for 30 minutes; provider errors warning at 5%/15 minutes, critical
  at 25%/10 minutes; rate limits at least 3/mailbox/5 minutes or 20/environment/
  10 minutes; reconciliation warning above five mailboxes or oldest above four
  hours, critical above 20 or oldest above 24 hours. Revocation/scope mismatch
  and credible credential compromise cause immediate NO-GO.
- Provider quota ceilings, project/app/grant, operators, backup contacts,
  consent, retention applicability and external verification remain
  `NOT_VERIFIED` or `NOT_APPROVED` until evidenced. Runtime may lower every
  numeric limit above; expanding one needs separately approved policy and a
  new profile version.

## Offline preflight checklist

- [ ] Schema is exact; only UUID references, fixed scope and safe codes are present.
- [ ] The mailbox is organization-owned and synthetic; exact included and
  excluded label references do not overlap.
- [ ] Workspace, environment, connection/version and both generations match the
  current binding, including the final generation revalidation.
- [ ] Mailbox Owner, Data, Security, Legal/Compliance, Operations, OAuth
  project/app/grant and Workspace administrator/consent evidence is attributable.
- [ ] Google restricted-scope applicability and actual verification result are
  checked; unknown results remain `NOT_VERIFIED`.
- [ ] #876 Option C blocks any live content read, staging or retention. The
  evaluator never permits provider construction or a call.
- [ ] Retention applicability and cleanup evidence have a distinct opaque
  reference for every artifact/storage class above.
- [ ] #821 profile and named operator/backup references are pinned; runtime may
  tighten but not expand its bounds.
- [ ] One-time authorization binds the exact manifest digest, generations and
  current time window.

Run the deterministic contract tests with:

```bash
uv run pytest -q tests/application/test_gmail_live_qualification_preflight.py
```
