---
status: accepted
---

# Canonical document access policy and delegated retrieval authority

Spine uses the current immutable-versioned `AccessPolicy` of each `SourceObject`
in PostgreSQL as the sole authorization authority because derived Cognee state,
cached retrieval results and historical policies cannot safely govern current
disclosure. Policies are allow-only, deny-by-default and fail-closed; revisions,
projected data, citations and derived answers inherit current source authority.
Security Domain, Cognee datasets and Context Profiles may only narrow access.

Authorization principals are distinct from `ActorRef`: humans and teams receive
`read_content`, while services receive purpose-and-operation-scoped
`process_content`. An Agent Deployment acts through an explicit service principal
and never inherits its creator's authority. Interactive execution receives only
the intersection of the authenticated human's authority and the service grant;
server-derived purpose and operation prevent confused-deputy use.

The R1 `administrator` role controls lifecycle, safe operational metadata and
policy administration but is not a content-superuser role. A self-benefiting
policy, membership, template or service-authority change requires approval by a
different verified human administrator with authority in the same
workspace/environment; ordinary revocations and changes benefiting only others
may be activated by one authorized administrator. R1 has no break-glass,
delegation or impersonation. One-time sealed bootstrap and separately authorized
administrator recovery establish or restore administration without granting
content access.

New content remains isolated under narrow admission authority until it has an
eligible active policy. Disclosure requires complete current authorization for
every contributing source, including multi-source graph facts and stored answers;
incomplete provenance fails closed. Authorization is revalidated before model
use and each protected disclosure, so policy or identity changes stop future
output without claiming that already transmitted bytes can be retracted.

These choices trade some operational convenience and availability for explicit
least privilege, immediate revocation, auditable separation of duties and a
single canonical enforcement boundary. Detailed bootstrap, proposal, admission,
metadata, concurrency, recovery and non-leakage semantics are part of the R1
architecture contract. The following safe-metadata decision applies: Access
Policy version identity and
generation may be exposed without grants, principal membership or protected
source fields. Security Domain is represented only by an immutable,
source-owned `SourceSecurityDomainBinding` resolved through the canonical policy
authority. Its reference, version and binding version are deny-only metadata;
they never grant `read_content` or `process_content` and are never read from
Cognee, projection partitions or provider payloads.

Binding-bearing policy documents use `access-policy.v2` and include the binding
reference in their canonical digest. Existing `access-policy.v1` documents and
hashes remain unchanged, and the binding reference is nullable with no
backfill. Missing or unavailable bindings are rendered as absent/null with a
safe status; no inferred default or provider diagnostic is published. The
metadata response remains independently authorized from protected content, so
an administrator without an explicit read grant cannot receive title, filename,
checksum, text, chunks or excerpts.
See the architecture contract in
[`docs/ARCHITECTURE.md`](../ARCHITECTURE.md#workspace-и-доступ).
