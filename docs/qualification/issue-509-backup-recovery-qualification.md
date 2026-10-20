# Issue #509 backup, isolated restore and recovery qualification

Status: **blocked / not verified; production qualification is not granted**.

The metadata-only evidence package is
[`issue-509-backup-recovery-qualification.json`](issue-509-backup-recovery-qualification.json).
It records the repository contract evidence and the exact missing deployment
prerequisites without promoting deterministic tests, documentation or an
unapproved target into a recovery result.

## Decision

The qualification is fail-closed. No deployment-approved backup coordinator,
protected destination, isolated restore target, verifier identity, projection
recovery worker binding or authorized canary was available for this exercise.
No backup or restore was started, and the live source deployment was not
modified. The report therefore records `blocked` and `not_verified` outcomes,
not `pass`.

The existing R1 contracts cover the deterministic paths for coordinated
component evidence, immutable artifact handling, isolated-target validation,
reconciliation, projection recovery and successor publication. Those checks
are supporting contract evidence only; they do not qualify a deployment.

## Acceptance traceability

| Criterion | Status | Safe result |
| --- | --- | --- |
| AC1 coherent fenced backup manifest | blocked | Approved coordinator and backup profile are unavailable. |
| AC2 canonical references, outbox evidence and original bytes | not_verified | No approved physical artifacts or digest/watermark set exists. |
| AC3 isolated restore and trust fence | blocked | No approved empty isolated target or verifier authorization exists. |
| AC4 failure paths fail closed | pass | Existing deterministic contracts cover corruption, mixed-generation, interruption and reconciliation rejection; no live exercise is claimed. |
| AC5 projection rebuild from verified canonical state | not_verified | The deployment-specific recovery worker/provider path was not exercised. |
| AC6 authorized canary and safe diagnostics | not_verified | No authorized canary target or operator identity was supplied. |
| AC7 existing runbooks exercised | not_verified | Runbook structure and deterministic exercises are present; deployment exercise is unavailable. |
| AC8 metadata-only report | pass | The versioned JSON package is checked by the qualification test for closed fields and privacy. |
| AC9 blocked/not-verified semantics | pass | Missing targets and authorization are recorded as blockers, never inferred as success. |

## Operator handoff

When the deployment owner supplies the prerequisites, rerun the existing
procedures in this order: approved coordinated backup, immutable manifest
read-back, isolated restore and reconciliation, projection rebuild/recovery,
then an authorized canary with safe diagnostic correlation. Record only opaque
artifact identities, digests, watermarks, generations, reason codes and audit
references in a new versioned report. Keep the target fenced and untrusted
until every trust, completeness, reconciliation and projection check passes.

The exercise must use a disposable isolated target and must never restore over
the live source. Do not use direct `pg_dump`, `tar`, ad hoc scripts, manual
manifest writes or fake-provider results as substitutes for the coordinated
deployment exercise.

No runbook change was made: review found no concrete procedural defect that can
be corrected safely without the missing deployment bindings. The issue remains
open until the approved physical exercise produces the evidence required by its
Definition of Done.
