# Issue #509 recovered application attempt v3

Status: **application recovery qualification remains NOT_VERIFIED**.

The owner-authorized synthetic run restored a fresh application target from
the verified `recovery:issue509-005` archive. PostgreSQL TLS, the application
role posture and runtime readiness were verified before the projection attempt.
The host-side `neo4j_community` handler passed its isolated dataset preflight
with backend access control enabled.

The public projection consumer reached the approved provider profile. LLM and
embedding connectivity succeeded, and the physical Cognee pipeline completed
against the isolated PostgreSQL and Neo4j stores. The canonical handoff then
failed closed with `projection.request.integrity_conflict`. No canonical
projection receipt, snapshot, activation or active pointer was committed.

The immutable archive was inspected after cleanup using metadata-only table
selection. Its projection partition had a consistent command identity and
digest and contained no prior projection receipt. That narrows the failure but
does not prove which internal canonical-handoff conflict branch fired. The
report therefore records the exact safe public failure code and leaves the
internal cause explicitly unverified.

Because publication was unavailable, the permitted-source application read was
not executed. No provider retry, serving or cutover occurred. Runtime database
access was revoked, the disposable operator authentication material was rotated
to an unknown value, the temporary authentication volume and graph container
were removed, and PostgreSQL was stopped.

This report does not claim production qualification. The canonical handoff
conflict must be diagnosed with safe stage-level evidence before any separately
authorized provider-backed retry. Normal publication, the single authorized
application read and independent evidence review remain required.

The JSON companion contains only opaque identifiers, digests, counts, bounded
states and safe reason codes. It contains no source content, authentication
values or raw provider response.
