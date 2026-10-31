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

Issue #662 adds the additive `provider-connection:v2` form at this seam. It
names the provider kind and carries an explicit, unique capability manifest.
Each capability is `supported`, `unsupported` or `unqualified`; only a
supported capability with qualification evidence may be used as a guarantee.
In particular, adapters cannot claim stable identity, incremental discovery or
confirmed deletion from an unsupported or unqualified capability.

The normalized observation at the seam is `external-observation:v2`. Its common
envelope retains the exact connection version, tenant scope, approved resource
scope and authorization/configuration generations. Binary, tabular, structured
and lifecycle representations are discriminated forms. External identity also
records provider-stable, scope-stable, path-scoped or unproven assurance.

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

Google v1 remains an accepted compatibility contract. Explicit mapping preserves
its connection IDs and generations, Drive file IDs, Sheets business-row keys,
opaque canonical identity, revision-bearing bytes and revision-scoped locators.
Both v1 and v2 content observations enter the same existing original upload,
`SourceObject`/`SourceRevision`, admission, ledger and transactional outbox path.
Lifecycle-only v2 observations cannot enter content ingestion; their later
handling must remain behind qualified lifecycle policy rather than fabricate an
empty content revision.

Opt-in Shared Drive qualification adds a preflight before read admission.
`shared_drive_approved` is configuration intent and is not evidence of owner
approval. A trusted external preflight authority must resolve both an approved
decision and a separate live execution authorization, each bound to the exact
workspace, environment, opaque resource-scope reference, Shared Drive ID,
connection, authorization/configuration generations and file/spreadsheet
allowlists. Missing, denied or mismatched evidence refuses the run before
credential resolution or provider calls. Offline and fake-backed reports retain
`NOT_EXERCISED`; only the separately owned live qualification may record provider
exercise evidence.
