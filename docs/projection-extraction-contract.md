# Provider-neutral projection extraction

Issue #200 adds `spine.memory.cognee.extraction` as the policy seam between
model/provider output and a physical graph adapter. The module has no Cognee
SDK import and can be qualified entirely offline.

`extract_projection(ExtractionBatch)` consumes provider-neutral entity and
relation candidates together with a pinned `OntologyDefinition`,
`ExtractionConfiguration`, source revisions, chunks and stable-locator
evidence. It returns an immutable `ExtractionResult`.

Accepted entities and relations contain:

- the pinned ontology and extraction configuration version/digest;
- workspace and environment scope;
- the exact `SourceRevision` identity;
- deterministic, de-duplicated evidence references sorted by evidence ID;
- entity chunk links and typed relation endpoints.

Candidates with forbidden or ambiguous mappings, invalid endpoints, duplicate
identities, stale/tombstoned revisions, missing evidence, or cross-revision
evidence are not returned as graph facts. They produce content-free typed
`ExtractionRejection` records. A result with both accepted and rejected
candidates is `degraded`; a result without an accepted fact is `rejected`.

`ExtractionConclusion` values remain in the result as model conclusions and are
never promoted into accepted entities or relations. The physical adapter
consumes a `GraphProjectionRequest` that wraps this result with the server-issued
candidate, configuration, canonical boundary and namespace. This keeps graph
lifecycle decisions outside extraction policy and does not import Cognee into
the policy module.
