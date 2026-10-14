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
content-free allowlist. PostgreSQL persistence
and production gateway composition remain Issue #7 responsibilities.
