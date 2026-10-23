---
status: accepted
---

# Physical projection artifact validation

Cognee chunk, entity and relation mappings carry a versioned physical-artifact
lineage contract. `chunk-projection-mapping:v3` contains source revision,
namespace, configuration digest and the complete discriminated parsed-locator
union (physical line, page text, OOXML text and XLSX cell). The retired `v2`
contract accepted physical-line locators only; it is not parsed as `v3` and is
never upgraded in place. Adapter validation compares those
content-free observations with an expected projection set and emits only safe
diagnostics and repair candidates; it never authorizes retrieval or persists a
canonical `ProjectionValidation` record.

Changing the mapping format requires a new shadow namespace and a complete
rebuild from the expected source-revision set. A `v3` rebuild regenerates every
mapping from the immutable parsed artifact; it does not deserialize a `v2`
adapter retry cache. The rebuilt namespace must pass
the adapter validator and the applicable retrieval/provenance/ACL regression
evaluation before Issue #7 may use its inputs.  A failed rebuild remains
unpublished; rollback is owned by the publication boundary and creates the
successor snapshot required by ADR 0018 from the last validated active inputs,
rather than rewinding a provider pointer. Existing `v1` or `v2` physical
artifacts are not silently upgraded or trusted.
