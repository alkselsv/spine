---
status: accepted
---

# Use pre-mounted read-only scopes for the first network-filesystem source

The first network-filesystem source reads one versioned, pre-mounted, read-only
scope whose mount lifecycle remains outside Spine. This keeps SMB/CIFS
credentials, negotiation and mount authority out of the connector while letting
the existing Provider Connection, canonical ingestion and conservative
reconciliation boundaries govern every observed file.

## Considered options

A direct SMB/CIFS client was rejected for this slice because it would add a
second credential, transport and recovery boundary before the mounted adapter
has been qualified. Treating the whole host mount namespace as one scope was
rejected because it would weaken tenant and connection-version isolation.

## Consequences

Every active connection version receives one canonical absolute leaf root and a
non-privileged read identity; the platform, not Spine, mounts and monitors it.
Filesystem identity remains path-scoped unless a later, separately qualified
backend policy proves stable object identity. Mount loss cannot authorize
deletion. The approved deployment profile, limits, ownership and qualification
cleanup are recorded in
[`r2-mounted-filesystem.md`](../runbooks/r2-mounted-filesystem.md).
