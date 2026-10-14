---
status: accepted
---

# Immutable ContextBundle handoff

`ContextBundle` is the canonical, replayable handoff from one authorized broker
run/attempt. It is an immutable record, not a cache: its identity includes the
bundle version and digest, while its attempt identity is unique within a
workspace/environment and idempotency key.

The bundle stores only typed, content-free evidence references. Every reference
retains source-revision, locator and digest lineage, and every evidence source
object is paired with the policy decision generations used to authorize it.
The bundle also pins the profile, projection snapshot, activation generation,
canonical boundary, canonical-as-of cut and projection configuration. Domain
validation rejects mixed tenant or snapshot lineage and the persistence seam
returns safe typed failures for unavailable evidence.

PostgreSQL is the source of truth. The Context Graph remains a rebuildable
projection and is never read directly by domain or application contracts. The
in-memory repository implements the same create/resolve semantics for offline
replay tests. Repeating an exact identity returns the original record; changed
content for either immutable identity is a typed conflict. Runtime access is
workspace/environment scoped, forced through RLS, and limited to insert/select
because bundles cannot be updated or deleted.
