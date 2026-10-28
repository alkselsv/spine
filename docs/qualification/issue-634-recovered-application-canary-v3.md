# Issue #634 recovered application canary v3

Status: **BLOCKED / NOT_VERIFIED**.

The separately authorized synthetic run restored the verified Issue #509
recovery set into the fresh isolated `spine634canary003` target at commit
`b8f1dee821bc2c455f3487745873e8713ffda116`. Archive integrity, application
ownership and ACLs, forced-RLS posture, PostgreSQL TLS, migrations and normal
runtime readiness passed.

Before any provider or Context Broker call, the exact production profile
repository resolved the server-approved stored reference. The preflight made
zero provider and zero Broker requests. Cognee relational and vector setup was
completed locally before the production projection consumer was invoked.

Exactly one provider-backed production consumer invocation was made. It reached
the approved provider boundary and failed closed during the physical projection
call with a bounded `TimeoutError`. The public safe outcome was
`projection.request.physical_retryable`. This reproduces the provider timeout
from canary v2 after removing its earlier local schema-setup variable. No retry
beyond the profile's bounded internal retry policy and no second consumer
invocation occurred.

The target retained one open projection run and committed no projection receipt,
snapshot, activation, active publication, source projection reference or
reconciliation result. Because no active publication existed, no Context Broker
request or temporary qualification authority was created. Context Bundle count
remained zero.

Cleanup revoked runtime and public connection access, removed the temporary
Cognee database and role, rotated all temporary authentication material, and
stopped the PostgreSQL target. No graph container or graph volume was created.
Raw provider diagnostics and temporary logs were removed after allowlisted safe
outcome extraction. No serving, cutover or customer data was involved.

The remaining blocker is reproducible provider connectivity within the approved
timeout and retry boundary. Normal publication and reconciliation, followed by
the single authorized Context Broker canary and its isolation, policy,
publication-pin and source-revision-lineage checks, remain unverified.
Independent final evidence review is still required before any qualification
claim.
