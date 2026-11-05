# Commit-fence production composition

Status: fail closed; PostgreSQL F1 sources are composition-eligible, no external
F2 provider is production-qualified.

`spine.infrastructure.commit_fence_composition` is the only production
selection boundary for EF commit-fence sources. It consumes immutable deployment
inputs and resolves one exact `CommitFenceBinding`; it is not an authority
registry and never creates or mutates an approval.

The production configuration and each F1 source declaration carry explicit v1
schema versions. Unsupported versions fail during construction, including when
the public immutable composition type is constructed directly instead of through
its convenience factory.

## F1 source allowlist

The production composition accepts only these demonstrated PostgreSQL owners:

| Owner | Stable issuer | Qualified boundary |
| --- | --- | --- |
| `source_lifecycle` | `postgresql_f1_issuer_id(SOURCE_LIFECYCLE)` | Current canonical source-lifecycle pointer and decision protected by the existing shared/exclusive source lock. |
| `access_policy` | `postgresql_f1_issuer_id(ACCESS_POLICY)` | Current AccessPolicy pointer/version protected by its existing row lock. |
| `retention_applicability` | `postgresql_f1_issuer_id(RETENTION_APPLICABILITY)` | Current exact-source retention-applicability pointer protected by its existing row lock. |

Eligibility is not a global grant. A consuming production root must explicitly
bind each owner to an exact set of `(purpose, action)` uses. Resolution also
requires the exact workspace/environment and the caller's active PostgreSQL Unit
of Work. Missing UoW, missing use, unknown issuer, overlapping sources or a
cross-scope binding denies selection. The selected F1 adapter still performs its
canonical state, lineage and validity checks inside that UoW.

The PostgreSQL manifest release pointer remains owned and fenced by #811; this
composition does not duplicate it. Upstream recording, retention terms,
manifest issuance, deletion and AccessPolicy approver authorities are not F1.

The selection returns the named owner and a transaction-bound F1 adapter. A
consumer can use that result at #795's existing retry-safe `authorize_commit`
injection seam; #976/#977 own the later deletion and retention consumer wiring.
This issue does not bypass those consumer tickets by changing #795 storage.

## F2 remains unavailable

There are currently no production F2 qualification records. Consequently the
v1 production composition has no external-source registration input: passing a
synthetic, missing, arbitrary or otherwise external adapter where an F1 source
is required fails construction, and every external binding resolves to DENY.
Direct construction cannot bypass this rule.

When #983 records real provider-, contract- and environment-specific evidence,
it must introduce a versioned successor composition (or an equally explicit
evidence-issued source type) binding the named provider, issuer, exact scope,
contract version, purpose/action uses and reviewed evidence. Caller-supplied
UUIDs or a self-declared evidence flag are not qualification.

## Isolated test composition

`build_test_commit_fence_composition` keeps deterministic fixtures available
without adding them to production selection. It binds one fixture to one exact
`CommitFenceBinding`; any changed issuer, tenant, environment, purpose, action,
lineage, generation, operation or validity field denies. The synthetic authority
continues to live under `tests/` and is not packaged with `spine`.

## Unchanged security boundary

Composition adds no database object, authority table, runtime grant, RLS policy
or `BYPASSRLS` capability. It changes no canonical owner and provides no live
provider credentials, rollout or permission expansion.
