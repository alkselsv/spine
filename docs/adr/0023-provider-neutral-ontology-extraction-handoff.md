---
status: accepted
---

# Provider-neutral ontology extraction handoff

Issue #200 introduces a provider-neutral extraction handoff for the Context
Graph projection seam. Model/provider output is treated as a candidate, not as
a canonical fact.

## Decision

`spine.memory.cognee.extraction.extract_projection` is the policy module for
mapping entity and relation candidates to one pinned ontology and extraction
configuration. It accepts only candidates whose source revision and stable
locator evidence are current, in scope, unambiguous and complete enough for
the candidate. Accepted facts carry workspace/environment scope, source
revision identity, deterministic evidence references, chunk links, typed
endpoints and the pinned configuration digests.

Forbidden, ambiguous, invalid or evidence-incomplete candidates become typed,
content-free rejections. Model conclusions remain separate from accepted
entities and relations. A result digest makes replay of the same candidate set
stable and duplicate candidate identities are handled deterministically.

The physical graph implementation is behind the `ProjectionGraphWriter`
protocol. The policy module does not import Cognee and cannot publish a
projection or change canonical source state.

## Rebuild, rollback and evaluation

A change to the extraction result schema, ontology mapping, evidence rules or
adapter contract increments its explicit version and digest. The projection
worker rebuilds a shadow candidate from the exact canonical boundary and
replays the synthetic extraction suite before a candidate can be validated.
The previous active snapshot remains untouched while the rebuild runs.

Rollback is a PostgreSQL-owned publication operation: it builds and validates
a new successor snapshot at a non-regressing boundary. It never reactivates a
prior snapshot by changing a Cognee alias or mutating an existing manifest.

Regression evaluation runs the pinned baseline fixtures plus the applicable
retrieval/provenance gates for the changed projection version. A failed or
incomplete evaluation keeps the candidate out of active publication and records
the typed degradation; it does not silently fall back to provider output.

## Consequences

- Offline tests can qualify extraction without an LLM, Cognee or a network.
- A future Cognee adapter consumes only `ExtractionResult` and cannot bypass
  ontology, provenance or evidence checks through this seam.
- PostgreSQL-owned ontology/configuration records remain authoritative; the
  baseline definitions used by fixtures are adapter inputs, not persistence
  records.
