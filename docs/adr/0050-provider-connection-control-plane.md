---
status: accepted
---

# Manage provider connections through approved references

Provider Connection configuration is an administrator control-plane operation,
not an approval operation. `POST /api/v1/source-connections` accepts one existing
approval identity, one logical connection identity, an expected current version
and an idempotency key. The server resolves the immutable approval inside the
trusted Workspace/Environment scope before invoking a provider observer. The
request cannot supply credentials, grants, resource scopes, provider kind,
capability claims or Security/Data Owner approval.

PostgreSQL stores provider-neutral approvals separately from append-only
connection versions. A version contains only opaque credential/grant/resource
references, explicit capability statements, generations and allowlisted safe
health observations. Tenant RLS protects both tables. A unique predecessor and
optimistic expected-version check make concurrent successors explicit conflicts;
the scope-bound command receipt makes a repeated command return the original
version without another provider call. The version and its allowlisted
`command.outcome` Audit Event commit in the same PostgreSQL transaction.

`GET /api/v1/source-connections` returns the current persisted safe observation
through a signed, tenant-bound cursor.
`GET /api/v1/source-connections/{connection_id}` may refresh health through the
provider observer only after current scoped admission. Revoked stored versions
are returned without a provider request. Public responses omit the service
principal and credential/grant references and expose no provider-controlled
names, content or exception text.

The deployable API composes this authority with the deterministic R2 provider
fake. The fake performs no network I/O and returns only qualified capability and
allowlisted health codes; replacing it requires another observer adapter, not an
API or workflow contract change.

## Considered options

Letting the operator submit the complete `ProviderConnection` was rejected
because it would let a transport request claim approved scopes or qualified
capabilities. Reusing mutable Google connection rows was rejected because the
provider-neutral v2 contract is additive and Google v1 remains a compatibility
contract during expand–migrate–contract. Storing raw provider diagnostics was
rejected because names, URLs and response text can disclose protected data or
bearer material.

## Consequences

Provider-specific adapters implement one small observer interface for approved
configuration and current health. The management module owns replay, versioning,
scope checks and safe projection for all providers. Connecting material still
does not publish an AccessPolicy, make content proposal-eligible or create a
`PricingBasis`; those remain separate decisions and canonical paths.
