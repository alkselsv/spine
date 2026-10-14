---
status: accepted
---

# Physical projection artifact validation

Cognee chunk, entity and relation mappings carry a versioned physical-artifact
lineage contract (`v2`) containing source revision, namespace, configuration
digest and stable locator references.  Adapter validation compares those
content-free observations with an expected projection set and emits only safe
diagnostics and repair candidates; it never authorizes retrieval or persists a
canonical `ProjectionValidation` record.

Changing the mapping format requires a new shadow namespace and a complete
rebuild from the expected source-revision set.  The rebuilt namespace must pass
the adapter validator and the applicable retrieval/provenance/ACL regression
evaluation before Issue #7 may use its inputs.  A failed rebuild remains
unpublished; rollback is owned by the publication boundary and creates the
successor snapshot required by ADR 0018 rather than rewinding a provider
pointer.  Existing v1 physical artifacts are not silently upgraded or trusted.
