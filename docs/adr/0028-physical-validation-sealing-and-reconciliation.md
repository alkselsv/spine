---
status: accepted
---

# Physical validation at the canonical projection boundary

The Cognee/ContextProjection adapter emits physical validation observations as
content-free `PhysicalValidationResult` values. Those values do not become
canonical authority by themselves. After the #208 receipt and source-reference
handoff has been accepted, the application validation seam translates the
observation into the #88 `ProjectionValidation` contract.

The translated gate is `physical_artifacts:r1`. Its metrics retain the exact
run, candidate, partition, adapter artifact reference and integrity digest,
SourceRevision lineage, expected/observed counts, safe diagnostics and repair
candidates. A failed physical check is recorded as failed canonical validation
evidence and prevents candidate sealing; the failed evidence is committed so
operators can diagnose the rejection. A candidate cannot be sealed without one
passed physical gate covering every candidate partition, even when another
quality gate passed.

Runtime physical checks are translated into append-only
`ProjectionReconciliationResult` records after activation. Healthy and
degraded outcomes retain the same evidence digest and safe findings, while the
`ProjectionSnapshot` remains immutable. Repair candidates are workflow input
only; this seam never executes physical repair, mutates canonical source or
selects an active pointer.

Validation and reconciliation identities are deterministic from their canonical
lineage and attempt identity. Redelivery with the same evidence replays the
same persistence command; reusing an attempt identity with changed evidence is
an integrity conflict.
