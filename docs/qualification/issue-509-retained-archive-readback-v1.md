# Issue #509 retained archive read-back v1

Status: **VERIFIED METADATA-ONLY OBSERVATION**.

The retained synthetic recovery volumes were inspected without network access
and without mounting any source volume writable. A disposable clone of the
stopped PostgreSQL volume was used only for allowlisted metadata queries and was
removed after capture. The original evidence and artifact volumes remain
unchanged.

The read-back confirms that `recovery:issue509-005` is a coordinated coherent
manifest with the recorded fence epoch and cutoff, a PostgreSQL recovery
watermark, an original-storage generation, verified PostgreSQL and object
components, explicit component verification references, SHA-256 integrity and
zero orphaned or indeterminate operations.

The PostgreSQL archive SHA-256 and original-storage archive SHA-256 match the
manifest component identities already recorded by recovery attempt v3. The
archive inventory contains canonical SourceObject/SourceRevision state,
finalized original-upload state, outbox intent/delivery state and recovery
quiescence evidence. The single finalized original record is
`finalized_verified`; its observed digest and length match its immutable object
reference. The original archive passed tar header verification and contains the
expected private receipt plus original object entry, without exposing either
name or content.

This observation is not another backup or restore and makes no serving,
cutover, customer-data or provider-usage claim.
