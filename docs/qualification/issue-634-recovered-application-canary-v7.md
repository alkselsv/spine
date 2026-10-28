# Issue #634 recovered application canary v7

Status: **BLOCKED / NOT_VERIFIED**.

Run007 used the separately authorized synthetic recovery set on fresh target
`spine634canary007`, commit
`e6338687abee5b5eb08fcf9a9ed7b2ae6743ca6f`, and the approved
`cognee-approved-production` profile. The local authorization bounded the
provider destination to `https://api.vsellm.ru/v1`, spend to USD 1.00,
concurrency to one, provider traffic to 1000 requests, and execution to one
projection consumer invocation followed by at most one temporary authority
binding and one Context Broker request. Authorization publication was not
requested.

Restore integrity, PostgreSQL TLS, typed migrations, runtime readiness and the
server-approved profile preflight passed. Two runner attempts failed closed
before the consumer because the recovered runner still contained earlier
authorization deadlines at two nested enforcement boundaries. Both deadlines
were corrected to the run007 hard expiry before any provider call or consumer
invocation.

Exactly one production projection consumer invocation then completed with the
least-privilege Cognee role, `pool_size=5`, `max_overflow=0`, and the shared
pgvector dataset handler. The role retained `CREATEDB=false` and no dataset
database creation was attempted. The canonical target contains one projection
run, receipt, finalized snapshot, activation, active publication and
reconciliation result, plus three source-revision-specific projection
references. This verifies the run006 least-privilege correction and normal
publication path. The run did not have an independent HTTP/token meter, so it
does not claim an exact provider request or token count.

Before creating temporary authority, the exact production Context Broker
composition was checked against the restored authority and policy metadata.
The one-binding path is service-only `noninteractive_processing`: the current
policy already contains the bounded QA service processing grant, and the scope
resolver plus current-authority resolver accept that mode. However, the
production evidence verifier unconditionally invokes
`decide_interactive_processing`, which requires both current human and service
authority. The restored human qualification deadline is expired; its deadline
is immutable and a replacement binding for the same principal scope is
forbidden. Satisfying the verifier would therefore require an additional
authority or policy/canonical mutation outside the authorization.

The run failed closed at that pre-Broker boundary. No temporary authority was
created, no Context Broker request was made, and no Context Bundle was issued.
Consequently run007 is not production qualification evidence for policy,
isolation, publication-pin or source-lineage behavior at the Broker boundary.

Cleanup revoked runtime and public connections, rotated six credentials,
removed the temporary Cognee database and role, stopped the PostgreSQL target,
and retained only its evidence volume. The one graph container and one graph
volume created by the projection were removed, restoring the pre-run graph
inventory. Raw Cognee state, logs and recovered runner material were deleted.
No serving, cutover, customer data, direct canonical edit or active-pointer
edit occurred.

The next implementation step is to make production evidence verification
dispatch through the authorization operation matching the trusted context
mode, with deterministic tests proving interactive and noninteractive paths.
After that change, a fresh authorized canary can create one short-lived service
binding and issue the single Broker request.
