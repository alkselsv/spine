# Issue #634 recovered application canary v9

Status: **REVIEW_PENDING / VERIFIED_PENDING_INDEPENDENT_REVIEW**.

Run009 used the separately authorized synthetic recovery set on fresh isolated
target `spine634canary009` at commit
`f5d7587b83cb5589a4bdf6a762438f2c20f1156d`. The authorization retained the
run007 provider, budget, request, token, timeout, retry and deadline limits and
expired no later than `2026-10-29T18:00:00Z`. It permitted exactly one
projection consumer, one temporary authority binding after publication, and
one Context Broker request. It did not permit authorization publication,
serving, cutover, customer data or direct canonical/active-pointer edits.

The recovery archive digest, fresh-target posture, PostgreSQL TLS, migration to
revision 71 and runtime readiness passed before provider access. The production
profile-repository preflight passed while provider and Broker counts were zero.
Offline preparation restored the synthetic application state and provisioned
disposable Cognee PostgreSQL and Neo4j stores without entering the provider
path.

The sole production projection consumer used the projection event itself as
diagnostic causation and completed. The normal guarded provider-backed path
produced one receipt, one sealed candidate, one snapshot, one activation, one
active publication, three source projection references and one healthy
reconciliation result. The activation generation and active-pointer CAS version
were both one. A second consumer invocation was not made.

Only after that publication was confirmed, run009 appended one temporary
service authority binding for the exact synthetic workspace/environment and
`answer_question` / `retrieve_context` operation. The sole normal Context
Broker request used the stored server-approved ContextProfileRef and returned
an allowed, complete Context Bundle with three evidence references. The bundle
pin matched the active snapshot and activation. All three evidence references
matched the request scope, canonical source revisions, boundary members and
source projection references. One current policy-decision generation was
retained. No second Broker request was made.

Guaranteed cleanup appended the revoked version of the same authority binding;
the current-authority resolver then returned no authority. Cleanup removed the
temporary Cognee database and role, both task-owned graph resources, all
temporary credentials, scripts and sensitive logs, revoked runtime access and
stopped PostgreSQL. Only the stopped evidence volume remains. No serving or
cutover occurred.

The configured profile enforced the authorized request/token/budget bounds, but
the disposable Cognee usage table contained no independent request/token/cost
records. Independent budget metering therefore remains explicitly unverified.
The execution evidence supports the canary claim; production qualification
remains pending the separately required independent final evidence review.
