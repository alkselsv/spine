---
status: accepted
---

# Admit provider reads through versioned references-only connections

External provider reads use a Spine-owned, immutable Provider Connection chain
that binds one Workspace/Environment, trusted service principal, approved
resource-scope references and exact authorization/configuration generations.
Credential and grant values remain behind opaque references, and every read
fails before a provider request unless the current connection version is active
and matches the complete trusted request scope.

## Considered options

Passing Google credentials or provider consent directly to a reader was rejected
because it makes the adapter an unversioned authority and cannot fence rotation,
revocation or stale configuration. Treating Google visibility as an
`AccessPolicy` grant was rejected because provider read authority, Spine content
disclosure and `PricingBasis` authority are independent decisions.

## Consequences

Connection registration and updates are append-only, idempotent and tenant-RLS
protected, with content-free audit evidence. The boundary deliberately selects
no production OAuth or service-account mode; a later composition may resolve an
opaque credential reference only after read admission. Google observations must
still pass through canonical ingestion and AccessPolicy before use or disclosure.
