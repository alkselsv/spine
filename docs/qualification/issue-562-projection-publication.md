# Issue #562 composed projection publication and recovery

Status: **PASS**.

Qualification run `issue562-r6-007` exercised the composed projection
publication lifecycle on exact commit
`a35f80f0747880f4bbf67788050db1607bc80ce7`, deployment fingerprint
`85cc8937d8a721a808e32b7086c66721c23a32bf4067e65f3289f7db4d9c05a5`,
authorization generation 2 and provider profile digest
`8935297e3def5e786f7e3d34db87d13c2106fbb7f82f8d695c747cee4fe1c84f`.
The Q16-01 and R4 prerequisite reports pin the same target.

The fresh physical run used PostgreSQL, Neo4j and PGVector with the approved
provider profile. All ten #212 gates passed in one coherent invocation,
including public SDK compatibility, structured provenance, unsafe-evidence
rejection, workspace and namespace isolation, explicit retrieval context,
tombstones, rebuild/recovery validation, failure cleanup and canonical
lifecycle separation. The committed content-free report is
`issue-562-cognee-production-report.json`; its SHA-256 is
`6af5671a3b6fd8c8bc40f87e3a10be777ebc33c9aac9b3eab4f2d9436952fca9`.

The initial active publication was stale by 13 canonical changes. A new
successor captured the current canonical boundary and published generation 2.
A second successor published generation 3 with final snapshot
`d3205816-19f2-56ee-9eb4-79225a639baa`. The final snapshot is bound to
boundary `b73d093f-8b72-4fa2-a41d-a35452d1bef4`, contains all 11 boundary
members, and records nine included revisions plus two tombstones. Its physical
validation passed, and 17 exact source references cover the nine processed
revisions and processing receipts.

The one-shot `before_physical_validation` fault left its candidate open with no
evidence or snapshot and preserved activation generation 1. After dispatcher
restart, the same delivery completed on attempt 2 and advanced to generation 2.
The one-shot `after_validation_before_activation` fault left a sealed,
validated candidate but no snapshot and preserved generation 2. Its restart
retry completed on attempt 2 and advanced to generation 3. Each injected fault
has one durable bounded audit record; successful delivery cleared transient
errors without erasing that audit evidence.

A deliberately stale activation request was rejected through the production
repository contract. The active snapshot, generation 3 and CAS version 3 were
unchanged before and after the probe. Recovery therefore occurred by publishing
a separately validated successor from the last active snapshot, never by
rewinding or mutating the active pointer.

All thirteen scenarios in the issue-specific runbook passed. Diagnostics retain
only bounded identifiers, states and reason codes; no document content,
credentials, provider error text or endpoints are present in the evidence.
Qualification fault profiles were removed after the run, and no further paid
provider calls are required for this result.
