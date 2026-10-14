# ContextProjection result mapping contract

Issue #207 implements the approved `issue15-to-issue7-projection:v1` handoff
without transferring canonical authority to the physical adapter.

`ProjectionResultMappingBridge` accepts the original provider-neutral projection
command and a distinct `AdapterProjectionReceipt`. Before producing Issue #7
inputs it resolves an immutable canonical mapping context through
`CanonicalProjectionGateway` and verifies:

- workspace, environment, run, candidate and partition identity;
- command identity and digest;
- the pinned projection configuration and Canonical Boundary;
- SourceRevision digest, processing receipt and stable-locator provenance;
- the server-selected Security Domain reference/version and the exact derived
  shadow namespace identity;
- the complete tenant, candidate, configuration, boundary, namespace and source
  envelope of every physical effect;
- an exact source mapping for every emitted projected object.

Only open candidates and physical `project`, `update`, `remove` and `rebuild`
operations are accepted. Retrieval stays behind the Context Broker boundary;
sealed candidates reject new receipt mappings.

The handoff contains a `ProjectionReceiptCommand` and immutable
`SourceProjectionReference` values. Adapter diagnostics remain represented by
`AdapterProjectionDiagnostic` at the input boundary and are copied only into the
typed warning/failure fields of the canonical receipt. The bridge does not create
`ProjectionValidation`, seal a candidate, assemble a snapshot, select an active
snapshot, or publish an activation. It introduces no `ProjectionVersion` type.
The canonical reference retains the Security Domain reference directly; its
version remains canonical on the `ProjectionPartition` reached through the
receipt's partition ID, avoiding a second independently mutable copy.

The canonical gateway owns durable replay and conflict handling. The included
`spine.application.testing.DeterministicCanonicalProjectionGateway` is an
offline conformance fake only:
same scoped command identity and digest returns the stored handoff, while a
different digest raises an explicit integrity conflict even if a caller changes
the physical namespace. Adapter implementation metadata has a closed,
content-free allowlist.

Issue #208 adds `PersistenceCanonicalProjectionGateway`, the production
adapter for the Issue #7 seam. It resolves the run-owned candidate, partition,
configuration, publication-eligible ontology and Canonical Boundary from the
canonical repositories. Exact locator evidence comes from an injected
`CanonicalProjectionLocatorResolver`; this resolver completes before the write
Unit of Work opens, so physical ContextProjection or Cognee work cannot be
hidden inside the PostgreSQL transaction.

`build_projection_result_persistence` is the application composition entry
point. It binds and returns the Issue #207 mapping bridge backed by the Issue
#87 repositories; its `map` operation accepts the original `ProjectionCommand`
and the non-authoritative adapter receipt. The injected locator resolver must
resolve stable locators from
canonical parsed-source/evidence records; physical adapter metadata is not an
authority for those identities.

The write Unit of Work records the `ProjectionReceipt` and all
`SourceProjectionReference` values atomically. Duplicate delivery with the same
identity and digest reuses the canonical receipt and references without a
second logical effect, including redelivery under a different adapter
idempotency key. A failed reference write rolls back the complete Unit of Work,
so retry produces one complete logical effect. Reusing an identity with another
digest, changing the
canonical evidence on replay, resolving an unpublished ontology, or supplying
missing, stale, cross-scope or incomplete locator evidence fails closed. The
gateway still has no operation for validation, candidate sealing, snapshot
assembly, activation or publication.

## Physical validation and the #88 boundary

`PhysicalProjectionEvidenceTranslator` consumes an accepted mapping result and
the content-free `PhysicalValidationResult` from the adapter validator. It
checks workspace/environment, namespace, pinned configuration, exact candidate
and SourceRevision lineage before producing the `physical_artifacts:r1`
`ProjectionValidationCommand`. Metrics retain safe counts, stale/tombstone/
missing/duplicate diagnostics, repair candidates and the canonical adapter
artifact reference plus integrity digest.

`PhysicalProjectionEvidenceCoordinator.validate_and_seal` records failed
physical evidence before rejecting sealing and seals only after a passed
physical gate for every candidate partition. The projection repository enforces
the same requirement, so a caller cannot bypass it by invoking the low-level
seal port directly.

After activation, `to_reconciliation_command` creates an append-only
`ProjectionReconciliationCommand` with healthy or degraded physical findings.
It preserves artifact evidence and repair suggestions without changing the
immutable snapshot or executing a repair. Deterministic validation and
reconciliation identities make redelivery idempotent; a changed digest for the
same reconciliation attempt is rejected.
