---
status: accepted
---

# Keep retention applicability as separate immutable authority

Spine records retention applicability as an immutable, tenant-scoped authority
binding with a compare-and-swap current pointer. A binding targets either one
exact Source Object or one separately approved source class, names the affected
source-bearing artifact classes, and pins an already approved retention policy
by ID, version ID and digest. Applicability evidence and its approving authority
are verified separately from policy approval.

Eligibility queries identify one immutable artifact version. A trusted lineage
resolver, rather than the caller, supplies the complete set of exact Source
Revisions behind originals and derived or mixed artifacts. Every revision must
have one current, effective, still-approved binding. Missing or incomplete
lineage and missing, ambiguous, conflicting, expired or unavailable authority
fail closed.

## Considered options

Reusing source-kind defaults, timestamps or an existing
`retention_eligible` boolean was rejected because none proves an approved policy
or applicability decision. Accepting caller-supplied revision lists was rejected
because a mixed artifact could omit an ineligible contributor. Combining legal
hold with applicability was rejected because a hold constrains physical deletion
but does not grant retention, retrieval or disclosure authority.

## Consequences

PostgreSQL keeps immutable binding history and only the current pointer mutates
through compare-and-swap. Writes, safe audit evidence and outbox intent commit in
one unit of work. Readers revalidate both policy and binding approval and expose
an explicit ineligible reason for suppression or rebuild. This decision defines
no retention duration, tombstone, cleanup workflow or physical-deletion
authority; those remain separate governed capabilities.
