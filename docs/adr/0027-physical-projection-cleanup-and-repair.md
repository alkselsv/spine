---
status: accepted
---

# Physical projection cleanup and repair

Cognee projection artifacts are derived state and may be incomplete after a
failed rebuild, interrupted deletion or superseded source revision. Cleanup
and repair therefore run through a workflow-facing recovery coordinator rather
than mutating canonical source or publication records.

Cleanup is addressed by an immutable plan containing workspace, environment,
candidate, configuration, canonical boundary, failed/retired namespace and
content-free physical mappings. The plan is rejected if any mapping belongs to
another workspace, namespace or configuration. Only failed and retired
namespaces are eligible; active, shadow and rebuilding namespaces are not
eligible for cleanup. Deletion is soft and idempotent. A partial physical
failure returns pending artifact identities and a retry resumes those effects
without repeating completed work.

Repair replays the exact immutable projection inputs that produced the expected
artifact: source revision identity, digest, revision number and locator
mappings are not reconstructed from Cognee. Tombstoned, stale or otherwise
non-current source revisions are fenced before a physical write, so a retry
cannot resurrect content. A newer accepted revision must be projected through
the normal successor workflow.

Repair is mapping-version exact. A `chunk-projection-mapping:v2` recovery plan
cannot write into a `v3` candidate or be interpreted as carrying a non-line
locator. Moving from `v2` to `v3` is a full shadow rebuild from canonical parsed
artifacts followed by validation and publication. If validation or regression
evaluation fails, the candidate stays unpublished; rollback publishes a new
successor from the last validated active inputs and never rewinds the active
pointer.

The coordinator and Cognee adapter expose only typed status, counts, stable
identifiers and safe failure codes. Provider exceptions and payloads never cross
the seam. PostgreSQL remains the authority for SourceRevision, tombstones,
projection manifests, active publication and authorization; all cleanup and
repair operations execute outside its UnitOfWork. Failed or repaired physical
state cannot make a namespace eligible for ordinary retrieval by itself.
