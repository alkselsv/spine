# Issue #509 recovered application attempt v1

Status: **application recovery qualification remains NOT_VERIFIED**.

The owner-authorized synthetic run created a separate application target from
the verified archive for `recovery:issue509-005`. Unlike the forensic restore
verifier, this target preserved application ownership and ACLs. Runtime database
readiness passed, including the restored forced-RLS and SECURITY DEFINER posture.

The normal public projection consumer then attempted a provider-backed rebuild.
Canonical request resolution and preparation passed after the temporary runner
normalized UUID values at its raw database adapter boundary. The provider
connection check timed out after its bounded retry, and the consumer failed
closed with `projection.request.physical_retryable`.

Post-failure inspection recorded zero projection receipts, zero snapshots and
zero activations. No publication existed, so the permitted-source application
read was not executed. No serving or cutover occurred. Runtime database access
was revoked, the temporary operator authentication material was rotated after a
third-party migration log disclosed it, and PostgreSQL and Neo4j were stopped.

This report does not claim production qualification. A fresh authorized run must
complete provider-backed projection and normal publication, then exercise the
single permitted-source application read. Independent evidence review remains
required.

The JSON companion contains only opaque identifiers, digests, counts, bounded
states and safe reason codes. It contains no source content, credentials or raw
provider response.
