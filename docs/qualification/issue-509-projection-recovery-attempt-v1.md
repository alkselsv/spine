# Issue #509 Context Projection recovery attempt v1

Status: **physical Context Projection recovery verified; complete production
qualification remains NOT_VERIFIED**.

The owner-authorized synthetic run reused the verified canonical restore from
`recovery:issue509-005` and rebuilt one restored Source Revision into fresh,
task-owned PGVector and Neo4j stores. The provider-backed projection completed,
and a strict candidate-only retrieval canary returned three chunks with three
matching evidence records. The candidate remained unpublished throughout the
exercise.

The first three bounded attempts failed closed while provider connectivity was
unavailable. After the owner reported that access was repaired, one additional
bounded run completed. A diagnostic replay also established that the normal
retrieval port intentionally rejects a BUILDING candidate. Qualification
therefore used a non-persisted ACTIVE view only to inspect the isolated physical
candidate through the same strict mapper; it did not change canonical state or
the publication pointer.

The restored application status canary verified the fail-closed `unpublished`
state. It did not perform the acceptance criterion's permitted-source read,
because the run authorization excluded promotion or activation. The restore
also exposed two procedural gaps: application table ACLs were not effective
after restore, and the restored SECURITY DEFINER function owner could not see
tenant rows under RLS. Temporary qualification grants and the temporary owner
change were reverted after capture.

No serving, promotion, activation or cutover occurred. PostgreSQL and Neo4j
targets were stopped, while task-owned volumes and metadata-only evidence were
retained for independent review. The issue must remain open until the restored
runtime ACL/ownership procedure is corrected, an authorized application source
read is exercised, and an independent reviewer approves the final evidence.

The JSON companion contains only opaque identifiers, digests, counts, bounded
states and safe reason codes. It contains no source content, filenames,
credentials or raw errors.
