---
status: accepted
---

# Keep Bitrix24 OAuth grant authority reference-only and PostgreSQL-canonical

For the offline Bitrix24 OAuth contract accepted in #709 and implemented by
#713, PostgreSQL is the canonical authority for the exact immutable grant
binding, active token-pair reference, token epoch, refresh claim and security
fence. The protected secret store contains immutable complete token-pair
candidates only; it cannot select the active pair. Callback activation commits
the verified candidate reference, binding, receipt and Audit Event together
after admission against the current approved Provider Connection. Refresh uses
a PostgreSQL CAS to switch the pointer and increment the independent token
epoch; an unknown commit is resolved from canonical state and an uncertain
provider response is never blindly retried.

The immutable binding also records the verified closed provider scopes. Reads
are admitted only through the qualified Disk read contract, require the matching
scope and current connection capabilities before secret resolution, and pin
profile plus data calls to one token epoch. CRM remains disabled until its
methods, resources and flow receive a separate approval. A provider-confirmed expired
access token may cause one durable refresh and one complete re-admission; a
second expiry or an uncertain refresh fails closed. Arbitrary REST methods and
CRM/Disk mutations are outside this authority.

Containment advances a scoped security fence without claiming provider
revocation. Recovery requires a separate authority and a fresh, current
Provider Connection/binding; stale workers cannot activate candidates or
release read results. Tokens, application credentials and callback payloads
never enter domain state, PostgreSQL, audit, diagnostics or responses.
Confirmed uninstall is a separate terminal-loss path: it requires independently
authorized, qualified evidence matching the exact portal member, application,
binding and connection version before moving the grant to reauthorization.
Recovery additionally requires a newly available immutable token-pair candidate;
the contained authority's active pair can never be rebound to the successor.

This decision approves only the provider-specific offline boundary. It does
not select a production secret-store backend, approve live OAuth transport,
change `ProviderConnection:v2` or Google v1 semantics, or authorize portal
access, credentials, deployment or production use.
