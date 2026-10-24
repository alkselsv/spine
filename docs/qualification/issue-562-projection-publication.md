# Issue #562 composed projection publication and recovery

Status: **PASS**.

Qualification run `issue562-r6-010` exercised the complete composed projection
publication lifecycle on exact commit
`a35f80f0747880f4bbf67788050db1607bc80ce7`, deployment fingerprint
`85cc8937d8a721a808e32b7086c66721c23a32bf4067e65f3289f7db4d9c05a5`,
authorization generation 2 and provider profile digest
`8935297e3def5e786f7e3d34db87d13c2106fbb7f82f8d695c747cee4fe1c84f`.
The Q16-01 and R4 prerequisite reports pin the same target.
The exact target, provider profile and bounded execution were authorized in
[#562 comment 6388915881](https://github.com/alkselsv/spine/issues/562#issuecomment-6388915881).

The fresh physical run used PostgreSQL, Neo4j and PGVector with the approved
provider profile. All ten #212 gates passed in one coherent invocation,
including public SDK compatibility, structured provenance, unsafe-evidence
rejection, workspace and namespace isolation, explicit retrieval context,
tombstones, rebuild/recovery validation, failure cleanup and canonical
lifecycle separation. The committed content-free report is
`issue-562-cognee-production-report.json`; its SHA-256 is
`dff0caafe702e6c14eec040736535605aaab3da09363d7acc1597439bf626076`.

The run began from active generation 4. A pre-validation fault and retry
published generation 5; a post-validation fault and retry published generation
6. A separate clean recovery successor then published generation 7. The run
subsequently tombstoned one synthetic current revision through the public
lifecycle API, advancing canonical acceptance from generation 14 to 15, and a
new non-replay successor captured that changed head. The final generation-8
snapshot `38971756-e09e-5198-b2b3-7c820bf46a8b` is bound to boundary
`e180710f-710b-467a-a82b-c076537a58df` and records eight included revisions
plus three tombstones.

The one-shot `before_physical_validation` fault left its candidate open with no
receipt or snapshot and preserved activation generation 4. After dispatcher
restart, the same logical command completed and advanced to generation 5.
The one-shot `after_validation_before_activation` fault left a sealed,
validated candidate with receipt `11b6bc96-1880-57b5-9eae-2534d6c8176b`
but no snapshot and preserved generation 5. Its restart retry advanced to
generation 6. Each injected fault
has one durable bounded audit record; successful delivery cleared transient
errors without erasing that audit evidence.

A deliberately stale activation request was rejected through the production
repository contract without changing the active generation. The final
changed-head command `edcedad2-3a3b-4fe5-80ed-39d6e4202ffe` is a distinct,
non-replay recovery successor: it sealed candidate
`52f52a83-13b0-5a45-813c-dab517ef4f56`, retained receipt
`3a38be38-a32f-556d-af69-f11064effdea`, and activated generation 8 through CAS.
Its predecessor is the generation-7 snapshot, so no historical pointer was
rewound or mutated.

Three public, scope-authorized document retrievals addressed the two retained
R4 tombstones and the tombstone created during r6-010. Each returned its exact
current canonical revision as `tombstoned` with no `content` field while one or
more historical projection references remained. Stale physical artifacts did
not restore ordinary disclosure.

All thirteen scenarios in the issue-specific runbook passed. Diagnostics retain
only bounded identifiers, states and reason codes; no document content,
credentials, provider error text or endpoints are present in the evidence.
Qualification fault profiles were removed after the run. The disposable target
was stopped after evidence capture; no further paid provider calls are required
for this result.
