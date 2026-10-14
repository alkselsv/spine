# PhysicalReadPlan:v1

`PhysicalReadContext:v1` and `PhysicalReadPlan:v1` are the provider-neutral
read-side seam owned by the Context Broker. The plan is constructed from one
trusted `PublicationPin`, an `AuthorizedRetrievalScope`, and immutable
PostgreSQL projection evidence. It is not a publication authority, an access
policy authority, or a physical provider command.

## Safety gates

The builder returns a plan only when it can prove all of the following for
every operation:

- the pin is internally intact and agrees with the authorized scope;
- the finalized snapshot, sealed run/candidate, partition, receipt, snapshot
  member, source reference and projection configuration all belong to the same
  workspace, environment and snapshot lineage;
- the source member is included and is one of the sources in the authorized
  scope;
- the receipt command identity/digest matches the registered partition and has
  no physical failure evidence;
- the namespace is derived by the provider-neutral router and is registered
  with the exact active lifecycle context. A namespace object merely marked
  `ACTIVE` is not sufficient. `SHADOW`, `REBUILDING`, `FAILED` and `RETIRED`
  namespaces are excluded;
- the provider capability reference proves that the exact source object and
  source revision restriction will be enforced before search.

Missing, fabricated, cross-workspace, stale/incompatible or ambiguous lineage
returns `PhysicalReadPlanResolution` with a typed non-ready outcome and no
physical operation. There is no namespace guessing and no post-search ACL
repair. Partition operations are ordered by `partition_identity` and UUID, and
fan-out and result/token limits remain bounded by the server-approved context.
No score scale is defined by this contract.

## Cognee qualification boundary

The current Cognee adapter derives a dataset name from a trusted routed
namespace and passes that dataset to `sdk.search`. A Cognee dataset or
`node_set` is a physical routing/partitioning detail, not an ACL authority.
The adapter's metadata/evidence checks after `sdk.search` do not establish
pre-retrieval source authorization.

Therefore a Cognee provider can consume this plan only after a qualified
provider-side source-revision restriction (or an equivalent trusted isolation
mechanism) is demonstrated. A provider that can only search a dataset/node set
and filter returned evidence afterward must be rejected with
`source_isolation_unproven`; it must not receive a physical call for a mixed or
unprovable authorized scope.
