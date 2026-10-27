# Issue #509 recovered application attempt v2

Status: **application recovery qualification remains NOT_VERIFIED**.

The owner-authorized synthetic run created a fresh application target from the
verified archive for `recovery:issue509-005`. The target preserved application
ownership and ACLs. After applying the production `NOINHERIT` posture to the
fresh cluster's runtime role, the normal runtime database readiness check passed.

The normal public projection consumer reached the approved provider profile.
Both the LLM and embedding connection checks passed. Physical projection then
failed closed before canonical publication because the Neo4j target did not
support the multi-database behavior required by the current Cognee adapter. The
safe failure was `Neo4jMultiDatabaseSupportError` with status 422, recorded here
as `neo4j_multi_database_unsupported`.

Post-failure inspection recorded zero projection receipts, zero snapshots, zero
activations and zero active projections. Because no publication existed, the
permitted-source application read was not executed. No provider retry, serving
or cutover occurred. Runtime database access was revoked, the disposable
operator authentication material was rotated to an unknown value, the temporary
authentication volume was removed, and PostgreSQL and Neo4j were stopped.

This report does not claim production qualification. A compatible Neo4j target
must be established before another separately authorized provider-backed run can
complete projection, normal publication and the single permitted-source
application read. Independent evidence review remains required.

The JSON companion contains only opaque identifiers, digests, counts, bounded
states and safe reason codes. It contains no source content, authentication
values or raw provider response.
