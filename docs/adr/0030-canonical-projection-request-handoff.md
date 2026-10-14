# Canonical projection request handoff

## Status

Accepted

## Context

The ingestion outbox contains only opaque identities and parser contract
versions. A projection worker must resolve the current canonical source,
processing receipt, projection run, candidate, partition, immutable
configuration and boundary from PostgreSQL before it can invoke a physical
Context Broker adapter. Cognee is a rebuildable projection and cannot become
the authority for any of those identities.

The physical adapter may perform I/O against Cognee, Neo4j and PGVector. A
PostgreSQL read transaction must not remain open across that call. The adapter
result also needs exact parsed-artifact locator evidence before it can be
recorded in the canonical projection ledgers.

## Decision

The existing `projection_request` outbox consumer is composed from three
explicit ports:

1. `PersistenceCanonicalProjectionRequestResolver` reads and validates all
   canonical identities in one tenant-scoped read Unit of Work. It returns a
   provider-neutral `ProjectCommand` context and rejects missing, ambiguous,
   stale, tombstoned or cross-scope state.
2. `ParsedArtifactCanonicalProjectionLocatorResolver` closes its read Unit of
   Work before loading the immutable parsed artifact and mapping parser-owned
   locators to the stable evidence contract.
3. `PersistenceCanonicalProjectionGateway` records the validated adapter
   receipt and source references in a new Unit of Work after physical I/O.

The composition exposes an `OutboxConsumerBinding` for the existing
`projection_request` route. It does not create a dispatcher or a second
orchestration loop. Duplicate delivery relies on the existing command
identity/digest idempotency contract of the physical adapter and canonical
gateway.

## Consequences

- PostgreSQL remains authoritative for lifecycle, tenancy, provenance and
  publication inputs.
- Physical projection is fail-closed when canonical evidence is incomplete and
  cannot observe an active PostgreSQL Unit of Work.
- Locator mapping is versioned and deterministic; projection result persistence
  can validate exact source-to-object evidence without reading provider text.
- The live #212 qualification must exercise this production composition against
  the approved PostgreSQL + Neo4j + PGVector profile before it may report
  `canonical_lifecycle_separation=passed`.
