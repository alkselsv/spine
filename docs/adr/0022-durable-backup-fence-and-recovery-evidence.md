---
status: accepted
---

# Durable backup fence and recovery evidence

Backup coordination uses one deployment-scoped PostgreSQL lease row as the
serialized boundary for ticket admission, fence activation and quiescence.
Fence-controlled mutations receive ordered durable tickets, while the recovery
manifest is immutable evidence containing the cutoff, watermark and verified
PostgreSQL/object-volume identities. Epoch-scoped quiescence evidence is stored
as a separate immutable record. Live lease release state remains separate from
both evidence records; later evidence failures remain outside this coordination
slice and cannot rewrite the immutable manifest.

This separates mutable coordination and takeover state from the historical trust
record, prevents ticket counts from masquerading as quiescence proof, and keeps
Profile B best-effort sets distinct from coherent Profile A recovery points.

Recovery trust is derived from an append-only, deployment-scoped
`recovery_evidence_invalidations` chain. Each record has a contiguous sequence,
the exact predecessor digest, a self digest, the targeted immutable manifest and
an explicit event kind. Invalidations preserve the historical manifest but make
it untrusted. A replacement event is accepted only when it independently names
a coherent replacement manifest plus validation and approval references.

Restore reconciliation consumes copied manifests and chain records in isolation;
it never repairs or mutates the live store. Missing, duplicated, non-contiguous,
mixed-generation, unknown or conflicting evidence produces an indeterminate or
unavailable trust state. Runtime readiness is derived from that state and is
allowed only for a complete currently trusted chain.
