# ADR-037: Tenant-scoped operational correlation read model

Status: accepted

## Context

Operators need to explain a safe operational failure without opening a second
run or feedback ledger. O02 metrics, O06 quarantine metadata, canonical #302
Q&A run records and immutable #298 usefulness feedback already own their facts,
but their trusted references are otherwise difficult to follow as one bounded
diagnostic view.

## Decision

Spine stores an insert-only, tenant-scoped correlation projection containing
only allow-listed source identities, version identities, UTC timestamps,
trusted trace/correlation references, run lifecycle outcomes, usefulness
outcomes and O06 safe delivery metadata. The projection is queried by one
trusted reference and follows only references present in the same tenant and
environment.

The projection is not authoritative for runs, feedback, metrics, deliveries,
audit or evidence. Source identities and schema versions remain visible as
lineage. Replayed source observations are deduplicated by a scoped key;
conflicting or schema-incompatible observations are returned as explicit
`incomplete` coverage. Missing source categories are explicit, and no source
data produces `unavailable`. Authorization denial is handled before lookup and
does not disclose whether a reference exists.

No prompts, questions, answers, citations, document text, comments, secrets,
raw provider failures or stack traces enter the projection.

## Consequences

- Operators can follow a safe delivery or metric reference to a canonical run
  outcome when the trusted references connect them.
- Rebuilds can replay source contracts into the projection without changing
  canonical records.
- The read model requires tenant-scoped RLS, insert-only runtime privileges,
  bounded reads and a documented rebuild path when its schema changes.
