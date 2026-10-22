---
status: accepted
---

# Isolated qualification target bindings

Qualification authority is bound to SOURCE or RESTORE through an immutable,
references-only Qualification Target Binding.  The binding pins one current
Qualification Authority Binding to an exact target inventory and scope,
principal alias, external secret reference, database-role reference and existing
Access Policy references; resolving it requires the caller's complete expected
binding digest and never discovers a target from an identity selector.

SOURCE and RESTORE principals, aliases, secret references, database roles,
target inventories and target scopes are disjoint within this bounded
qualification inventory. References may recur in an explicitly versioned,
immutable successor chain for the same target so that rotation retains history;
each successor advances the pinned canonical authority version, making its
predecessor unresolvable. Different roles on one target may share
only its target inventory reference. The RESTORE verifier is an independent
service principal with its own alias, secret, database role and scope; its
database access is read-only and it cannot resolve on SOURCE.
PostgreSQL exposes the canonical records to runtime only through an execute-only,
tenant-scoped resolver guarded by forced RLS; runtime has no table access.

Secret values remain outside canonical state and are returned only by a
provider-neutral External Secret Reference resolver.  Missing, ambiguous,
expired, revoked or scope-mismatched references fail closed.  External cleanup
failure cannot preserve authority because target resolution independently
requires the referenced canonical authority version to remain current, active
and before its hard deadline. PostgreSQL accepts and resolves a target binding
only while every referenced Access Policy version exists in the same Workspace
and Environment; it never creates `read_content` or `process_content` grants.
