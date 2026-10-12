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
