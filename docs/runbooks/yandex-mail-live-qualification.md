# Yandex Mail live qualification (offline preparation)

This runbook prepares evidence for a future, separately authorized isolated
qualification. This repository change performs no Yandex connection. Production
enrollment and generalization are prohibited. The application gate is
`yandex-mail-live-qualification-preflight:v1`; it must run before constructing
any IMAP connector or session. CI uses only an injected fake provider port.

Use opaque references and synthetic records only. Never put secrets, mailbox
addresses, folder names, message metadata or content in these artifacts.
All `*_ref` and evidence `subject_refs` values must be UUIDs representing opaque
references; they must never contain literal names or provider data.

## Synthetic fixture example

```yaml
schema: yandex-mail-live-fixture-manifest:v1
workspace_id: 00000000-0000-4000-8000-000000000001
environment_id: 00000000-0000-4000-8000-000000000002
connection: {id: 00000000-0000-4000-8000-000000000003, version_id: 00000000-0000-4000-8000-000000000004, configuration_generation: 7, authorization_generation: 9}
mailbox_ref: 65d7a6f0-7428-5b01-8918-3a98a08112bc
included_folder_refs: [9dcb8c11-84e5-5a06-9cdb-6d71f743e872]
excluded_folder_refs: [3f7c8f12-b8bf-5d63-8a82-15c45196da7c]
purpose_ref: e956d22c-2b03-5654-913c-2a4dcfae8149
oauth_app_ref: 87070ca7-0e15-567a-9408-8ccbb61e0bc8
oauth_account_ref: eb190edb-2082-50a5-86c2-a398786ba60d
oauth_grant_ref: 47aec850-1977-508c-94e6-2375e82b65aa
operator_ref: 74868f7d-1604-5560-8442-6dd3b061e49e
backup_operator_ref: b10a03e4-8bea-5470-bb37-27186d7cad09
ops_profile: email-sync-ops:v1
```

The UUIDs above are synthetic labels only and identify no real account,
workspace, mailbox or person.

## Execution authorization template

```yaml
schema: yandex-mail-live-execution-authorization:v1
authorization_ref: <opaque UUID>
manifest_digest: <sha256>
workspace_id: <UUID>
environment_id: <UUID>
connection_id: <UUID>
connection_version_id: <UUID>
configuration_generation: <int>
authorization_generation: <int>
approved_operation_profile: yandex-mail-isolated-qualification:v1
approved_window_start: <timestamp>
approved_window_end: <timestamp>
approver_role: Project Owner
approver_ref: <opaque UUID>
decision: APPROVED | NOT_APPROVED
conditions: [<safe condition code>]
```

## Synthetic environment manifest

```yaml
schema: yandex-mail-live-fixture-manifest:v1
workspace_id: <UUID>
environment_id: <UUID>
connection: {id: <UUID>, version_id: <UUID>, configuration_generation: <int>, authorization_generation: <int>}
mailbox_ref: <opaque synthetic organization-owned reference>
mailbox_owner_approval_ref: <opaque ref>
data_owner_approval_ref: <opaque ref>
oauth_app_ref: <opaque ref>
oauth_account_subject_ref: <opaque ref>
oauth_grant_ref: <opaque ref>
scope: mail:imap_ro
transport: {host_profile: yandex-imap-tls993:v1, port: 993, tls: certificate-validated, auth: XOAUTH2}
included_folder_refs: [<opaque qualified folder ref>]
excluded_folder_refs: [<opaque excluded folder ref>]
purpose_ref: <opaque ref>
synthetic_fixture_release_ref: <opaque ref>
retention_applicability_refs: [<opaque refs covering every artifact class>]
cleanup_plan_ref: <opaque ref>
ops_profile: email-sync-ops:v1
operator_ref: <opaque ref>
backup_operator_ref: <opaque ref>
```

Required attributable evidence keys are mailbox owner, data owner, security,
legal/compliance, operations, OAuth app/account/grant, corporate account and
agreement applicability, exact scope, TLS/XOAUTH2, retention, cleanup/revocation
and incident contacts. A reference without a verified evidence decision does
not satisfy a gate.

## Redacted qualification report

```yaml
schema: yandex-mail-live-qualification-report:v1
manifest_digest: <sha256>
authorization_ref: <opaque ref>
started_at: <timestamp>
finished_at: <timestamp>
criteria: [{id: <safe id>, result: PASS | FAIL | NOT_VERIFIED, evidence_ref: <opaque ref>, safe_code: <code>}]
cleanup_status: PASS | FAIL | NOT_VERIFIED
revocation_status: PASS | FAIL | NOT_VERIFIED
production_generalization: PROHIBITED
contains_mailbox_content: false
```

## Cleanup and revocation evidence

```yaml
schema: yandex-mail-live-cleanup-evidence:v1
manifest_digest: <sha256>
connection_suspended_at: <timestamp>
grant_revocation_evidence_ref: <opaque ref>
stores_checked: [originals, parsed, attachments, projections, caches, staging, replicas, backups, diagnostics]
store_results: [{store: <safe class>, result: CLEAN | NOT_APPLICABLE | FAIL | NOT_VERIFIED, evidence_ref: <opaque ref>}]
verified_by_ref: <opaque ref>
verified_at: <timestamp>
```

## Gate and operational profile

Decisions are deterministic: malformed/protected data or prohibited scope is
`DENY`; missing approval, binding, scope, retention, cleanup or one-time run
authorization is `NOT_APPROVED`; unverified provider or corporate-agreement
facts are `NOT_VERIFIED`. Only a current authorization matching the exact
manifest digest and execution window returns `VERIFIED` and permits the injected
provider port. Recheck workspace, environment, account, connection and both
generations immediately before the run.

Permit certificate-validated TLS 993, XOAUTH2, `mail:imap_ro`, EXAMINE and
non-mutating PEEK reads only. Deny passwords, app passwords, POP3/SMTP,
`mail:imap_full`, STORE/COPY/MOVE/EXPUNGE/APPEND, expanded scope and Yandex Disk
grant reuse. Pin `email-sync-ops:v1`: one session/mailbox, at most 20 commands
per minute and 2,000 per day; at most 100 commands and 25 message fetches per
attempt; five attempts and 30 minutes per logical operation; 5-second connect
and TLS timeouts, 30-second command timeout, 10-minute attempt limit, 20 MiB
raw MIME guard, plus the approved parsing, attachment, staging, cleanup limits
and alerts. Additional pinned parser/staging bounds are 10 MiB per attachment,
20 attachments per message, 30 MiB archives at a 20:1 expansion ratio, archive
depth 2, 2 MiB extracted text, 2 CPU seconds and 128 MiB per parser worker,
parser/attachment concurrency 2/1, one hour staging TTL with 15-minute scans,
256 MiB per attempt and 1 GiB per environment, five cleanup retries and ten
minutes to verify cleanup. These are Spine safety bounds, not claims about
provider limits. Approved cleanup alerts fire at 15 minutes (warning), one hour,
cap breach or failed backup cleanup (critical).

The run operator verifies connection suspension, provider grant revocation and
local token deletion separately, then checks all stores and backup disposition.
No credentials, application-password exception, live execution or production
enrollment are authorized by this preparation.
