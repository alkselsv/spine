# Issue #368 R1 operator runbook exercise record

Status: **runbooks published; synthetic and disposable exercises recorded; no
production qualification claimed.** The machine-readable, versioned evidence
index is [`issue-368-r1-runbook-exercises.json`](issue-368-r1-runbook-exercises.json).

## Traceability

| Operator flow | Procedure | Positive evidence | Negative/rejected evidence |
|---|---|---|---|
| O03 coordinated backup | [Backup and evidence review](../runbooks/r1-backup-evidence.md) | Matching independently verified components and immutable artifact replay | Mixed component evidence, lost fence, missing finalized object |
| O04 isolated restore | [Restore and reconciliation](../runbooks/r1-isolated-restore.md) | Offline immutable verified-result replay; disposable PostgreSQL/filesystem restore **not run locally** | Mixed evidence is rejected before restore; corrupt/incomplete paths fail closed |
| O05 projection recovery | [Rebuild, recovery, successor rollback](../runbooks/r1-projection-recovery.md) | Application request/worker recovery and successor rollback; PostgreSQL handoff/publication CAS **not run locally** | Bounded terminal rejection, invalid validation and historical pointer rewind rejection |
| O06 quarantine | [Read-only inspection](../runbooks/r1-quarantine-inspection.md) | Bounded scoped API pages; runtime-role PostgreSQL read-only/RLS inspection **not run locally** | Denied, unavailable and cross-scope cursor checks; PostgreSQL RLS isolation **not run locally** |
| O01/O02/O07 diagnosis | [Telemetry and metrics](../runbooks/r1-telemetry-metrics-diagnosis.md) | Deduplicated metrics and safe trusted-reference correlation | Denied, stale, degraded, unavailable, missing or version-mismatched evidence |

Each listed exercise uses synthetic fixtures or an owned disposable test target.
Its immutable evidence consists of the schema-versioned node IDs and safe result
state in the JSON index, plus the ordinary test-run result in CI. No raw rows,
payloads, credentials, production identifiers, backup destinations or protected
content are committed here.

## Production boundary

The contracts are implemented, but operational bindings remain deployment
responsibilities. No backup/restore CLI or HTTP route, protected destination,
credential, encryption binding, retention enforcement, isolated production
target, independent production verifier, Cognee/provider profile, or external
telemetry exporter is approved by this record. Do not execute an ad hoc restore
or claim production recovery readiness. Complete the owner-approved bindings
and rerun the exact isolated procedures before any production qualification.

## Reproduction

Run the commands under each runbook's **Exercise** section from the repository
root. PostgreSQL exercises require the repository's owned disposable test
database/container; when that prerequisite is unavailable, record the node as
not run rather than interpreting a skip as qualification.
